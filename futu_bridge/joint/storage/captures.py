"""Import original real Futu captures and observed Polymarket metadata into typed snapshots.

Historical bar labels remain source labels. This importer does not claim original
tick receipt times or infer when a historical daily candle became available.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from decimal import Decimal
import hashlib
import json
from pathlib import Path

import pandas as pd
import pyarrow as pa

from .snapshots import Snapshot, SnapshotStore

BAR_SCHEMA = pa.schema([
    pa.field("symbol", pa.string(), nullable=False), pa.field("timeframe", pa.string(), nullable=False),
    pa.field("event_ns", pa.int64(), nullable=False), pa.field("trading_date", pa.string(), nullable=False),
    *[pa.field(name, pa.decimal128(38, 9), nullable=False) for name in ("open", "high", "low", "close", "volume")],
    pa.field("turnover_raw", pa.string()), pa.field("available_ns", pa.int64()),
    pa.field("capture_observed_ns", pa.int64(), nullable=False),
    pa.field("source_row", pa.int64(), nullable=False), pa.field("source_sha256", pa.string(), nullable=False),
], metadata={b"joint_schema": b"futu_source_bars_v1", b"availability": b"unknown_historical_availability"})

EVENT_SCHEMA = pa.schema([
    pa.field("event_id", pa.string(), nullable=False), pa.field("slug", pa.string(), nullable=False),
    pa.field("title", pa.string(), nullable=False), pa.field("description", pa.large_string(), nullable=False),
    pa.field("observed_ns", pa.int64(), nullable=False), pa.field("payload_json", pa.large_string(), nullable=False),
    pa.field("source_sha256", pa.string(), nullable=False),
], metadata={b"joint_schema": b"polymarket_observed_events_v1"})


def timestamp(value: str) -> int:
    """Require explicit source timezone and retain integer UTC nanoseconds."""
    result = pd.Timestamp(value)
    if pd.isna(result) or result.tzinfo is None:
        raise ValueError("Capture timestamps must declare their timezone")
    return int(result.value)


def futu_table(payload: bytes) -> pa.Table:
    """Preserve all source bars and exact decimal values, without applying the RL session filter."""
    envelope = json.loads(payload, parse_float=Decimal)
    source, data = envelope["source"], envelope["data"]
    if (source.get("provider") != "futu-desktop-session" or source.get("kind") != "historical"
            or not data.get("complete_requested_range") or data.get("truncated")
            or data.get("timeframe") not in ("1m", "1d") or data.get("adjustment") != "none"):
        raise ValueError("Expected a complete unadjusted real Futu minute/daily capture")
    rows, previous = [], None
    digest = hashlib.sha256(payload).hexdigest()
    observed = timestamp(source["observed_at"])
    for index, bar in enumerate(data["bars"]):
        event = timestamp(bar["timestamp"])
        prices = {key: Decimal(str(bar[key])) for key in ("open", "high", "low", "close", "volume")}
        if (bar.get("symbol", data["symbol"]) != data["symbol"] or (previous is not None and event <= previous)
                or not all(v.is_finite() for v in prices.values()) or prices["volume"] < 0
                or min(prices[k] for k in ("open", "high", "low", "close")) <= 0
                or prices["low"] > min(prices["open"], prices["close"])
                or prices["high"] < max(prices["open"], prices["close"])):
            raise ValueError("Invalid source identity, ordering or OHLCV")
        rows.append({"symbol": data["symbol"], "timeframe": data["timeframe"], "event_ns": event,
                     "trading_date": bar["date"], **prices,
                     "turnover_raw": str(bar["turnover_raw"]) if bar.get("turnover_raw") is not None else None,
                     "available_ns": None, "capture_observed_ns": observed, "source_row": index,
                     "source_sha256": digest})
        previous = event
    if not rows:
        raise ValueError("An empty capture cannot certify market-data ingestion")
    return pa.Table.from_pylist(rows, schema=BAR_SCHEMA)


def event_table(payload: bytes) -> pa.Table:
    """Retain observed event text and full metadata without backdating knowledge to event creation."""
    envelope = json.loads(payload)
    source = envelope["source"]
    if source.get("provider") != "polymarket" or source.get("kind") != "metadata":
        raise ValueError("Expected real Polymarket metadata from the fixed provider")
    events = envelope["data"]["events"]
    digest, observed = hashlib.sha256(payload).hexdigest(), timestamp(source["observed_at"])
    rows = [{"event_id": str(event["id"]), "slug": event["slug"], "title": event["title"],
             "description": event.get("description", ""), "observed_ns": observed,
             "payload_json": json.dumps(event, ensure_ascii=False, allow_nan=False), "source_sha256": digest}
            for event in events]
    if not rows or len({row["event_id"] for row in rows}) != len(rows):
        raise ValueError("Empty or duplicate event metadata")
    return pa.Table.from_pylist(rows, schema=EVENT_SCHEMA)


def ingest(store: SnapshotStore, source: Path, dataset: str, kind: str, *,
           expected_head: str | None = None) -> Snapshot:
    """Archive original bytes and publish a complete typed snapshot with explicit lineage."""
    payload = source.read_bytes()
    converters = {"futu-bars": futu_table, "polymarket-events": event_table}
    if kind not in converters:
        raise ValueError("Unsupported initial capture schema")
    table = converters[kind](payload)
    metadata = {"capture_sha256": store.archive(payload), "capture_kind": kind,
                "schema_version": 1, "real_orders_enabled": False,
                "availability": "observed_metadata" if kind == "polymarket-events" else "source_labels_only"}
    return store.publish(dataset, table, metadata, expected_head=expected_head)


def main() -> None:
    """Import an existing capture without calling providers, changing defaults or placing orders."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--kind", choices=("futu-bars", "polymarket-events"), required=True)
    parser.add_argument("--expected-head")
    args = parser.parse_args()
    result = ingest(SnapshotStore(args.root), args.source, args.dataset, args.kind, expected_head=args.expected_head)
    print(json.dumps(asdict(result), indent=2))


if __name__ == "__main__":
    main()
