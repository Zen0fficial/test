"""Synthetic transport/admission regressions, separate from real HK live runs."""

import copy
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from futu_bridge.converters.live_source import Journal, LocalSSE, TickSelector, hk_symbol, local_url, sse_events
from futu_bridge.converters.live_verify import iso_ns


def trade(identity="900719925474099312345", price="426.2"):
    return {"sequence": 5, "connection": 1, "received_ns": 1793856643772915300, "type": "trades", "body": {
        "source": {"provider": "futu-desktop-session", "live_push": True},
        "data": {"symbol": "HK.00700", "initial_snapshot": False, "trades": [
            {"trade_id": identity, "price": price, "size_decimal": "100", "exchange_timestamp_ms": 1793856643123}]}}}


def book():
    envelope = trade()
    envelope["type"] = "orderbook"
    envelope["body"]["data"] = {"symbol": "HK.00700", "book_kind": "top_of_book", "observed_at": "2026-11-05T05:30:43.772915Z",
        "push": {"state_complete": True}, "bids": [{"price": "426.2", "volume": "1000"}],
        "asks": [{"price": "426.4", "volume": "900"}]}
    return envelope


class LiveConverterTests(unittest.TestCase):
    def test_symbol_keeps_leading_zeros(self):
        self.assertEqual(hk_symbol("00700"), "HK.00700")
        self.assertEqual(hk_symbol("HK.00700"), "HK.00700")
        for bad in ("700", "US.00700", "００７００", "../00700"):
            with self.assertRaises(ValueError): hk_symbol(bad)

    def test_loopback_only(self):
        self.assertEqual(local_url("http://127.0.0.1:8765/"), "http://127.0.0.1:8765")
        for bad in ("https://example.com", "http://user:secret@localhost", "http://localhost/data", "http://localhost?x=1"):
            with self.assertRaises(ValueError): local_url(bad)

    def test_sse_multiline_unicode_and_heartbeat(self):
        raw = ': alive\r\nevent: trades\r\nid: abc\r\ndata: {"name":\r\ndata: "腾讯"}\r\n\r\n'.encode()
        events = sse_events(io.BytesIO(raw))
        self.assertEqual(next(events), ("keepalive", "", None))
        self.assertEqual(next(events), ("trades", "abc", {"name": "腾讯"}))
        with self.assertRaises(ConnectionError): next(events)

    def test_sse_malformed_and_oversize(self):
        with self.assertRaises(ValueError): next(sse_events(io.BytesIO(b'data: bad\n\n')))
        with self.assertRaises(ValueError): next(sse_events(io.BytesIO(b'data: '+b'a'*100+b'\n'), max_bytes=30))

    def test_bootstrap_and_backfill_are_not_live_ticks(self):
        for key in ("initial_snapshot", "backfill"):
            value = trade(); value["body"]["data"][key] = True
            self.assertEqual(TickSelector("00700").select(value), [])

    def test_exact_trade_identity_and_cross_connection_deduplication(self):
        selector = TickSelector("00700")
        value = trade()
        selected = selector.select(value)[0]
        self.assertEqual(selected["trade_id"], "900719925474099312345")
        self.assertEqual(selected["event_ns"], 1793856643123000000)
        value["connection"] = 2
        self.assertEqual(selector.select(value), [])
        self.assertEqual(selector.counts["duplicate_trades"], 1)

    def test_trade_revision_is_reported_not_replayed_as_a_new_fill(self):
        selector = TickSelector("00700")
        selector.select(trade())
        self.assertEqual(selector.select(trade(price="427")), [])
        self.assertEqual(selector.counts["trade_revisions"], 1)

    def test_deduplication_memory_is_bounded(self):
        selector = TickSelector("00700", max_seen=2)
        for identity in ("1", "2", "3"): selector.select(trade(identity))
        self.assertEqual(len(selector.trades), 2)
        self.assertEqual(selector.counts["dedupe_evictions"], 1)

    def test_book_preserves_utc_microseconds_and_timestamp_basis(self):
        row = TickSelector("00700").select(book())[0]
        self.assertEqual(row["event_ns"], 1793856643772915000)
        self.assertEqual(row["timestamp_basis"], "provider_observed")
        self.assertEqual(iso_ns("2026-11-05T13:30:43.772915+08:00"), row["event_ns"])

    def test_incomplete_depth_and_last_price_do_not_fabricate_native_ticks(self):
        for case in ("incomplete", "depth", "last"):
            value = book()
            if case == "incomplete": value["body"]["data"]["asks"] = []
            if case == "depth": value["body"]["data"]["book_kind"] = "depth"
            if case == "last": value["type"] = "quotes"
            self.assertEqual(TickSelector("00700").select(value), [])

    def test_invalid_origin_symbol_and_number_fail(self):
        for case in ("origin", "symbol", "number"):
            value = trade()
            if case == "origin": value["body"]["source"]["live_push"] = False
            if case == "symbol": value["body"]["data"]["symbol"] = "US.AAPL"
            if case == "number": value["body"]["data"]["trades"][0]["price"] = "NaN"
            with self.assertRaises(ValueError): TickSelector("00700").select(value)

    def test_journal_rotates_with_fixed_retention(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = Journal(Path(directory) / "events.jsonl", max_bytes=20)
            for index in range(12): journal.write({"index": index, "text": "test"})
            self.assertLessEqual(len(list(Path(directory).iterdir())), 4)
            self.assertGreater(journal.rotations, 0)

    def test_transport_retries_503_and_eof_then_stops(self):
        class Handler(BaseHTTPRequestHandler):
            calls = 0
            def log_message(self, *args): pass
            def do_GET(self):
                type(self).calls += 1
                if self.calls == 1:
                    self.send_error(503); return
                self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
                ready = {"source": {"provider": "futu-desktop-session", "live_push": True}}
                self.wfile.write(('event: ready\ndata: '+json.dumps(ready)+'\n\nevent: trades\ndata: '+json.dumps(trade()["body"])+'\n\n').encode())
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                reader = LocalSSE(f"http://127.0.0.1:{server.server_port}", "00700", directory)
                reader.start()
                try:
                    event = reader.events.get(timeout=5)
                    self.assertEqual(event["type"], "trades")
                    self.assertGreaterEqual(reader.counts["http_stream_requests"], 2)
                    self.assertGreaterEqual(reader.counts["disconnects"], 1)
                finally: reader.close()
                self.assertFalse(reader.thread.is_alive())
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)


if __name__ == "__main__": unittest.main()
