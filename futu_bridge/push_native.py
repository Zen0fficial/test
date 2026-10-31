"""Native subscription ownership and callback routing for the desktop provider.

Only market-data callbacks enter this adapter. Reconnect reuses the same verified
process, releases only handles allocated by this provider, and reports an unknown
interval. Neither reconnect nor a current book snapshot constitutes tick replay.
"""

import ctypes
from ctypes import wintypes
import hashlib
import threading
import uuid

from .provider import ProviderError, ProviderResult
from .push import NativePushStream


class NativePushMixin:
    def _init_push(self, stream_mode, push_queue_size):
        if stream_mode not in ("push", "poll"):
            raise ValueError("stream_mode must be push or poll")
        if type(push_queue_size) is not int or not 1 <= push_queue_size <= 100000:
            raise ValueError("push_queue_size must be an integer between 1 and 100000")
        self.stream_mode, self.push_queue_size = stream_mode, push_queue_size
        self._capture_generation = 0
        self._push_contexts = {}
        self._push_all_contexts = {}
        self._push_registry_lock = threading.RLock()
        self._push_lifecycle_lock = threading.RLock()
        self._owned_push_handles = set()
        self._push_closing = False
        self._push_cleanup_warnings = []

    def _capture_process_identity(self):
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        kernel.GetProcessTimes.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, self.pid)
        if not handle:
            raise ProviderError("desktop_process_unavailable", "Cannot inspect the desktop process generation.", 503)
        times = [wintypes.FILETIME() for _ in range(4)]
        try:
            if not kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
                raise ProviderError("desktop_process_unavailable", "Cannot verify the desktop process generation.", 503)
            return (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        finally:
            kernel.CloseHandle(handle)

    def _push_context_list(self):
        if not hasattr(self, "_push_contexts"):
            return []
        with self._push_registry_lock:
            return list(self._push_all_contexts.values())

    def _push_disconnected(self, reason):
        for context in self._push_context_list():
            context["stream"].disconnected(reason)

    def _on_push_message(self, message, data, generation):
        if self._closed or generation != self._capture_generation:
            return
        if message.get("type") == "error":
            for context in self._push_context_list():
                with context["lock"]:
                    context["decoder"].reset()
                    context["first"].clear()
                context["stream"].note_gap("native_agent_error", {"unknown_interval": True})
            return
        envelope = message.get("payload")
        if message.get("type") != "send" or not isinstance(envelope, dict) or envelope.get("kind") != "native_push":
            return
        with self._push_registry_lock:
            context = self._push_contexts.get(envelope.get("subscription_id"))
        if context is None:
            return  # An old generation or an already closed subscription.
        with context["lock"]:
            stream = context["stream"]
            try:
                if envelope.get("capture_epoch") != context["token"]:
                    context["decoder"].reset()
                    context["first"].clear()
                    stream.note_gap("invalid_capture_epoch", {"unknown_interval": True})
                    return
                previous_gaps = stream.status()["capture_gaps"]
                if not stream.observe_capture(envelope.get("capture_sequence"), envelope["capture_epoch"]):
                    context["decoder"].reset()
                    context["first"].clear()
                    return
                if stream.status()["capture_gaps"] != previous_gaps:
                    context["decoder"].reset()
                    context["first"].clear()
                event = envelope.get("event")
                if event != "data":
                    context["decoder"].reset()
                    context["first"].clear()
                    stream.note_gap(event or "unknown_native_status", {
                        "reason": envelope.get("reason"), "protocol": envelope.get("protocol"),
                        "unknown_interval": True, "exchange_replay_verified": False})
                    return
                if envelope.get("protocol") != 6212 or not isinstance(data, (bytes, bytearray)):
                    raise ValueError("Invalid native market push frame")
                if envelope.get("payload_size") != len(data):
                    raise ValueError("Native push payload length mismatch")
                decoded = context["decoder"].decode(bytes(data), allowed_stock_ids=set(context["records"]))
                from .native import normalize_book, normalize_quote, normalize_trades, utcnow
                for update in decoded:
                    kind = "quotes" if update["kind"] == "quote" else update["kind"]
                    if kind not in context["types"]:
                        continue
                    for record in context["records"].get(update["stock_id"], []):
                        if kind == "quotes":
                            value = {"quotes": [normalize_quote(record, update["raw"], utcnow())], "count": 1}
                        elif kind == "orderbook":
                            value = normalize_book(record, update["raw"], 10)
                        elif kind == "trades":
                            value = normalize_trades(record, update["raw"])
                        else:
                            continue
                        metadata = {key: item for key, item in update.items() if key not in ("raw", "kind")}
                        value["push"] = metadata
                        source = self._source("native_push")
                        source.update({"live_push": True, "protocol": 6212,
                                       "native_source_raw": envelope.get("native_source_raw"),
                                       "captured_at_unix_ms": envelope.get("captured_at_unix_ms"),
                                       "description": "Decoded native market-data push; no repeated snapshot requests.",
                                       "exchange_sequence_verified": False})
                        key = (kind, record["symbol"], update.get("bit"), update.get("prob"), update.get("broker_id"))
                        first = key not in context["first"]
                        warnings = ["No verified exchange sequence or historical replay is available for this push route."]
                        if kind == "orderbook":
                            warnings.append("Book updates describe price levels; they are not individual order placement/cancellation messages.")
                        stream.ingest(kind, record["symbol"], ProviderResult(value, source, warnings),
                                      capture_epoch=context["token"], initial_snapshot=first)
                        context["first"].add(key)
            except Exception as exc:
                # Callback failures cannot escape into Frida or disappear silently.
                context["decoder"].reset()
                context["first"].clear()
                stream.note_gap("push_decode_error", {"error_type": type(exc).__name__, "unknown_interval": True})

    def _start_push_context(self, context, reconnect=False):
        from .push_wire import PushDecoder
        if self._closed or self._push_closing or context["stream"].status()["closed"]:
            raise ProviderError("stream_closed", "The native subscription is closed.", 503)
        token = uuid.uuid4().hex
        with context["lock"]:
            context.update(token=token, decoder=PushDecoder(depth=10), first=set(), handles=[])
        with self._push_registry_lock:
            self._push_contexts[token] = context
            self._push_all_contexts[id(context)] = context
        if reconnect:
            # Native Sub may synchronously deliver bootstrap callbacks.
            context["stream"].reconnected(token)
        try:
            prepared = self._rpc("pushprepare", token)
            context["handles"] = list(dict.fromkeys(context["handles"] + prepared.get("handles", [])))
            self._owned_push_handles.update(context["handles"])
            if self._closed or self._push_closing or context["stream"].status()["closed"]:
                raise ProviderError("stream_closed", "The native subscription closed during preparation.", 503)
            result = self._rpc("pushsubscribe", token, list(context["records"]), context["types"])
            # A partial/error result may omit a handle already allocated during
            # preparation. Keep ownership until this token is unsubscribed.
            context["handles"] = list(dict.fromkeys(context["handles"] + result.get("handles", [])))
            self._owned_push_handles.update(context["handles"])
            if self._closed or self._push_closing or context["stream"].status()["closed"]:
                raise ProviderError("stream_closed", "The native subscription closed during setup.", 503)
            accepted = {(entry["stock_id"], entry["type"]) for entry in result.get("accepted", [])}
            required = {(stock_id, kind) for stock_id in context["records"] for kind in context["types"]}
            if result.get("status") != "locally_subscribed" or not required.issubset(accepted):
                raise ProviderError("push_subscription_unavailable", "Futu did not accept every requested native subscription.", 422,
                                    {"accepted": result.get("accepted", []), "skipped": result.get("skipped", [])})
            context["stream"].source.update({"subscription": result, "server_acceptance_verified": False})
            context["subscription"] = result
            return {"capture_epoch": token, "source": {"subscription": result}}
        except Exception:
            self._remove_push_context(context)
            raise

    def _stream_push(self, symbols, types):
        if self._closed or self._detached:
            raise ProviderError("desktop_disconnected", "Reconnect an existing stream or open a new desktop client.", 503)
        if "pushsubscribe" not in self.native_info.get("methods", []):
            raise ProviderError("push_unavailable", "The verified native push adapter is unavailable.", 503)
        if not symbols or not types or not set(types).issubset({"quotes", "trades", "orderbook"}):
            raise ValueError("Provide symbols and quote/orderbook/trade stream types")
        records = {}
        for symbol in symbols:
            record = self._resolve(symbol)
            if "trades" in types and record.get("instrument_type_v2") == 6:
                raise ProviderError("index_ticks_unsupported", "Index ticks are not executed market trades.", 422)
            records.setdefault(record["stock_id"], []).append(record)
        context = {"records": records, "types": list(dict.fromkeys(types)), "lock": threading.RLock(), "handles": []}
        canonical = list(dict.fromkeys(record["symbol"] for rows in records.values() for record in rows))
        source = self._source("native_push")
        source.update({"protocol": 6212, "description": "Native desktop market push, with explicit delivery gaps and no exchange replay."})
        stream = NativePushStream(canonical, context["types"], source, max_queue=self.push_queue_size,
                                  on_close=lambda: self._remove_push_context(context),
                                  on_reconnect=lambda: self._reconnect_push_context(context))
        context["stream"] = stream
        with self._push_lifecycle_lock:
            try:
                self._start_push_context(context)
            except Exception:
                stream.close()
                raise
        return stream

    def _remove_push_context(self, context):
        with context["lock"]:
            token = context.get("token")
            handles = set(context.get("handles", []))
        script = self._script
        with self._push_registry_lock:
            if token and self._push_contexts.get(token) is context:
                self._push_contexts.pop(token, None)
            if context["stream"].status()["closed"]:
                self._push_all_contexts.pop(id(context), None)
        if token and not self._closed and not self._detached and script is not None:
            finished = threading.Event()

            def cleanup():
                try:
                    # Cleanup cannot wait behind a pending history/reconnect RPC.
                    script.exports_sync.pushunsubscribe(token)
                    with self._push_registry_lock:
                        self._owned_push_handles.difference_update(handles)
                except Exception:
                    # Retain ownership for recovery after capture reattachment.
                    pass
                finally:
                    finished.set()

            threading.Thread(target=cleanup, name="futu-push-cleanup", daemon=True).start()
            if not finished.wait(0.2):
                with self._push_registry_lock:
                    self._push_cleanup_warnings.append({"reason": "native_unsubscribe_timeout", "handles": sorted(handles)})
                    self._push_cleanup_warnings[:] = self._push_cleanup_warnings[-16:]

    def _reconnect_push_context(self, context):
        from .native import _frida, QOTBASE_SHA256, COREBIZ_SHA256
        with self._push_lifecycle_lock:
            if self._closed:
                raise ProviderError("desktop_disconnected", "The desktop provider is closed.", 503)
            if not self._detached:
                self._remove_push_context(context)
                self._settle_push_contexts([context])
                return self._start_push_context(context, reconnect=True)
            if self._capture_process_identity() != self._process_started:
                raise ProviderError("desktop_process_changed", "Futu restarted; create a new client and rediscover option identities.", 503)
            if (hashlib.sha256(self.module_path.read_bytes()).hexdigest() != QOTBASE_SHA256 or
                    hashlib.sha256(self.core_module_path.read_bytes()).hexdigest() != COREBIZ_SHA256):
                raise ProviderError("version_unsupported", "Desktop modules changed before reconnect.", 503)
            contexts = self._push_context_list()
            if not any(item is context for item in contexts):
                contexts.append(context)
            with self._push_registry_lock:
                self._push_contexts.clear()
            try:
                self._attach_capture(_frida().get_local_device())
                retained = list(self._owned_push_handles)
                for offset in range(0, len(retained), 100):
                    self._rpc("pushrecover", retained[offset:offset + 100])
                    self._owned_push_handles.difference_update(retained[offset:offset + 100])
                self._settle_push_contexts(contexts)
            except Exception:
                self._detached = True
                session = self._session
                if session is not None:
                    try:
                        session.detach()
                    except Exception:
                        pass
                for item in contexts:
                    item["stream"].disconnected("capture_recovery_failed")
                raise
            result, target_error = None, None
            for item in contexts:
                try:
                    restored = self._start_push_context(item, reconnect=True)
                    if item is context:
                        result = restored
                except Exception as exc:
                    item["stream"].disconnected("resubscribe_failed")
                    if item is context:
                        target_error = exc
            if target_error is not None:
                raise target_error
            return result

    def _settle_push_contexts(self, contexts):
        """Wait for native unsubscribe batching; this never polls market data."""
        if "pushsettle" not in self.native_info.get("methods", []):
            return
        keys = {}
        for context in contexts:
            for item in context.get("subscription", {}).get("accepted", []):
                key = (item["stock_id"], item["bit"], item["prob"])
                keys[key] = {name: item[name] for name in ("stock_id", "bit", "prob")}
        if keys:
            values = list(keys.values())
            for offset in range(0, len(values), 400):
                settled = self._rpc("pushsettle", values[offset:offset + 400], 2500)
                for context in contexts:
                    context["stream"].source["unsubscribe_settlement"] = settled

    def _close_push_streams(self):
        if not hasattr(self, "_push_registry_lock"):
            return
        with self._push_registry_lock:
            self._push_closing = True
            contexts = list(self._push_all_contexts.values())
        for context in contexts:
            context["stream"].close()

