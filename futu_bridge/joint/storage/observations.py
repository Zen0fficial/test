"""Exact typed probability, trade and book observations plus lossless original JSON.

Historical probabilities have unknown historical receipt/availability times. Live
books are observed price-level snapshots, never fictitious exchange order deltas.
The raw archive retains every supplied field, including ones not mapped for ML.
"""

from __future__ import annotations

from decimal import Decimal
import hashlib
import json

import pyarrow as pa

from .captures import timestamp
from .incremental import VersionContract

DECIMAL = pa.decimal128(38, 9)
PROBABILITY_SCHEMA = pa.schema([
    ("token_id", pa.string()), ("event_ns", pa.int64()), ("probability", DECIMAL),
    ("resolution_seconds", pa.int64()), ("observed_ns", pa.int64()), ("available_ns", pa.int64()),
    ("source_sha256", pa.string()),
], metadata={b"joint_schema": b"polymarket_probability_v1", b"availability": b"unknown_historical_availability"})
LEVEL = pa.struct([("level", pa.int32()), ("price", DECIMAL), ("quantity", DECIMAL),
                   ("order_count", pa.int64())])
OBSERVATION_SCHEMA = pa.schema([
    ("message_id", pa.string()), ("session_id", pa.string()), ("sequence", pa.int64()),
    ("kind", pa.string()), ("symbol", pa.string()), ("event_ns", pa.int64()), ("received_ns", pa.int64()),
    ("observed_ns", pa.int64()), ("initial_snapshot", pa.bool_()), ("book_kind", pa.string()),
    ("bids", pa.list_(pa.field("element", LEVEL))), ("asks", pa.list_(pa.field("element", LEVEL))),
    ("trade_id", pa.string()),
    ("price", DECIMAL), ("quantity", DECIMAL), ("payload_json", pa.large_string()),
    ("source_sha256", pa.string()), ("quality_flags", pa.list_(pa.field("element", pa.string()))),
], metadata={b"joint_schema": b"futu_observation_v2", b"exchange_lossless": b"false",
             b"payload_format": b"canonical_json_v1"})
PROBABILITY_VERSIONS = VersionContract(("token_id", "event_ns"), "observed_ns",
                                       ("probability", "resolution_seconds"))
OBSERVATION_VERSIONS = VersionContract(("message_id",), "received_ns", ("payload_json",))


def probability_table(payload: bytes) -> pa.Table:
    """Retain all genuine historical probability samples with exact string token IDs."""
    value = json.loads(payload, parse_float=Decimal)
    source = value["source"]
    if source.get("provider") != "polymarket" or source.get("kind") != "historical":
        raise ValueError("Expected fixed-provider Polymarket historical data")
    token = source["request_params"]["token_id"]
    if not isinstance(token, str) or not token.isdigit():
        raise ValueError("Token IDs must remain exact decimal strings")
    digest, observed = hashlib.sha256(payload).hexdigest(), timestamp(source["observed_at"])
    rows, previous = [], None
    for item in value["data"]["data"]:
        event = int(item["timestamp"]) * 1_000_000_000
        price = Decimal(str(item["price"]))
        if not price.is_finite() or not 0 <= price <= 1 or (previous is not None and event <= previous):
            raise ValueError("Probability range or timestamp order is invalid")
        rows.append({"token_id": token, "event_ns": event, "probability": price,
                     "resolution_seconds": int(item["resolution_seconds"]), "observed_ns": observed,
                     "available_ns": None, "source_sha256": digest})
        previous = event
    if not rows:
        raise ValueError("Empty history cannot certify probability ingestion")
    return pa.Table.from_pylist(rows, schema=PROBABILITY_SCHEMA)


def levels(values: list[dict[str, object]], side: str) -> list[dict[str, object]]:
    """Preserve ordered levels and precise price/quantity, rejecting malformed books."""
    result, previous = [], None
    for row in values:
        price, size = Decimal(str(row["price"])), Decimal(str(row["volume"]))
        if (not price.is_finite() or not size.is_finite() or price <= 0 or size < 0
                or (previous is not None and (price >= previous if side == "bids" else price <= previous))):
            raise ValueError("Book level price/size/order is invalid")
        result.append({"level": int(row["level"]), "price": price, "quantity": size,
                       "order_count": row.get("order_count")})
        previous = price
    return result


def observation_table(payload: bytes) -> pa.Table:
    """Normalize captured SSE records while retaining all messages and original field payloads."""
    rows, digest = [], hashlib.sha256(payload).hexdigest()
    for raw in payload.splitlines():
        envelope = json.loads(raw)
        kind, body = envelope["type"], envelope["body"]
        source, data = body.get("source", {}), body.get("data", {})
        if source.get("provider") != "futu-desktop-session":
            raise ValueError("Unverified stream provider")
        session = str(data.get("capture_epoch") or source.get("capture_epoch")
                      or source.get("subscription", {}).get("capture_epoch") or body.get("request_id", ""))
        if not session:
            raise ValueError("Missing source session identity")
        sequence = int(envelope["sequence"])
        identity = f"{session}:{envelope['connection']}:{sequence}"
        common = {"message_id": identity, "session_id": session, "sequence": sequence,
                  "kind": kind, "symbol": data.get("symbol"), "received_ns": int(envelope["received_ns"]),
                  "observed_ns": timestamp(source["observed_at"]), "event_ns": None,
                  "initial_snapshot": bool(data.get("initial_snapshot")), "book_kind": data.get("book_kind"),
                  "bids": None, "asks": None, "trade_id": None, "price": None, "quantity": None,
                  "payload_json": json.dumps(envelope, sort_keys=True, separators=(",", ":"),
                                              ensure_ascii=False, allow_nan=False), "source_sha256": digest,
                  "quality_flags": ["exchange_sequence_unverified", "gaps_possible"]}
        if kind == "orderbook":
            common.update(bids=levels(data["bids"], "bids"), asks=levels(data["asks"], "asks"))
            common["quality_flags"].append("observed_levels_not_order_deltas")
            if common["bids"] and common["asks"] and common["bids"][0]["price"] > common["asks"][0]["price"]:
                raise ValueError("Crossed captured book")
        if kind == "trades":
            for index, trade in enumerate(data["trades"]):
                if not isinstance(trade["trade_id"], str) or not trade["trade_id"]:
                    raise ValueError("Trade identity must be a nonempty exact string")
                price, size = Decimal(str(trade["price"])), Decimal(trade["size_decimal"])
                if not price.is_finite() or not size.is_finite() or price <= 0 or size <= 0:
                    raise ValueError("Invalid observed trade price/quantity")
                rows.append({**common, "message_id": f"{identity}:{index}", "trade_id": trade["trade_id"],
                             "event_ns": int(trade["exchange_timestamp_ms"]) * 1_000_000,
                             "price": price, "quantity": size})
        else:
            rows.append(common)
    return pa.Table.from_pylist(rows, schema=OBSERVATION_SCHEMA)
