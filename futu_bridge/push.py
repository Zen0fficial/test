"""Bounded delivery of native callbacks; no polling or exchange-completeness claim.

The adapter must call ``ingest`` for each subscribed capture message. Capture
sequences are generated locally by the interception agent, not by an exchange.
They detect losses between that agent and this object only. Trade IDs are opaque
identities and are never interpreted as a contiguous exchange sequence.
"""

from collections import OrderedDict, deque
import copy
import datetime as dt
import json
import threading
import time
from typing import Callable, Dict, List, Optional

from .provider import EventStream, ProviderError, ProviderEvent, ProviderResult


_CLOCK_FIELDS = {"observed_at", "served_at", "request_id", "RecvPushTime",
                 "ServerSendTimeMs", "ServerRecvTimeMs", "raw_times"}


def _utcnow():
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _market_value(value):
    if isinstance(value, dict):
        return {key: _market_value(item) for key, item in value.items() if key not in _CLOCK_FIELDS}
    if isinstance(value, list):
        return [_market_value(item) for item in value]
    return value


class NativePushStream(EventStream):
    """A thread-safe callback queue with observable overflow and reconnect gaps.

    ``on_reconnect`` runs outside the queue lock and must enforce its own native
    request timeout. It may return ``{"capture_epoch": value, "source": {...}}``.
    It should install the new capture before returning. Deduplication memory is
    preserved, so replayed initial snapshots are not presented as new executions.
    It is bounded: an ID evicted from that memory can subsequently be emitted
    again. ``on_close`` also runs outside the queue lock, after waking readers.

    Capture numbering is expected to start at 1 for every subscription epoch.
    Set ``capture_sequence_starts_at=None`` only if the adapter cannot know the
    first expected number (for example, attachment to an existing global stream).

    One pending, coalesced gap notice is stored separately from the bounded data
    queue. Thus a slow consumer cannot silently overwrite its overflow warning.
    Heartbeat events carry lifecycle and gap information for existing SSE clients.
    """

    def __init__(self, symbols: List[str], types: List[str], source: Dict,
                 max_queue: int = 1024, max_seen_trade_ids: int = 10_000,
                 on_close: Optional[Callable] = None,
                 on_reconnect: Optional[Callable] = None,
                 capture_epoch=None, capture_sequence_starts_at=1):
        if not symbols or not types or not set(types).issubset({"quotes", "orderbook", "trades"}):
            raise ValueError("Provide symbols and quote/orderbook/trade stream types")
        if not isinstance(source, dict) or not source:
            raise ValueError("Native push requires nonempty source provenance")
        if isinstance(max_queue, bool) or not isinstance(max_queue, int) or max_queue < 1:
            raise ValueError("max_queue must be a positive integer")
        if (isinstance(max_seen_trade_ids, bool) or not isinstance(max_seen_trade_ids, int)
                or max_seen_trade_ids < 1):
            raise ValueError("max_seen_trade_ids must be a positive integer")
        if capture_sequence_starts_at is not None and (
                isinstance(capture_sequence_starts_at, bool)
                or not isinstance(capture_sequence_starts_at, int) or capture_sequence_starts_at < 1):
            raise ValueError("capture_sequence_starts_at must be a positive integer or None")
        self.symbols = list(dict.fromkeys(symbols))
        self.types = list(dict.fromkeys(types))
        self.max_queue, self.max_seen_trade_ids = max_queue, max_seen_trade_ids
        self.source = dict(source)
        self.source.update({"kind": "native_push", "delivery": "native_callback", "live_push": True,
                            "lossless": False, "gaps_possible": True,
                            "exchange_sequence_verified": False,
                            "deduplication_replays_supported": False,
                            "max_seen_trade_ids_per_symbol": max_seen_trade_ids,
                            "types": self.types, "symbols": self.symbols})
        self._condition = threading.Condition()
        self._queue = deque()
        self._pending_gap = None
        self._seen = {}
        self._closed = False
        self._connected = True
        self._reconnecting = False
        self._on_close, self._on_reconnect = on_close, on_reconnect
        self._capture_epoch = None if capture_epoch is None else str(capture_epoch)
        self._last_capture_sequence = None
        self._capture_sequence_starts_at = capture_sequence_starts_at
        self._event_sequence = 0
        self._counters = {name: 0 for name in (
            "capture_messages", "accepted_messages", "emitted_events", "duplicate_trades",
            "revised_trade_observations", "dedupe_evictions", "queue_dropped_events",
            "capture_gaps", "missing_capture_messages", "capture_regressions",
            "disconnects", "reconnects", "reconnect_failures", "backfill_messages")}

    def _event_source(self, source, kind, initial=False, backfill=False):
        result = dict(source)
        result.update(self.source)
        result.update({"upstream_kind": source.get("kind"), "kind": kind,
                       "observed_at": _utcnow(), "initial_snapshot": initial,
                       "backfill": backfill, "capture_epoch": self._capture_epoch})
        if backfill:
            result["live_push"] = False
        return result

    def _gap(self, category, **details):
        self._counters["capture_gaps"] += 1
        if self._pending_gap is None:
            self._pending_gap = {"event": "gap", "reasons": [], "occurrences": 0,
                                 "dropped_events": 0, "missing_capture_messages": 0,
                                 "first_observed_at": _utcnow()}
        gap = self._pending_gap
        if category not in gap["reasons"] and len(gap["reasons"]) < 16:
            gap["reasons"].append(category)
        gap["occurrences"] += 1
        gap["dropped_events"] += details.get("dropped_events", 0)
        gap["missing_capture_messages"] += details.get("missing_capture_messages", 0)
        gap["last_details"] = details
        gap["last_observed_at"] = _utcnow()
        self._condition.notify_all()

    def _enqueue(self, kind, data, source, warnings=()):
        if len(self._queue) >= self.max_queue:
            dropped = self._queue.popleft()
            self._counters["queue_dropped_events"] += 1
            self._gap("queue_overflow", dropped_events=1,
                      dropped_type=dropped[0], dropped_capture_sequence=dropped[1].get("capture_sequence"))
        self._queue.append((kind, data, source, list(warnings)))
        self._condition.notify_all()

    def _lifecycle(self, status, reason=None, **details):
        data = {"event": "stream_status", "status": status, "reason": reason,
                "capture_epoch": self._capture_epoch, **details}
        self._enqueue("heartbeat", data, self._event_source(self.source, "native_push_status"))

    def _observe_capture(self, sequence, epoch):
        if epoch is not None:
            epoch = str(epoch)
            if self._capture_epoch is not None and epoch != self._capture_epoch:
                self._gap("capture_epoch_changed", previous_epoch=self._capture_epoch,
                          capture_epoch=epoch, unknown_interval=True)
                self._last_capture_sequence = None
            self._capture_epoch = epoch
        if sequence is None:
            return True
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
            raise ProviderError("provider_contract_error", "Capture sequence must be a positive exact integer.", 502)
        self._counters["capture_messages"] += 1
        previous = self._last_capture_sequence
        if previous is None and self._capture_sequence_starts_at is not None:
            previous = self._capture_sequence_starts_at - 1
        if previous is not None:
            if sequence <= previous:
                self._counters["capture_regressions"] += 1
                self._gap("capture_sequence_regression", previous_capture_sequence=previous,
                          capture_sequence=sequence, unknown_interval=True)
                return False
            if sequence != previous + 1:
                missing = sequence - previous - 1
                self._counters["missing_capture_messages"] += missing
                self._gap("capture_sequence_gap", previous_capture_sequence=previous,
                          capture_sequence=sequence, missing_capture_messages=missing)
        self._last_capture_sequence = sequence
        return True

    def observe_capture(self, capture_sequence, capture_epoch=None):
        """Account for a capture message that the adapter intentionally filters.

        Do not call this and pass the same sequence to ``ingest``. If an agent
        supplies a global sequence, all its messages, including filtered messages,
        must be accounted for; a per-subscription sequence is preferable.
        """
        if (isinstance(capture_sequence, bool) or not isinstance(capture_sequence, int)
                or capture_sequence < 1):
            raise ProviderError("provider_contract_error", "Capture sequence must be a positive exact integer.", 502)
        with self._condition:
            if self._closed or not self._connected:
                return False
            return self._observe_capture(capture_sequence, capture_epoch)

    def note_gap(self, reason, details=None):
        """Report a decode failure or unverified recovery interval from the adapter."""
        if not isinstance(reason, str) or not reason or len(reason) > 256:
            raise ValueError("A gap reason must contain 1 to 256 characters")
        if details is not None and not isinstance(details, dict):
            raise ValueError("Gap details must be a dictionary")
        with self._condition:
            if not self._closed:
                self._gap(reason, **copy.deepcopy(details or {}))

    def _trades(self, data):
        rows = data.get("trades")
        if not isinstance(rows, list):
            raise ProviderError("provider_contract_error", "A native trade event requires a trades list.", 502)
        # Validate the complete batch before mutating deduplication state.
        prepared = []
        for row in rows:
            if (not isinstance(row, dict) or isinstance(row.get("trade_id"), (float, bool))
                    or not isinstance(row.get("trade_id"), (str, int)) or str(row["trade_id"]) == ""):
                raise ProviderError("provider_contract_error", "Native trade IDs must be nonempty exact strings or integers.", 502)
            try:
                row["trade_id"] = str(row["trade_id"])
                fingerprint = json.dumps(_market_value(row), sort_keys=True, separators=(",", ":"), allow_nan=False)
            except (ValueError, TypeError) as exc:
                raise ProviderError("provider_contract_error", "Native trade fields must be finite JSON data.", 502) from exc
            prepared.append((row, str(row["trade_id"]), fingerprint))
        seen = self._seen.setdefault(data["symbol"], OrderedDict())
        selected, new_ids, revised_ids = [], [], []
        for row, trade_id, fingerprint in prepared:
            previous = seen.get(trade_id)
            if previous == fingerprint:
                self._counters["duplicate_trades"] += 1
            else:
                selected.append(row)
                if previous is None:
                    new_ids.append(trade_id)
                else:
                    revised_ids.append(trade_id)
                    self._counters["revised_trade_observations"] += 1
            seen[trade_id] = fingerprint
            seen.move_to_end(trade_id)
            while len(seen) > self.max_seen_trade_ids:
                seen.popitem(last=False)
                self._counters["dedupe_evictions"] += 1
        data.update({"trades": selected, "count": len(selected), "received_count": len(rows),
                     "new_trade_ids": new_ids, "revised_trade_ids": revised_ids,
                     "complete_history": False})
        return bool(selected)

    def ingest(self, kind, symbol, result, capture_sequence=None, capture_epoch=None,
               initial_snapshot=False, backfill=False):
        """Receive one decoded native callback; never fetch data or poll upstream.

        ``initial_snapshot`` identifies subscription bootstrap data, which must not
        be described as newly executed trades. ``backfill`` identifies an explicit
        snapshot/history fetch used for recovery; it does not certify a filled gap.
        Returns whether a market event was queued (duplicates return False).
        """
        if (not isinstance(result, ProviderResult) or not isinstance(result.source, dict)
                or not result.source or not isinstance(result.data, dict)):
            raise ProviderError("provider_contract_error", "Native push requires data and source dictionaries.", 502)
        if kind not in self.types or symbol not in self.symbols:
            raise ProviderError("provider_contract_error", "Native event does not belong to this subscription.", 502)
        data = copy.deepcopy(result.data)
        if data.get("symbol", symbol) != symbol:
            raise ProviderError("provider_contract_error", "Native event symbol does not match its subscription route.", 502)
        data["symbol"] = symbol
        with self._condition:
            if self._closed or not self._connected:
                return False
            if not self._observe_capture(capture_sequence, capture_epoch):
                return False
            changed = self._trades(data) if kind == "trades" else True
            if not changed and not initial_snapshot and not backfill:
                return False
            self._counters["accepted_messages"] += 1
            if backfill:
                self._counters["backfill_messages"] += 1
            data.update({"initial_snapshot": bool(initial_snapshot), "backfill": bool(backfill),
                         "gaps_possible": True, "lossless": False,
                         "capture_sequence": (capture_sequence if capture_sequence is not None
                                              else None if backfill else self._last_capture_sequence),
                         "capture_epoch": self._capture_epoch})
            warnings = list(result.warnings)
            if initial_snapshot:
                warnings.append("Initial observed snapshot; these records are not claimed to be new market activity.")
            if backfill:
                warnings.append("Explicit recovery data; completeness of the interrupted interval is unverified.")
            if kind == "trades":
                warnings.append("Trade identity memory is bounded; an evicted ID may be delivered again. Revised IDs indicate changed observations, not a verified exchange correction message.")
            source = self._event_source(result.source, "native_push_snapshot" if initial_snapshot else "native_push",
                                        bool(initial_snapshot), bool(backfill))
            self._enqueue(kind, data, source, warnings)
            return True

    def note_backfill(self, kind, symbol, result):
        """Deliver explicitly requested recovery data without claiming gap repair."""
        return self.ingest(kind, symbol, result, initial_snapshot=True, backfill=True)

    def disconnected(self, reason="native_transport_disconnected"):
        with self._condition:
            if self._closed or not self._connected:
                return
            self._connected = False
            self._counters["disconnects"] += 1
            self._gap("disconnected", reason=str(reason), unknown_interval=True)
            self._lifecycle("disconnected", str(reason))

    def reconnected(self, capture_epoch=None, source=None):
        """Mark successful adapter reattachment; this is not exchange gap recovery."""
        with self._condition:
            if self._closed:
                return False
            if source:
                self.source.update({key: value for key, value in source.items()
                                    if key not in {"lossless", "gaps_possible", "exchange_sequence_verified", "live_push", "kind"}})
            if self._connected and capture_epoch is not None and str(capture_epoch) == self._capture_epoch:
                return True
            self._capture_epoch = (str(capture_epoch) if capture_epoch is not None
                                   else "reconnect-" + str(self._counters["reconnects"] + 1))
            self._last_capture_sequence = None
            self._connected = True
            self._counters["reconnects"] += 1
            self._gap("reconnected_without_verified_replay", unknown_interval=True)
            self._lifecycle("reconnected", "Interrupted interval has not been proven complete.")
            return True

    def reconnect(self):
        """Reattach through the adapter callback, retaining bounded trade identity memory."""
        with self._condition:
            if self._closed:
                raise ProviderError("stream_closed", "Cannot reconnect a closed native stream.", 409)
            if self._on_reconnect is None:
                raise ProviderError("reconnect_unavailable", "This native adapter has no reconnect callback.", 501)
            if self._reconnecting:
                raise ProviderError("reconnect_in_progress", "Native stream reconnection is already running.", 409)
            self._reconnecting = True
        self.disconnected("reconnect_requested")
        try:
            outcome = self._on_reconnect() or {}
            if not isinstance(outcome, dict):
                raise ProviderError("provider_contract_error", "Reconnect callback must return a dictionary or None.", 502)
            self.reconnected(outcome.get("capture_epoch"), outcome.get("source"))
        except Exception as exc:
            with self._condition:
                self._counters["reconnect_failures"] += 1
                if not self._closed:
                    self._connected = False
                    self._gap("reconnect_failed", unknown_interval=True)
                    self._lifecycle("reconnect_failed", "Native adapter could not restore this subscription.")
            if isinstance(exc, ProviderError):
                raise
            raise ProviderError("native_reconnect_failed", "Native adapter could not restore this subscription.", 503) from exc
        finally:
            with self._condition:
                self._reconnecting = False
        return self.status()

    def status(self):
        with self._condition:
            return {**self._counters, "connected": self._connected, "closed": self._closed,
                    "reconnecting": self._reconnecting, "queue_size": len(self._queue),
                    "queue_capacity": self.max_queue, "gap_notice_pending": self._pending_gap is not None,
                    "capture_epoch": self._capture_epoch, "last_capture_sequence": self._last_capture_sequence,
                    "seen_trade_ids": {symbol: len(ids) for symbol, ids in self._seen.items()},
                    "lossless": False, "exchange_sequence_verified": False,
                    "deduplication_replays_supported": False}

    def read(self, timeout):
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        deadline = time.monotonic() + timeout
        with self._condition:
            while True:
                if self._closed:
                    raise StopIteration
                if self._pending_gap is not None:
                    data, self._pending_gap = self._pending_gap, None
                    data.update({"gaps_possible": True, "lossless": False,
                                 "capture_epoch": self._capture_epoch,
                                 "sequence_scope": "local_capture_only",
                                 "exchange_completeness": "unverified"})
                    item = ("heartbeat", data, self._event_source(self.source, "native_push_status"),
                            ["Capture gap or interruption observed; absence of a gap notice does not establish exchange-feed completeness."])
                elif self._queue:
                    item = self._queue.popleft()
                else:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return None
                    self._condition.wait(remaining)
                    continue
                self._event_sequence += 1
                self._counters["emitted_events"] += 1
                kind, data, source, warnings = item
                return ProviderEvent(kind, ProviderResult(data, source, warnings), str(self._event_sequence))

    def close(self):
        with self._condition:
            if self._closed:
                return
            self._closed, self._connected = True, False
            self._queue.clear()
            self._pending_gap = None
            self._condition.notify_all()
            callback, self._on_close = self._on_close, None
        if callback is not None:
            callback()
