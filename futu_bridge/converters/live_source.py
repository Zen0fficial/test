"""Bounded, reconnecting SSE consumer of the unchanged loopback provider API."""

from __future__ import annotations

from collections import Counter, OrderedDict
import json
from pathlib import Path
import queue
import socket
import threading
import time
import urllib.parse
import urllib.request

from .capture import write_json
from .validation import nanoseconds, number, timestamp


def local_url(base_url):
    value = urllib.parse.urlsplit(base_url)
    if (value.scheme != "http" or value.hostname not in ("127.0.0.1", "localhost", "::1")
            or value.username or value.password or value.query or value.fragment or value.path not in ("", "/")):
        raise ValueError("Use an unauthenticated loopback provider URL")
    return base_url.rstrip("/")


def hk_symbol(symbol):
    value = symbol if symbol.startswith("HK.") else "HK." + symbol
    if len(value) != 8 or not value[3:].isascii() or not value[3:].isdigit():
        raise ValueError("Expected a five-digit HK symbol, e.g. 00700 or HK.00700")
    return value


class Journal:
    """Bounded JSONL diagnostics; current file plus three older segments."""
    def __init__(self, path, max_bytes=32 * 1024 * 1024):
        self.path, self.max_bytes = Path(path), max_bytes
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.rotations = 0

    def write(self, value):
        line = json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self.lock:
            if self.path.exists() and self.path.stat().st_size + len(line.encode()) > self.max_bytes:
                for index in (3, 2, 1):
                    previous = self.path if index == 1 else self.path.with_suffix(self.path.suffix + f".{index - 1}")
                    if previous.exists():
                        previous.replace(self.path.with_suffix(self.path.suffix + f".{index}"))
                self.rotations += 1
            with self.path.open("a", encoding="utf-8") as file:
                file.write(line)


def sse_events(response, max_bytes=2 * 1024 * 1024):
    kind, identity, data, size = "message", "", [], 0
    while True:
        raw = response.readline(max_bytes + 1)
        if not raw:
            raise ConnectionError("Provider stream ended")
        size += len(raw)
        if size > max_bytes:
            raise ValueError("SSE event exceeds size limit")
        line = raw.decode("utf-8").rstrip("\r\n")
        if not line:
            if data:
                yield kind, identity, json.loads("\n".join(data))
            kind, identity, data, size = "message", "", [], 0
        elif line.startswith(":"):
            yield "keepalive", "", None
            if not data:
                size = 0
        else:
            key, _, value = line.partition(":")
            value = value.removeprefix(" ")
            if key == "event": kind = value
            elif key == "id": identity = value
            elif key == "data": data.append(value)


class LocalSSE:
    def __init__(self, base_url, symbol, output, *, queue_size=8192, disconnect_after=0):
        self.base_url, self.symbol = local_url(base_url), hk_symbol(symbol)
        self.output = Path(output)
        self.events = queue.Queue(maxsize=queue_size)
        self.raw = Journal(self.output / "raw.jsonl")
        self.status_log = Journal(self.output / "transport.jsonl")
        self.counts = Counter()
        self.stop_event = threading.Event()
        self.response = None
        self.thread = None
        self.connected = False
        self.disconnect_after = disconnect_after
        self.started_ns = time.time_ns()
        self.last_received_ns = None
        self.sequence = 0

    def start(self):
        self.thread = threading.Thread(target=self._run, name="futu-converter-sse", daemon=True)
        self.thread.start()

    def _status(self, state, **details):
        self.status_log.write({"at_ns": time.time_ns(), "state": state, **details})
        write_json(self.output / "transport_status.json", self.status())

    def _run(self):
        retry = 0
        forced = False
        while not self.stop_event.is_set():
            try:
                url = self.base_url + "/v1/stream?" + urllib.parse.urlencode({"symbols": self.symbol, "types": "quotes,trades,orderbook"})
                self.counts["http_stream_requests"] += 1
                request = urllib.request.Request(url, headers={"Accept": "text/event-stream"})
                with urllib.request.urlopen(request, timeout=20) as response:
                    self.response = response
                    if response.headers.get_content_type() != "text/event-stream":
                        raise ValueError("Expected an SSE response")
                    self.counts["connections"] += 1
                    connection = self.counts["connections"]
                    connected_at = time.monotonic()
                    self.connected = True
                    self._status("connected", connection=connection, unknown_gap=connection > 1)
                    for kind, identity, body in sse_events(response):
                        if self.stop_event.is_set(): break
                        received = time.time_ns()
                        self.last_received_ns = received
                        self.counts[kind] += 1
                        if kind == "keepalive": continue
                        if kind == "ready":
                            source = body.get("source", {})
                            if source.get("provider") != "futu-desktop-session" or source.get("live_push") is not True:
                                raise ValueError("The converter requires the existing native Futu push provider")
                        if kind in ("error", "end"):
                            raise ConnectionError("Provider reported " + kind)
                        self.sequence += 1
                        envelope = {"sequence": self.sequence, "connection": connection, "received_ns": received,
                                    "type": kind, "sse_id": identity, "body": body}
                        self.raw.write(envelope)
                        if kind == "heartbeat":
                            state = body.get("data", {})
                            if state.get("event") == "gap" or state.get("status") in ("disconnected", "closed"):
                                self.counts["provider_gaps"] += 1
                                raise ConnectionError("Provider reported a delivery gap or disconnection")
                        if kind in ("quotes", "trades", "orderbook", "heartbeat"):
                            try:
                                self.events.put_nowait(envelope)
                            except queue.Full as exc:
                                self.counts["queue_overflows"] += 1
                                raise ConnectionError("Consumer queue overflow; continuity is unknown") from exc
                        if time.monotonic() - connected_at > 10: retry = 0
                        if self.disconnect_after and not forced and time.monotonic() - connected_at >= self.disconnect_after:
                            forced = True
                            self.counts["forced_disconnects"] += 1
                            raise ConnectionError("Converter-only disconnect test")
            except Exception as exc:
                if not self.stop_event.is_set():
                    self.counts["disconnects"] += 1
                    self.connected = False
                    self._status("disconnected", reason=str(exc), unknown_gap=True, exchange_replay_available=False)
                    self.stop_event.wait(min(10, 0.5 * (2 ** min(retry, 5))))
                    retry += 1
            finally:
                self.response = None
                self.connected = False
        self._status("stopped")

    def status(self):
        return {"symbol": self.symbol, "connected": self.connected, "started_ns": self.started_ns,
                "last_received_ns": self.last_received_ns, "queue_size": self.events.qsize(),
                "counts": dict(self.counts), "raw_log_rotations": self.raw.rotations,
                "exchange_lossless": False, "replay_on_reconnect": False}

    def close(self):
        self.stop_event.set()
        response = self.response
        if response is not None:
            # Interrupt this converter's pending socket read, without touching
            # the provider process or any other subscriber.
            try:
                response.fp.raw._sock.shutdown(socket.SHUT_RDWR)
            except (AttributeError, OSError):
                pass
        if self.thread:
            self.thread.join(timeout=25)
            if self.thread.is_alive():
                raise RuntimeError("Converter SSE reader did not stop")


class TickSelector:
    """Nautilus-side admission policy; full provider responses stay in raw logs."""
    def __init__(self, symbol, max_seen=100000):
        self.symbol, self.max_seen = hk_symbol(symbol), max_seen
        self.trades = OrderedDict()
        self.counts = Counter()

    def select(self, envelope):
        body, kind = envelope["body"], envelope["type"]
        if kind == "heartbeat":
            self.counts["provider_status_events"] += 1
            return []
        data, source = body.get("data", {}), body.get("source", {})
        if source.get("provider") != "futu-desktop-session" or source.get("live_push") is not True:
            raise ValueError("Unverified live provider origin")
        if data.get("symbol") != self.symbol:
            raise ValueError("Unexpected stream symbol")
        if data.get("initial_snapshot") or data.get("backfill"):
            self.counts["bootstrap_skipped"] += 1
            return []
        prefix = str(envelope["sequence"])
        common = {"connection": envelope["connection"], "received_ns": envelope["received_ns"], "symbol": self.symbol}
        if kind == "trades":
            result = []
            for row in data["trades"]:
                identity = row["trade_id"]
                if not isinstance(identity, str) or not identity:
                    raise ValueError("Trade IDs must be exact strings")
                event_ns = int(number(row["exchange_timestamp_ms"])) * 1_000_000
                price, size = number(row["price"]), number(row["size_decimal"])
                if price <= 0 or size <= 0: raise ValueError("Invalid trade price/size")
                signature = (event_ns, price, size)
                if identity in self.trades:
                    if self.trades[identity] != signature:
                        self.counts["trade_revisions"] += 1
                    else: self.counts["duplicate_trades"] += 1
                    continue
                self.trades[identity] = signature
                if len(self.trades) > self.max_seen:
                    self.trades.popitem(last=False)
                    self.counts["dedupe_evictions"] += 1
                result.append({**common, "record_id": prefix + ":trade:" + identity, "kind": "trade",
                               "trade_id": identity, "event_ns": event_ns, "timestamp_basis": "exchange",
                               "price": str(price), "size": str(size)})
            return result
        if kind == "orderbook" and data.get("book_kind") == "top_of_book":
            if not data.get("bids") or not data.get("asks") or not data.get("push", {}).get("state_complete"):
                self.counts["incomplete_books"] += 1
                return []
            bid, ask = data["bids"][0], data["asks"][0]
            bp, ap = number(bid["price"]), number(ask["price"])
            bs, ass = number(bid["volume"]), number(ask["volume"])
            if bp <= 0 or ap < bp or min(bs, ass) < 0: raise ValueError("Invalid top-of-book")
            return [{**common, "record_id": prefix + ":quote", "kind": "quote",
                     "event_ns": nanoseconds(timestamp(data["observed_at"])), "timestamp_basis": "provider_observed",
                     "bid": str(bp), "ask": str(ap), "bid_size": str(bs), "ask_size": str(ass)}]
        self.counts["archived_only_" + kind] += 1
        return []
