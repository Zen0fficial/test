"""Acquire an explicit research basket from the unchanged loopback Futu bars API.

This is a manually selected basket, not historical index membership or a claim
of survivorship-free investment evaluation. Each source response stays intact.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import urllib.parse
import urllib.request

import pyarrow as pa

from .captures import futu_table
from .snapshots import SnapshotStore


def capture(symbols: list[str], start: str, end: str, output: Path) -> dict[str, object]:
    """Sequentially request genuine unadjusted daily captures and fail closed on missing constituents."""
    if not symbols or len(set(symbols)) != len(symbols):
        raise ValueError("Provide a nonempty unique explicit symbol basket")
    output.mkdir(parents=True, exist_ok=False)
    store, tables, sources = SnapshotStore(output / "store"), [], {}
    for ordinal, symbol in enumerate(symbols):
        query = urllib.parse.urlencode({"symbol": symbol, "timeframe": "1d", "start": start,
                                       "end": end, "adjustment": "none"})
        with urllib.request.urlopen("http://127.0.0.1:8765/v1/bars?" + query, timeout=90) as response:
            payload = response.read()
        table = futu_table(payload)
        if set(table["symbol"].to_pylist()) != {symbol}:
            raise ValueError("Provider returned a different panel instrument")
        path = output / f"response_{ordinal}.json"
        path.write_bytes(payload)
        digest = store.archive(payload)
        sources[symbol] = {"file": path.name, "sha256": digest, "rows": table.num_rows}
        tables.append(table)
        (output / "progress.json").write_text(json.dumps(sources, indent=2), encoding="utf-8")
    panel = pa.concat_tables(tables).sort_by([("symbol", "ascending"), ("event_ns", "ascending")])
    snapshot = store.publish("research_basket_daily", panel,
                             {"universe": "explicit manually selected research basket", "sources": sources,
                              "historical_index_membership": False, "real_orders_enabled": False}, expected_head=None)
    report = {"passed": True, "snapshot": snapshot.identity, "snapshot_sha256": snapshot.sha256,
              "sources": sources, "rows": panel.num_rows, "start": start, "end": end,
              "requested_symbols": symbols, "provider": "unchanged local Futu", "real_orders_enabled": False,
              "universe_bias": "Basket selected retrospectively; not historical index membership",
              "code_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    (output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def main() -> None:
    """Capture a user-selected basket with a fixed local provider and no execution capability."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", nargs="+", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(capture(args.symbols, args.start, args.end, args.output), indent=2))


if __name__ == "__main__":
    main()
