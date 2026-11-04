import unittest

from futu_bridge.polymarket_onchain import CONTRACTS, RPC_URLS, PolygonPublicClient
from futu_bridge.provider import ProviderError


class RPCFixture:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, url, **kwargs):
        self.calls.append((url, kwargs["body"]))
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return {"jsonrpc": "2.0", "id": 1, "result": value}


class PolygonPublicTests(unittest.TestCase):
    def test_single_backup_checks_chain_and_reuses_verification(self):
        self.assertEqual(RPC_URLS, ("https://polygon.drpc.org",))
        rpc = RPCFixture("0x89", "0x100", "0x101")
        client = PolygonPublicClient(transport=rpc)
        first = client.status()
        self.assertEqual(first.data, {"chain_id": 137, "block_number": 256})
        self.assertEqual(first.source["upstream"], RPC_URLS[0])
        self.assertEqual(first.source["provider"], "drpc")
        second = client.status()
        self.assertEqual(second.data["block_number"], 257)
        self.assertEqual(rpc.calls[-1][1]["method"], "eth_blockNumber")
        self.assertEqual(len(rpc.calls), 3)

    def test_wrong_chain_does_not_add_another_provider(self):
        rpc = RPCFixture("0x1")
        with self.assertRaises(ProviderError) as caught:
            PolygonPublicClient(transport=rpc).status()
        self.assertEqual(caught.exception.details["attempts"][0]["code"], "wrong_chain")
        self.assertEqual(len(rpc.calls), 1)

    def test_all_providers_fail_explicitly(self):
        rpc = RPCFixture(ProviderError("offline", "offline"))
        with self.assertRaises(ProviderError) as caught:
            PolygonPublicClient(transport=rpc).status()
        self.assertEqual(caught.exception.code, "public_rpc_unavailable")
        self.assertEqual(len(caught.exception.details["attempts"]), 1)

    def test_logs_pin_range_preserve_raw_fields_and_topics(self):
        raw = [{"address": CONTRACTS["ctf_exchange"], "blockNumber": "0xff", "removed": False,
                "data": "0x1234", "extra": {"unchanged": True}}]
        rpc = RPCFixture("0x89", "0x100", raw)
        result = PolygonPublicClient(transport=rpc).logs("ctf_exchange", blocks="2", topics=[None, ["0x" + "a" * 64]])
        self.assertIs(result.data, raw)
        query = rpc.calls[-1][1]["params"][0]
        self.assertEqual((query["fromBlock"], query["toBlock"]), ("0xff", "0x100"))
        self.assertEqual(query["topics"], [None, ["0x" + "a" * 64]])
        self.assertTrue(result.warnings)

    def test_bad_ranges_and_arbitrary_methods_never_reach_rpc(self):
        rpc = RPCFixture()
        client = PolygonPublicClient(transport=rpc)
        cases = [("logs", {"contract": "unknown"}), ("logs", {"contract": "ctf", "blocks": 101}),
                 ("logs", {"contract": "ctf", "from_block": 1, "to_block": 101}),
                 ("logs", {"contract": "ctf", "from_block": 10, "to_block": 9}),
                 ("logs", {"contract": "ctf", "topics": [["not-a-hash"]]}),
                 ("logs", {"contract": "ctf", "blocks": True}), ("logs", {"contract": "ctf", "blocks": 1.5}),
                 ("eth_sendTransaction", {}), ("status", {"method": "eth_sendRawTransaction"}),
                 ("balance", {"address": "0x" + "1" * 40, "token_id": "1"})]
        for resource, params in cases:
            with self.subTest(resource=resource, params=params), self.assertRaises(ProviderError):
                client.get(resource, **params)
        self.assertFalse(rpc.calls)

    def test_logs_reject_wrong_contract_or_block(self):
        for log in ({"address": CONTRACTS["positions"], "blockNumber": "0x1"},
                    {"address": CONTRACTS["ctf"], "blockNumber": "0x2"}):
            rpc = RPCFixture("0x89", [log])
            with self.assertRaises(ProviderError) as caught:
                PolygonPublicClient(transport=rpc).logs("ctf", from_block=1, to_block=1)
            self.assertEqual(caught.exception.code, "rpc_invalid_response")

    def test_balance_exact_uint256_encoding_at_explicit_block(self):
        wallet, token, balance = "0x" + "Ab" * 20, 2**255 + 12345, 2**200 + 98765
        rpc = RPCFixture("0x89", "0x" + format(balance, "064x"))
        result = PolygonPublicClient(transport=rpc).balance(wallet, str(token), contract="positions", block="0x100")
        request = rpc.calls[-1][1]
        self.assertEqual(request["method"], "eth_call")
        call, height = request["params"]
        self.assertEqual(height, "0x100")
        self.assertEqual(call["to"], CONTRACTS["positions"])
        self.assertEqual(call["data"], "0x00fdd58e" + wallet[2:].lower().zfill(64) + format(token, "064x"))
        self.assertEqual(result.data["balance_raw"], str(balance))
        self.assertEqual(result.data["token_id"], str(token))

    def test_balance_validates_wallet_and_token_system(self):
        rpc = RPCFixture()
        client = PolygonPublicClient(transport=rpc)
        for address, token, contract in (("bad", "1", "ctf"), ("0x" + "1" * 40, -1, "ctf"),
                                         ("0x" + "1" * 40, 2**256, "ctf"), ("0x" + "1" * 40, 1, "ctf_exchange")):
            with self.assertRaises(ProviderError):
                client.balance(address, token, contract=contract)
        self.assertFalse(rpc.calls)

    def test_receipt_missing_and_wrong_receipt_are_distinct(self):
        tx = "0x" + "1" * 64
        rpc = RPCFixture("0x89", None, {"transactionHash": "0x" + "2" * 64})
        client = PolygonPublicClient(transport=rpc)
        self.assertIsNone(client.receipt(tx).data)
        with self.assertRaises(ProviderError) as caught:
            client.receipt(tx)
        self.assertEqual(caught.exception.code, "rpc_invalid_response")


if __name__ == "__main__":
    unittest.main()
