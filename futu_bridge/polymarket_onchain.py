"""Free, keyless Polygon reads for Polymarket's current contracts.

Public RPC is bounded, rate-limited access to raw chain data, not an indexed
replacement for all GraphQL history. No transaction submission method exists.
"""

import re
import threading
from dataclasses import replace

from .client import MarketDataResponse
from .polymarket import JsonTransport, invalid, utcnow
from .provider import ProviderError

RPC_URLS = ("https://polygon.drpc.org",)
CONTRACTS = {
    "ctf": "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045",
    "ctf_exchange": "0xE111180000d2663C0091e4f400237545B87B996B",
    "neg_risk_exchange": "0xe2222d279d744050d28e00520010520000310F59",
    "neg_risk_adapter": "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296",
    "positions": "0x006F54F7f9A22e0000CC2AB60031000000ae9fEF",
    "exchange_v3": "0xe3333700cA9d93003F00f0F71f8515005F6c00Aa",
    "binary_module": "0x1000008dD9001B968442c1000017eaE6E0dA00Ba",
    "neg_risk_module": "0x200000900045e3B6259600682756002200028933",
    "combinatorial_module": "0x30000034706C7d8e12009DAB006Be20000c031A8",
    "oracle": "0x0A0a0A0A8B00C51b7D810501b03F230028C04a87",
    "auto_redeemer": "0xa1200000d0002264C9a1698e001292D00E1b00af",
}


def catalog():
    return {"provider": "drpc", "role": "onchain_backup", "chain_id": 137, "rpc_endpoints": list(RPC_URLS), "contracts": dict(CONTRACTS),
            "authentication": "none", "billing": "none", "read_only": True,
            "resources": ["status", "logs", "balance", "receipt"], "max_blocks_per_read": 100,
            "sources": ["https://docs.polygon.technology/pos/reference/rpc-endpoints",
                        "https://docs.polymarket.com/resources/contracts"],
            "contracts_checked_at": "2026-11-04",
            "limitations": ["Public RPC rate limits and historical-state availability vary.",
                            "Log reads require an explicit contract and bounded block range.",
                            "Explicit onchain reads only; this cannot replace metadata, order books or indexed history.",
                            "Raw logs need ABI decoding; this is not an indexed GraphQL service.",
                            "Recent blocks can be reorganized; retain block hashes and removed flags."]}


def _integer(value, name, *, lower=0, upper=2**64 - 1):
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise invalid(name + " must be an integer.")
    try:
        number = int(value, 16 if value.startswith("0x") else 10) if isinstance(value, str) else value
    except ValueError as exc:
        raise invalid(name + " must be a decimal or hex integer.") from exc
    if not lower <= number <= upper:
        raise invalid(name + " is outside the supported range.")
    return number


def _quantity(value):
    if not isinstance(value, str) or not re.fullmatch(r"0x[0-9a-fA-F]+", value):
        raise ProviderError("rpc_invalid_response", "RPC returned an invalid hex value.", 502)
    return int(value, 16)


def _contract(name):
    if not isinstance(name, str) or name not in CONTRACTS:
        raise invalid("Select a contract name from /v1/polymarket/onchain/catalog.")
    return CONTRACTS[name]


class PolygonPublicClient:
    def __init__(self, *, transport=None):
        self.transport = transport or JsonTransport(timeout=12, retries=0)
        self._verified = set()
        self._lock = threading.Lock()

    def _send(self, endpoint, method, params):
        reply = self.transport.request(endpoint, method="POST",
                                       body={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        if not isinstance(reply, dict) or reply.get("id") != 1 or reply.get("jsonrpc") != "2.0":
            raise ProviderError("rpc_invalid_response", "RPC response envelope was invalid.", 502)
        if reply.get("error"):
            error = reply["error"]
            if not isinstance(error, dict):
                raise ProviderError("rpc_invalid_response", "RPC error envelope was invalid.", 502)
            raise ProviderError("rpc_error", "Public RPC rejected the read.", 502,
                                {"rpc_code": error.get("code"), "rpc_message": str(error.get("message", ""))[:300]})
        if "result" not in reply:
            raise ProviderError("rpc_invalid_response", "RPC response has no result.", 502)
        return reply["result"]

    def _read(self, method, params):
        failures = []
        for endpoint in RPC_URLS:
            try:
                with self._lock:
                    verified = endpoint in self._verified
                if not verified:
                    chain = self._send(endpoint, "eth_chainId", [])
                    if _quantity(chain) != 137:
                        raise ProviderError("wrong_chain", "RPC does not serve Polygon mainnet.", 502)
                    with self._lock:
                        self._verified.add(endpoint)
                value = self._send(endpoint, method, params)
                source = {"provider": "drpc", "role": "onchain_backup", "resource": "polymarket_onchain", "chain_id": 137,
                          "upstream": endpoint, "method": method, "observed_at": utcnow(), "read_only": True,
                          "authentication": "none", "billing": "none", "failed_endpoints": failures}
                return MarketDataResponse(value, source)
            except ProviderError as exc:
                failures.append({"endpoint": endpoint, "code": exc.code, "details": exc.details})
        raise ProviderError("public_rpc_unavailable", "Free public RPC reads failed. Retry later or narrow the block range.",
                            502, {"attempts": failures})

    def status(self):
        result = self._read("eth_blockNumber", [])
        return replace(result, data={"chain_id": 137, "block_number": _quantity(result.data)})

    def logs(self, contract, *, from_block=None, to_block=None, blocks=5, topics=None):
        address = _contract(contract)
        blocks = _integer(blocks, "blocks", lower=1, upper=100)
        if topics is not None:
            if not isinstance(topics, list) or len(topics) > 4:
                raise invalid("topics must be an array with up to four positions.")
            for topic in topics:
                values = topic if isinstance(topic, list) else [topic]
                if not 1 <= len(values) <= 20 or any(v is not None and (not isinstance(v, str) or
                        not re.fullmatch(r"0x[0-9a-fA-F]{64}", v)) for v in values):
                    raise invalid("A topic must be null, a 32-byte hex string, or a bounded OR array.")
        if from_block is not None:
            start = _integer(from_block, "from_block")
        else:
            start = None
        end = _integer(to_block, "to_block") if to_block is not None else self.status().data["block_number"]
        start = max(0, end - blocks + 1) if start is None else start
        if end < start or end - start + 1 > 100:
            raise invalid("Log requests must span 1..100 blocks; chunk longer ranges explicitly.")
        query = {"address": address, "fromBlock": hex(start), "toBlock": hex(end)}
        if topics is not None:
            query["topics"] = topics
        result = self._read("eth_getLogs", [query])
        if not isinstance(result.data, list):
            raise ProviderError("rpc_invalid_response", "RPC logs response was not an array.", 502)
        for log in result.data:
            if not isinstance(log, dict) or str(log.get("address", "")).lower() != address.lower() or not start <= _quantity(log.get("blockNumber")) <= end:
                raise ProviderError("rpc_invalid_response", "RPC returned logs outside the requested contract/range.", 502)
        result.source.update(contract=contract, contract_address=address, from_block=start, to_block=end)
        result.warnings.append("Raw logs for this range only. Recent blocks may reorganize; this is not complete market history.")
        return result

    def balance(self, address, token_id, *, contract, block=None):
        if contract not in ("ctf", "positions"):
            raise invalid("Choose contract=ctf (V1) or contract=positions (protocol V2) explicitly.")
        if not isinstance(address, str) or not re.fullmatch(r"0x[0-9a-fA-F]{40}", address):
            raise invalid("address must be a 20-byte wallet address.")
        token = _integer(token_id, "token_id", upper=2**256 - 1)
        height = _integer(block, "block") if block is not None else self.status().data["block_number"]
        # ERC-1155 balanceOf(address,uint256). eth_call executes a read at a pinned
        # block and never sends a transaction or incurs a chain gas payment.
        data = "0x00fdd58e" + address[2:].lower().zfill(64) + format(token, "064x")
        result = self._read("eth_call", [{"to": CONTRACTS[contract], "data": data}, hex(height)])
        if not isinstance(result.data, str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", result.data):
            raise ProviderError("rpc_invalid_response", "ERC-1155 balance read did not return a uint256.", 502)
        result = replace(result, data={"address": address, "token_id": str(token), "contract": contract,
                       "contract_address": CONTRACTS[contract], "block_number": height,
                       "balance_raw": str(int(result.data, 16))})
        result.source.update(block_number=height, contract_address=CONTRACTS[contract])
        result.warnings.append("Raw ERC-1155 units for the explicitly selected token system. This does not enumerate a wallet's tokens.")
        return result

    def receipt(self, transaction_hash):
        if not isinstance(transaction_hash, str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", transaction_hash):
            raise invalid("transaction_hash must be a 32-byte hex hash.")
        result = self._read("eth_getTransactionReceipt", [transaction_hash])
        if result.data is not None and (not isinstance(result.data, dict) or str(result.data.get("transactionHash", "")).lower() != transaction_hash.lower()):
            raise ProviderError("rpc_invalid_response", "RPC returned a different transaction receipt.", 502)
        return result

    def get(self, resource, **params):
        methods = {"status": self.status, "logs": self.logs, "balance": self.balance, "receipt": self.receipt}
        if resource not in methods:
            raise invalid("Unknown on-chain resource.")
        # Reject unknown/missing arguments before running any network request.
        import inspect
        try:
            inspect.signature(methods[resource]).bind(**params)
        except TypeError as exc:
            raise invalid(str(exc)) from exc
        return methods[resource](**params)
