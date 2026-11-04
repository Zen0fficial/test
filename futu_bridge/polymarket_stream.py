"""Closeable, bounded public WebSockets with explicit continuity notices."""

import json
import queue
import random
import re
import threading
import time
from urllib.parse import urlsplit
from urllib.request import getproxies, proxy_bypass

from .polymarket import JsonTransport, invalid, utcnow
from .provider import ProviderError

URLS = {"market": "wss://ws-subscriptions-clob.polymarket.com/ws/market",
        "activity": "wss://ws-live-data.polymarket.com",
        "rtds": "wss://ws-live-data.polymarket.com",
        "sports": "wss://sports-api.polymarket.com/ws"}
_END = object()


def subscription_frames(channel, *, token_ids=None, subscriptions=None, event_slug=None, market_slug=None, symbols=None, venue=None):
    if channel in ("reference", "coinbase", "binance"):
        raise ProviderError("source_removed", "External reference feeds were removed. Use channel=market with Polymarket token_ids.", 410)
    if channel not in URLS:
        raise invalid("channel must be market, activity, rtds, or sports.")
    if symbols is not None or venue is not None:
        raise invalid("External reference venues and symbols are no longer supported.")
    if channel == "market":
        if isinstance(token_ids, str):
            token_ids = token_ids.split(",")
        if not isinstance(token_ids, (list, tuple)) or not 1 <= len(token_ids) <= 500 or any(
                not isinstance(t, str) or not re.fullmatch(r"\d{1,78}", t) for t in token_ids):
            raise invalid("market streaming requires 1..500 decimal token_ids.")
        if subscriptions or event_slug or market_slug:
            raise invalid("Market streams take token_ids only.")
        return [{"assets_ids": list(dict.fromkeys(token_ids)), "type": "market", "custom_feature_enabled": True}]
    if token_ids:
        raise invalid("token_ids applies only to market streams.")
    if channel == "sports":
        if subscriptions or event_slug or market_slug:
            raise invalid("The sports feed does not take filters.")
        return []
    if channel == "activity":
        if subscriptions or event_slug and market_slug:
            raise invalid("Activity takes at most one event_slug or market_slug.")
        filters = {k: v for k, v in (("event_slug", event_slug), ("market_slug", market_slug)) if v}
        if any(not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,300}", v) for v in filters.values()):
            raise invalid("Invalid activity slug.")
        return [{"action": "subscribe", "subscriptions": [{"topic": "activity", "type": "trades",
                    **({"filters": json.dumps(filters)} if filters else {})}]}]
    if event_slug or market_slug:
        raise invalid("Use subscription filters for this channel.")
    if not isinstance(subscriptions, list) or not 1 <= len(subscriptions) <= 64:
        raise invalid("Provide 1..64 subscription objects.")
    if len(json.dumps(subscriptions)) > 50000:
        raise invalid("Subscription frame is too large.")
    allowed = {"activity", "crypto_prices", "crypto_prices_chainlink", "crypto_prices_twap_sixty", "equity_prices"}
    for sub in subscriptions:
        if not isinstance(sub, dict) or set(sub) - {"topic", "type", "filters"} or sub.get("topic") not in allowed:
            raise invalid("Only public RTDS activity and legacy reference-price topics are allowed.")
        if not isinstance(sub.get("type"), str) or "filters" in sub and not isinstance(sub["filters"], str):
            raise invalid("RTDS requires type and optional JSON-string filters.")
    return [{"action": "subscribe", "subscriptions": subscriptions}]


class PolymarketStream:
    """Raw market events; deltas are not advertised as reconstructed full books.

    read(timeout) returns {type, data, source}, None, or raises StopIteration.
    No missed-trade replay is claimed. Reconnect and overflow emit gap events.
    """

    def __init__(self, channel="market", *, token_ids=None, subscriptions=None,
                 event_slug=None, market_slug=None, symbols=None, venue=None, queue_size=2048, connect_timeout=15,
                 connector=None, rest_transport=None):
        self.frames = subscription_frames(channel, token_ids=token_ids, subscriptions=subscriptions,
                                          event_slug=event_slug, market_slug=market_slug, symbols=symbols, venue=venue)
        if not isinstance(queue_size, int) or isinstance(queue_size, bool) or not 2 <= queue_size <= 100000:
            raise invalid("queue_size must be 2..100000.")
        if not 0 < connect_timeout <= 60:
            raise invalid("connect_timeout must be 0..60 seconds.")
        try:
            import websocket
        except ImportError as exc:
            raise ProviderError("dependency_missing", 'Install the "polymarket" extra for streaming.', 503) from exc
        self._websocket = websocket
        self._connector = connector or websocket.create_connection
        self.channel, self.url = channel, URLS[channel]
        self._rest = rest_transport or JsonTransport(timeout=3, retries=0)
        self._mode = "websocket"
        self._fallback_snapshots = 0
        self._last_fallback = None
        self._timeout = connect_timeout
        self._queue = queue.Queue(maxsize=queue_size)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._socket = None
        self._thread = None
        self._dropped = self._pending_dropped = self._reconnects = 0
        self._received = 0
        self._deferred = None
        self.on_close = None
        self.source = {"provider": "polymarket", "kind": "live_stream", "channel": channel,
                       "upstream": self.url, "connected_at": utcnow(), "lossless": False,
                       "transport": "websocket", "authentication": "none", "billing": "none",
                       "delivery": "raw snapshots and updates; no replay across reconnects"}
        if channel == "market":
            self.source["fallback"] = "Official CLOB REST book snapshots during WebSocket interruptions"
        if channel == "rtds":
            self.source["warning"] = "Legacy reference topics are deprecated and may only provide a snapshot."
        initial_failure = False
        try:
            self._connect()
        except ProviderError:
            if channel != "market":
                self.close()
                raise
            initial_failure = True
        self._thread = threading.Thread(target=self._run, args=(initial_failure,), name="polymarket-" + channel, daemon=True)
        self._thread.start()

    def _connect(self):
        options = {"timeout": self._timeout, "enable_multithread": True}
        host = urlsplit(self.url).hostname
        proxies = getproxies()
        proxy = proxies.get("https") or proxies.get("http")
        if proxy and not proxy_bypass(host):
            parsed = urlsplit(proxy if "://" in proxy else "http://" + proxy)
            if parsed.scheme in ("http", "https"):
                options.update(http_proxy_host=parsed.hostname, http_proxy_port=parsed.port or 80,
                               proxy_type="http")
                if parsed.username:
                    options["http_proxy_auth"] = (parsed.username, parsed.password or "")
        try:
            sock = self._connector(self.url, **options)
            self._socket = sock
            if self._stop.is_set():
                sock.close()
                return
            for frame in self.frames:
                sock.send(json.dumps(frame))
            sock.settimeout(1)
        except ProviderError:
            if self._socket:
                self._socket.close()
            raise
        except Exception as exc:
            if self._socket:
                self._socket.close()
            raise ProviderError("stream_unavailable", "Could not open the Polymarket WebSocket.", 502,
                                {"channel": self.channel, "error_type": type(exc).__name__}) from exc

    def _event(self, kind, data):
        return {"type": kind, "data": data, "source": {**self.source, "observed_at": utcnow()}}

    def _put(self, event):
        with self._lock:
            if self._stop.is_set():
                return
            if self._queue.full():
                # Discard the stale backlog before admitting more deltas.
                while not self._queue.empty():
                    self._queue.get_nowait()
                    self._dropped += 1
                    self._pending_dropped += 1
                if self._deferred is not None and self._deferred is not _END:
                    self._deferred = None
                    self._dropped += 1
                    self._pending_dropped += 1
            self._queue.put_nowait(event)

    def _fallback_snapshot(self):
        if self.channel != "market" or self._stop.is_set():
            return
        if self._last_fallback is not None and time.monotonic() - self._last_fallback < 2:
            return
        self._last_fallback = time.monotonic()
        tokens = self.frames[0]["assets_ids"]
        try:
            books = self._rest.request("https://clob.polymarket.com/books", method="POST",
                                       body=[{"token_id": token} for token in tokens])
            if not isinstance(books, list) or len(books) != len(tokens):
                raise ProviderError("invalid_fallback_response", "CLOB did not return every requested book.", 502)
            seen = set()
            for book in books:
                if (not isinstance(book, dict) or book.get("asset_id") not in tokens or book["asset_id"] in seen
                        or not isinstance(book.get("bids"), list) or not isinstance(book.get("asks"), list)
                        or not str(book.get("timestamp", "")).isdigit()):
                    raise ProviderError("invalid_fallback_response", "CLOB returned an invalid book snapshot.", 502)
                seen.add(book["asset_id"])
            for book in books:
                event = self._event("update", {**book, "event_type": "book"})
                event["source"].update(upstream="https://clob.polymarket.com/books", transport="rest",
                                       kind="snapshot", fallback_active=True, snapshot_only=True,
                                       delivery="Full book snapshots only; missed trades and deltas are not replayed.")
                self._put(event)
                self._fallback_snapshots += 1
        except ProviderError as exc:
            self._put(self._event("error", {"code": "fallback_unavailable", "upstream_code": exc.code,
                                           "message": "Both live transport and the free REST snapshot are unavailable; retrying."}))

    def _recover(self):
        if self._stop.is_set():
            return False
        self._mode = "rest_fallback" if self.channel == "market" else "reconnecting"
        self._put(self._event("gap", {"reason": "disconnected", "replay_available": False,
                                     "action": "Discard reconstructed books until a fresh snapshot arrives."}))
        if self._socket:
            try:
                self._socket.close(timeout=1)
            except Exception:
                pass
        attempt = 0
        while not self._stop.is_set():
            self._fallback_snapshot()
            if self._stop.wait(min(10, 0.5 * 2 ** min(attempt, 5)) + random.uniform(0, 0.25)):
                return False
            try:
                self._connect()
                if self._stop.is_set():
                    return False
                self._mode = "websocket"
                self._reconnects += 1
                self._put(self._event("reconnected", {"reconnects": self._reconnects, "replay_available": False}))
                return True
            except ProviderError:
                attempt += 1
        return False

    def _run(self, initial_failure=False):
        last_ping = last_rx = time.monotonic()
        try:
            if initial_failure:
                if not self._recover():
                    return
                last_ping = last_rx = time.monotonic()
            while not self._stop.is_set():
                try:
                    interval = 5 if self.channel in ("activity", "rtds") else 10
                    if self.channel != "sports" and time.monotonic() - last_ping >= interval:
                        self._socket.send("PING")
                        last_ping = time.monotonic()
                    try:
                        opcode, raw = self._socket.recv_data(control_frame=True)
                    except self._websocket.WebSocketTimeoutException:
                        if self.channel != "sports" and time.monotonic() - last_rx > 45:
                            raise OSError("Heartbeat expired")
                        continue
                    if opcode == self._websocket.ABNF.OPCODE_CLOSE:
                        code = int.from_bytes(raw[:2], "big") if raw and len(raw) >= 2 else 1006
                        if code in (1008, 4001, 4008):
                            self._put(self._event("error", {"code": "stream_policy_rejection", "close_code": code,
                                                           "message": "Upstream closed the subscription; review credentials or filters."}))
                            return
                        raise OSError("WebSocket closed")
                    last_rx = time.monotonic()
                    if opcode in (self._websocket.ABNF.OPCODE_PING, self._websocket.ABNF.OPCODE_PONG):
                        # websocket-client already answers protocol ping frames.
                        continue
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8")
                    # RTDS sends an empty TEXT frame after connecting. It is not
                    # the RFC6455 CLOSE frame; treating it as EOF causes a loop.
                    if raw in ("", "PONG", "pong"):
                        continue
                    if raw in ("ping", "PING"):
                        self._socket.send("pong" if raw == "ping" else "PONG")
                        continue
                    if len(raw) > 8 * 1024 * 1024:
                        raise ValueError("Oversized stream frame")
                    payload = json.loads(raw, parse_constant=lambda v: (_ for _ in ()).throw(ValueError(v)))
                    for value in payload if isinstance(payload, list) else [payload]:
                        if not isinstance(value, dict):
                            continue
                        if value.get("op") == "error" or value.get("type") == "error" or value.get("error"):
                            self._put(self._event("error", {"code": "subscription_rejected", "message": "Upstream rejected the subscription.",
                                                           "upstream": value}))
                            return
                        self._received += 1
                        # Keep the wire event name in data. SSE always uses this safe name.
                        self._put(self._event("update", value))
                except (OSError, ValueError, self._websocket.WebSocketException):
                    if not self._recover():
                        return
                    last_ping = last_rx = time.monotonic()
        finally:
            if self._socket:
                try:
                    self._socket.close(timeout=1)
                except Exception:
                    pass
            self._terminal()

    def _terminal(self):
        with self._lock:
            if self._queue.full():
                self._queue.get_nowait()
                self._dropped += 1
                self._pending_dropped += 1
            self._queue.put_nowait(_END)

    def read(self, timeout=15):
        if not 0 < timeout <= 3600:
            raise ValueError("timeout must be positive and at most 3600 seconds")
        if self._stop.is_set():
            raise StopIteration
        with self._lock:
            if self._pending_dropped:
                dropped, self._pending_dropped = self._pending_dropped, 0
                return self._event("gap", {"reason": "consumer_overflow", "dropped": dropped,
                                           "action": "Fetch a fresh order book; prior deltas are incomplete."})
            item, self._deferred = self._deferred, None
        if item is None:
            try:
                item = self._queue.get(timeout=timeout)
            except queue.Empty:
                return None
        # A producer can overflow the queue while this reader is waking up.
        # Keep the retrieved event behind the continuity notice in that race.
        with self._lock:
            if self._pending_dropped:
                self._deferred = item
                dropped, self._pending_dropped = self._pending_dropped, 0
                return self._event("gap", {"reason": "consumer_overflow", "dropped": dropped,
                                           "action": "Fetch a fresh order book; prior deltas are incomplete."})
        if item is _END:
            self.close()
            raise StopIteration
        return item

    def status(self):
        return {"closed": self._stop.is_set(), "received": self._received,
                "dropped": self._dropped, "reconnects": self._reconnects, "source": self.source,
                "mode": self._mode, "fallback_snapshots": self._fallback_snapshots}

    def close(self):
        if self._stop.is_set():
            return
        self._stop.set()
        if self._socket:
            try:
                self._socket.shutdown()
                self._socket.close(timeout=1)
            except Exception:
                pass
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=self._timeout + 1)
        self._terminal()
        if self.on_close:
            callback, self.on_close = self.on_close, None
            callback()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def __iter__(self):
        while True:
            try:
                value = self.read()
            except StopIteration:
                return
            if value is not None:
                yield value
