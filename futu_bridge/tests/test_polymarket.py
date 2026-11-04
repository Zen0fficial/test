import json
import io
import queue
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from futu_bridge.polymarket import (PolymarketClient, JsonTransport, _endpoint,
                                    next_page, page_items)
from futu_bridge.polymarket_cli import export_pages
from futu_bridge.polymarket_stream import PolymarketStream, subscription_frames
from futu_bridge.provider import ProviderError
from futu_bridge.server import MarketDataServer


class FakeTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, url, **kwargs):
        self.calls.append((url, kwargs))
        value = self.responses.pop(0) if self.responses else {"data": [], "pagination": {"next_cursor": None}}
        if isinstance(value, Exception):
            raise value
        return value


class PolymarketTests(unittest.TestCase):
    def test_preserves_all_raw_fields_and_token_ids(self):
        raw = {"markets": [{"clobTokenIds": '["123456789012345678901234567890"]',
                            "outcomes": '["A","B","C"]', "new_field": {"exact": "0.123456789"}}],
               "next_cursor": "opaque"}
        transport = FakeTransport(raw)
        with PolymarketClient(transport=transport) as client:
            result = client.markets(closed=False)
        self.assertIs(result.data, raw)
        self.assertEqual(transport.calls[0][1]["params"]["version"], ["v1", "v2"])
        self.assertEqual(result.source["api"], "gamma")
        self.assertEqual(len(result.source["payload_sha256"]), 64)

    def test_literal_route_wins_over_identifier_template(self):
        self.assertEqual(_endpoint("gamma", "/markets/keyset", "GET")["path"], "/markets/keyset")
        self.assertEqual(_endpoint("gamma", "/markets/123", "GET")["path"], "/markets/{id}")

    def test_read_allowlist_rejects_orders_auth_and_path_escape(self):
        transport = FakeTransport()
        with PolymarketClient(transport=transport) as client:
            for path, method in (("/order", "POST"), ("/orders", "POST"), ("/auth/api-key", "POST"),
                                 ("/balance-allowance/update", "GET"), ("//evil.test", "GET"),
                                 ("/markets/../order", "GET"), ("/markets/%2e%2e", "GET"),
                                 ("/book?token_id=1", "GET")):
                with self.subTest(path=path), self.assertRaises(ProviderError):
                    client.request("clob", path, method=method, body={} if method == "POST" else None)
        self.assertFalse(transport.calls)

    def test_batch_limits_and_buy_side_are_preserved(self):
        transport = FakeTransport({"1": {"BUY": "0.55"}})
        with PolymarketClient(transport=transport) as client:
            self.assertEqual(client.batch("prices", ["1"]).data["1"]["BUY"], "0.55")
            self.assertEqual(transport.calls[0][1]["body"], [{"token_id": "1", "side": "BUY"}])
            for tokens in ([], ["1"] * 501, [123], ["abc"]):
                with self.assertRaises(ProviderError):
                    client.batch("books", tokens)

    def test_bad_filters_fail_before_network(self):
        transport = FakeTransport()
        with PolymarketClient(transport=transport) as client:
            for resource, params in (("trades", {"offset": 0}), ("trades", {"market": "x"}),
                                     ("markets", {"limit": 101}), ("markets", {"offset": 0}),
                                     ("price", {"token_id": "1"}), ("trades", {"limit": float("nan")})):
                with self.subTest(resource=resource, params=params), self.assertRaises(ProviderError):
                    client.get(resource, **params)
            with self.assertRaises(ProviderError):
                client.request("data", "/v2/trades", params="not an object")
        self.assertFalse(transport.calls)

    def test_both_cursor_contracts_and_exact_terminal_page(self):
        for resource, first, last, cursor_name in (
            ("markets", {"markets": [{"id": 1}], "next_cursor": "opaque"}, {"markets": [], "next_cursor": None}, "after_cursor"),
            ("trades", {"data": [{"id": 1}], "pagination": {"next_cursor": "opaque"}},
             {"data": [], "pagination": {"next_cursor": None}}, "cursor"),
        ):
            with self.subTest(resource=resource):
                transport = FakeTransport(first, last)
                with PolymarketClient(transport=transport) as client:
                    pages = list(client.iter_pages(resource, max_pages=None))
                self.assertEqual(transport.calls[1][1]["params"][cursor_name], "opaque")
                self.assertTrue(pages[-1].source["pagination"]["complete"])
                self.assertIsNone(pages[-1].source["pagination"]["next_params"])

    def test_repeated_cursor_fails_instead_of_looping(self):
        raw = {"data": [1], "pagination": {"next_cursor": "same"}}
        with PolymarketClient(transport=FakeTransport(raw, raw)) as client:
            iterator = client.iter_pages("trades", max_pages=None)
            next(iterator)
            with self.assertRaisesRegex(ProviderError, "did not advance"):
                next(iterator)

    def test_offset_search_and_clob_end_cursors(self):
        self.assertEqual(next_page("gamma", "/tags", {"limit": 2}, [1, 2]), {"limit": 2, "offset": 2})
        self.assertEqual(next_page("gamma", "/public-search", {}, {"pagination": {"hasMore": True}}), {"page": 2})
        self.assertEqual(next_page("gamma", "/events/pagination", {},
                                   {"data": [1, 2], "pagination": {"hasMore": True}}), {"offset": 2})
        self.assertIsNone(next_page("clob", "/simplified-markets", {}, {"next_cursor": "LTE="}))
        with self.assertRaises(ProviderError):
            next_page("data", "/v2/trades", {}, {"pagination": {"has_more": True}})

    def test_export_limits_and_failure_are_explicit(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "markets.jsonl"
            with PolymarketClient(transport=FakeTransport({"markets": [1], "next_cursor": "more"})) as client:
                manifest = export_pages(client, output, resource="markets", max_pages=1)
            self.assertFalse(manifest["complete"])
            self.assertEqual(manifest["status"], "limited")
            self.assertEqual(manifest["next_params"]["after_cursor"], "more")
            self.assertEqual(json.loads(output.read_text())["data"]["markets"], [1])
            with self.assertRaises(FileExistsError):
                export_pages(PolymarketClient(), output, resource="markets")
            failed = Path(temp) / "failed.jsonl"
            with PolymarketClient(transport=FakeTransport(ProviderError("offline", "offline"))) as client:
                with self.assertRaises(ProviderError):
                    export_pages(client, failed, resource="markets")
            self.assertFalse(failed.exists())
            state = json.loads(Path(str(failed) + ".manifest.json").read_text())
            self.assertEqual(state["status"], "failed")
            self.assertFalse(state["complete"])

    def test_removed_subgraphs_never_contact_a_third_provider(self):
        transport = FakeTransport()
        with PolymarketClient(transport=transport) as client:
            for query in ("mutation { x }", "query { _meta { block { number } } }"):
                with self.assertRaises(ProviderError) as caught:
                    client.subgraph("orders", query)
                self.assertEqual(caught.exception.code, "source_removed")
        self.assertFalse(transport.calls)

    def test_client_close_prevents_requests(self):
        client = PolymarketClient(transport=FakeTransport())
        client.close()
        with self.assertRaises(ProviderError):
            client.markets()

    def test_rate_limit_retry_and_proxy_safe_error(self):
        error = HTTPError("https://data-api.polymarket.com/v2/trades", 429, "limited",
                          {"Retry-After": "0"}, io.BytesIO(b'{"error":"busy"}'))
        response = io.BytesIO(b'{"data":[]}')
        with patch("futu_bridge.polymarket.build_opener") as factory, patch("futu_bridge.polymarket.time.sleep") as sleep:
            factory.return_value.open.side_effect = [error, response]
            self.assertEqual(JsonTransport(retries=1).request("https://data-api.polymarket.com/v2/trades"), {"data": []})
            self.assertEqual(factory.return_value.open.call_count, 2)
            sleep.assert_called_once_with(0)
        error = HTTPError("https://data-api.polymarket.com/v2/trades", 429, "limited",
                          {"Retry-After": "90"}, io.BytesIO(b'{"error":"busy"}'))
        with patch("futu_bridge.polymarket.build_opener") as factory:
            factory.return_value.open.side_effect = error
            with self.assertRaises(ProviderError) as caught:
                JsonTransport().request("https://data-api.polymarket.com/v2/trades")
            self.assertEqual(caught.exception.code, "upstream_rate_limited")
            self.assertEqual(caught.exception.details["retry_after"], "90")

    def test_key_required_sources_replaced_even_with_existing_keys(self):
        transport = FakeTransport()
        with PolymarketClient(transport=transport) as client, patch.dict("os.environ", {"GOLDSKY_FEEDS_API_KEY": "fixture-secret"}):
            with self.assertRaises(ProviderError) as caught:
                client.request("goldsky", "/data/polymarket/activity", params={"limit": 2})
            self.assertEqual(caught.exception.code, "source_replaced")
            catalog = client.catalog()
            self.assertEqual(catalog["billing_policy"], "free_only")
            self.assertNotIn("goldsky", catalog["base_urls"])
            self.assertTrue(all(e["api"] != "goldsky" for e in catalog["endpoints"]))
            self.assertNotIn("fixture-secret", json.dumps(catalog))
            self.assertEqual(catalog["onchain"]["authentication"], "none")
            self.assertEqual(catalog["source_policy"]["provider_count"], 2)
            self.assertEqual(catalog["source_policy"]["primary"]["provider"], "polymarket")
            self.assertEqual(catalog["source_policy"]["backup"]["provider"], "drpc")
            self.assertEqual(catalog["onchain"]["rpc_endpoints"], ["https://polygon.drpc.org"])
            self.assertNotIn("reference", catalog["streams"])
        self.assertFalse(transport.calls)


class FakeSocket:
    def __init__(self, events=()):
        self.incoming = queue.Queue()
        for event in events:
            self.incoming.put(event)
        self.sent, self.closed = [], False

    def send(self, value):
        self.sent.append(value)

    def recv(self):
        import websocket
        try:
            return self.incoming.get(timeout=0.02)
        except queue.Empty:
            raise websocket.WebSocketTimeoutException()

    def recv_data(self, control_frame=False):
        value = self.recv()
        return (8, b"\x03\xe8") if value is None else (1, value)

    def settimeout(self, value):
        pass

    def close(self, **kwargs):
        self.closed = True
        self.incoming.put(None)

    def shutdown(self):
        self.close()


class PolymarketStreamTests(unittest.TestCase):
    def setUp(self):
        try:
            import websocket  # noqa: F401
        except ImportError:
            self.skipTest("optional WebSocket extra absent")

    def test_market_wire_protocol_and_cleanup(self):
        socket = FakeSocket(['[{"event_type":"book","asset_id":"1","bids":[],"asks":[]}]'])
        rest = FakeTransport()
        with PolymarketStream(token_ids=["1"], connector=lambda *a, **k: socket, rest_transport=rest) as stream:
            event = stream.read(timeout=1)
            self.assertEqual(event["data"]["event_type"], "book")
            self.assertEqual(json.loads(socket.sent[0])["assets_ids"], ["1"])
            self.assertEqual(event["source"]["transport"], "websocket")
            self.assertFalse(rest.calls)
        self.assertTrue(socket.closed)
        self.assertFalse(stream._thread.is_alive())
        with self.assertRaises(StopIteration):
            stream.read()

    def test_overflow_emits_gap_before_new_deltas(self):
        socket = FakeSocket()
        with PolymarketStream(token_ids=["1"], queue_size=2, connector=lambda *a, **k: socket) as stream:
            for i in range(3):
                stream._put(stream._event("update", {"n": i}))
            gap = stream.read(timeout=1)
            self.assertEqual(gap["type"], "gap")
            self.assertEqual(gap["data"]["dropped"], 2)
            self.assertEqual(stream.read(timeout=1)["data"]["n"], 2)

    def test_reconnect_reports_gap_and_resubscribes(self):
        sockets = [FakeSocket([None]), FakeSocket(['{"event_type":"book","asset_id":"1"}'])]
        rest = FakeTransport([{"asset_id": "1", "bids": [], "asks": [], "timestamp": "1234567890000"}])
        with PolymarketStream(token_ids=["1"], connector=lambda *a, **k: sockets.pop(0), rest_transport=rest) as stream:
            self.assertEqual(stream.read(timeout=2)["type"], "gap")
            fallback = stream.read(timeout=2)
            self.assertEqual(fallback["data"]["event_type"], "book")
            self.assertEqual(fallback["source"]["transport"], "rest")
            self.assertTrue(fallback["source"]["snapshot_only"])
            self.assertEqual(rest.calls[0][0], "https://clob.polymarket.com/books")
            self.assertEqual(stream.read(timeout=2)["type"], "reconnected")
            restored = stream.read(timeout=2)
            self.assertEqual(restored["data"]["event_type"], "book")
            self.assertEqual(restored["source"]["transport"], "websocket")
            self.assertEqual(stream.status()["reconnects"], 1)
            self.assertEqual(stream.status()["mode"], "websocket")

    def test_rtds_empty_text_frame_is_not_disconnect(self):
        socket = FakeSocket(["", '{"topic":"activity","payload":{"price":0.5}}'])
        with PolymarketStream("activity", connector=lambda *a, **k: socket) as stream:
            event = stream.read(timeout=1)
            self.assertEqual(event["type"], "update")
            self.assertEqual(event["data"]["topic"], "activity")
            self.assertEqual(stream.status()["reconnects"], 0)

    def test_removed_reference_sources_fail_before_connecting(self):
        for channel in ("reference", "coinbase", "binance"):
            with self.subTest(channel=channel), patch("websocket.create_connection") as connect:
                with self.assertRaises(ProviderError) as caught:
                    PolymarketStream(channel, symbols=["BTC-USD"])
                self.assertEqual(caught.exception.code, "source_removed")
                connect.assert_not_called()

    def test_initial_websocket_failure_uses_rest_then_recovers(self):
        rest = FakeTransport([{"asset_id": "1", "bids": [], "asks": [], "timestamp": "1234567890000", "hash": "raw"}])
        socket = FakeSocket(['{"event_type":"book","asset_id":"1"}'])
        with patch("websocket.create_connection", side_effect=[OSError("offline"), socket]):
            with PolymarketStream(token_ids=["1"], rest_transport=rest) as stream:
                self.assertEqual(stream.read(timeout=2)["type"], "gap")
                fallback = stream.read(timeout=2)
                self.assertTrue(fallback["source"]["fallback_active"])
                self.assertEqual(fallback["data"]["hash"], "raw")
                self.assertEqual(stream.read(timeout=2)["type"], "reconnected")
                self.assertEqual(stream.read(timeout=2)["source"]["transport"], "websocket")
                self.assertEqual(stream.status()["fallback_snapshots"], 1)

    def test_bad_or_failed_rest_backup_never_emits_fake_book(self):
        replies = [ProviderError("offline", "offline"), [],
                   [{"asset_id": "2", "bids": [], "asks": [], "timestamp": "123"}],
                   [{"asset_id": "1", "bids": [], "asks": []}]]
        for reply in replies:
            with self.subTest(reply=reply):
                socket = FakeSocket(['{"event_type":"book","asset_id":"1"}'])
                with patch("websocket.create_connection", side_effect=[OSError("offline"), socket]):
                    with PolymarketStream(token_ids=["1"], rest_transport=FakeTransport(reply)) as stream:
                        self.assertEqual(stream.read(timeout=2)["type"], "gap")
                        error = stream.read(timeout=2)
                        self.assertEqual(error["type"], "error")
                        self.assertEqual(error["data"]["code"], "fallback_unavailable")
                        self.assertEqual(stream.read(timeout=2)["type"], "reconnected")
                        self.assertEqual(stream.status()["fallback_snapshots"], 0)

    def test_invalid_or_private_streams_rejected(self):
        for channel, options in (("user", {}), ("market", {"token_ids": ["bad"]}),
                                 ("rtds", {"subscriptions": [{"topic": "clob_user", "type": "*"}]})):
            with self.assertRaises(ProviderError):
                subscription_frames(channel, **options)


class PolymarketHTTPTests(unittest.TestCase):
    def setUp(self):
        self.transport = FakeTransport()
        self.server = MarketDataServer(("127.0.0.1", 0), polymarket=PolymarketClient(transport=self.transport))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:" + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, path, body=None, method=None, headers=None):
        request = Request(self.base + path, data=json.dumps(body).encode() if body is not None else None,
                          method=method, headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urlopen(request, timeout=3) as response:
                return response.status, json.load(response)
        except HTTPError as exc:
            return exc.code, json.load(exc)

    def test_namespaced_reads_and_existing_health(self):
        status, body = self.request("/v1/polymarket/markets?closed=true&limit=2")
        self.assertEqual(status, 200)
        self.assertEqual(body["source"]["provider"], "polymarket")
        status, health = self.request("/health")
        self.assertTrue(health["read_only"])
        self.assertFalse(health["provider_connected"])
        self.assertTrue(health["resources"]["polymarket"])

    def test_read_post_accepts_batches_but_rejects_orders_and_put(self):
        status, body = self.request("/v1/polymarket/clob/books", [{"token_id": "1"}])
        self.assertEqual(status, 200)
        self.assertEqual(self.transport.calls[-1][1]["method"], "POST")
        for path, method, body in (("/v1/polymarket/clob/order", "POST", {}),
                                   ("/v1/polymarket/clob/books", "PUT", []),
                                   ("/v1/quotes", "POST", {})):
            status, _ = self.request(path, body, method)
            self.assertIn(status, (404, 405))
        self.assertEqual(len(self.transport.calls), 1)

    def test_cross_origin_and_bad_params_block_before_transport(self):
        status, _ = self.request("/v1/polymarket/markets", headers={"Origin": "https://example.com"})
        self.assertEqual(status, 403)
        status, _ = self.request("/v1/polymarket/request", {"api": "clob", "path": "/book", "params": "bad"})
        self.assertEqual(status, 400)
        status, _ = self.request("/v1/polymarket/markets?not_a_filter=1")
        self.assertEqual(status, 400)
        self.assertFalse(self.transport.calls)

    def test_onchain_http_read_routes_and_mutations_rejected(self):
        from futu_bridge.polymarket_onchain import PolygonPublicClient
        rpc = FakeTransport({"jsonrpc": "2.0", "id": 1, "result": "0x89"},
                            {"jsonrpc": "2.0", "id": 1, "result": "0x100"})
        self.server.polymarket._onchain = PolygonPublicClient(transport=rpc)
        status, body = self.request("/v1/polymarket/onchain/status")
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["block_number"], 256)
        status, body = self.request("/v1/polymarket/onchain/catalog")
        self.assertEqual(body["billing"], "none")
        status, _ = self.request("/v1/polymarket/onchain/logs?contract=ctf&topics=bad")
        self.assertEqual(status, 400)
        status, _ = self.request("/v1/polymarket/onchain/eth_sendTransaction")
        self.assertEqual(status, 400)
        status, _ = self.request("/v1/polymarket/onchain/status", {"method": "eth_sendRawTransaction"})
        self.assertEqual(status, 404)
        status, body = self.request("/v1/polymarket/goldsky/activity?limit=2")
        self.assertEqual(status, 410)
        self.assertEqual(body["error"]["code"], "source_replaced")
        self.assertEqual(len(rpc.calls), 2)

    def test_removed_reference_and_subgraph_http_routes(self):
        for path, body in (("/v1/polymarket/stream?channel=reference&symbols=BTC-USD", None),
                           ("/v1/polymarket/stream", {"channel": "reference", "symbols": ["BTC-USD"]}),
                           ("/v1/polymarket/subgraphs", None),
                           ("/v1/polymarket/subgraphs/orders", {"query": "{ _meta { block { number } } }"})):
            status, result = self.request(path, body)
            self.assertEqual(status, 410)
            self.assertEqual(result["error"]["code"], "source_removed")
        self.assertFalse(self.transport.calls)


if __name__ == "__main__":
    unittest.main()
