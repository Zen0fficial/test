"""Compare real live-engine callbacks with independently decoded source logs."""

import argparse
from collections import Counter
import datetime as dt
from decimal import Decimal
import json
from pathlib import Path
import statistics

from .capture import read_json, write_json


def records(path):
    with Path(path).open(encoding="utf-8") as f:
        for line in f:
            if line.strip(): yield json.loads(line, parse_float=Decimal)


def iso_ns(value):
    stamp = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None: raise ValueError("Source timestamp lost its timezone")
    delta = stamp.astimezone(dt.timezone.utc) - dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
    return (delta.days * 86400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1000


def verify(output, require_reconnect=False):
    root = Path(output)
    if list(root.glob("*.jsonl.[123]")):
        raise ValueError("Logs rotated; use a bounded test capture for exhaustive verification")
    report = read_json(root / "run.json")
    raw = {str(r["sequence"]): r for r in records(root / "raw.jsonl")}
    if not raw: raise ValueError("No provider traffic")
    starts = {}
    for row in raw.values(): starts.setdefault(row["connection"], row["received_ns"])
    admitted = list(records(root / "admitted.jsonl"))
    callbacks = list(records(root / "callbacks.jsonl"))
    if not admitted or len(admitted) != len(callbacks):
        raise ValueError(f"Live delivery loss: admitted={len(admitted)}, callbacks={len(callbacks)}")
    accepted = {r["record_id"]: r for r in admitted}
    if len(accepted) != len(admitted): raise ValueError("Duplicate admission IDs")
    seen, trade_ids, counts = set(), set(), Counter()
    callback_lag, exchange_lag = [], []
    first_trade, last_trade = None, None
    for row in callbacks:
        identity = row["record_id"]
        if identity in seen: raise ValueError("Duplicate framework callback")
        seen.add(identity)
        if identity not in accepted: raise ValueError("Callback without admission")
        source = raw[identity.split(":", 1)[0]]
        data, provenance = source["body"]["data"], source["body"]["source"]
        if provenance.get("provider") != "futu-desktop-session" or provenance.get("live_push") is not True:
            raise ValueError("Callback was not sourced from native provider push")
        if data.get("initial_snapshot") or data.get("backfill"):
            raise ValueError("A bootstrap/backfill was presented as new live data")
        if data["symbol"] != row["symbol"] or row["symbol"] != "HK.00700":
            raise ValueError("Live symbol identity changed")
        if row["instrument_id"] != ("00700.SEHK" if report["platform"] == "NautilusTrader" else "00700"):
            raise ValueError("Native platform instrument identity changed")
        if row["connection"] != source["connection"] or row["received_ns"] != source["received_ns"]:
            raise ValueError("Receipt provenance changed")
        if row["kind"] == "trade":
            native = next(t for t in data["trades"] if t["trade_id"] == row["trade_id"])
            if row["trade_id"] in trade_ids: raise ValueError("Duplicate live trade across reconnects")
            trade_ids.add(row["trade_id"])
            stamp = native["exchange_timestamp_ms"] * 1_000_000
            expected = {"price": Decimal(str(native["price"])), "size": Decimal(native["size_decimal"])}
            if row["timestamp_basis"] != "exchange": raise ValueError("Trade timestamp is not exchange time")
            if stamp >= min(starts.values()): counts["fresh_exchange_trades"] += 1
            if row["connection"] > 1 and stamp >= starts[row["connection"]]: counts["fresh_trades_after_reconnect"] += 1
            first_trade = stamp if first_trade is None else min(first_trade, stamp)
            last_trade = stamp if last_trade is None else max(last_trade, stamp)
            exchange_lag.append((row["callback_ns"] - stamp) / 1_000_000)
        else:
            if source["type"] != "orderbook" or data["book_kind"] != "top_of_book":
                raise ValueError("A native quote must come from a complete provider BBO")
            stamp = iso_ns(data["observed_at"])
            expected = {"bid": Decimal(str(data["bids"][0]["price"])), "ask": Decimal(str(data["asks"][0]["price"])),
                        "bid_size": Decimal(str(data["bids"][0]["volume"])), "ask_size": Decimal(str(data["asks"][0]["volume"]))}
            if row["timestamp_basis"] != "provider_observed": raise ValueError("Book time was mislabeled as exchange time")
        if stamp != row["event_ns"]:
            raise ValueError(f"Native timestamp changed for {identity}: source={stamp}, callback={row['event_ns']}")
        for field, value in expected.items():
            if Decimal(row[field]) != value or Decimal(accepted[identity][field]) != value:
                raise ValueError(f"Native decimal mismatch: {identity} {field}")
        lag = (row["callback_ns"] - source["received_ns"]) / 1_000_000
        if lag < 0: raise ValueError("Framework callback precedes local receipt")
        callback_lag.append(lag)
        counts[row["kind"]] += 1
    if seen != set(accepted): raise ValueError("Undelivered admitted observations")
    if counts["fresh_exchange_trades"] < 5 or counts["quote"] < 5:
        raise ValueError("Insufficient fresh market activity; a quiet stream is not a passing live test")
    counters = {**report.get("selection", {}), **report["transport"]["counts"], **report["counts"]}
    for key in ("conversion_errors", "queue_overflows", "trade_revisions", "provider_gaps"):
        if counters.get(key, 0): raise ValueError("Live test reported " + key)
    if report.get("live_orders") != 0: raise ValueError("Live observer submitted an order")
    if report.get("pending_callbacks", 0) or report["transport"].get("connected"):
        raise ValueError("Live feed did not drain and shut down")
    if require_reconnect and (counters.get("connections", 0) < 2 or counters.get("forced_disconnects", 0) != 1 or not counts["fresh_trades_after_reconnect"]):
        raise ValueError("Fresh live recovery after the forced disconnect is unverified")
    def summary(values):
        values = sorted(values)
        return {"median": statistics.median(values), "p95": values[int((len(values) - 1) * .95)], "max": max(values)}
    result = {"passed": True, "platform": report["platform"], "symbol": "HK.00700", "counts": dict(counts),
              "connections": counters.get("connections"), "forced_disconnects": counters.get("forced_disconnects", 0),
              "exact_native_decimal_and_timestamp_match": True, "live_orders": 0,
              "trade_observation_span_seconds": (last_trade - first_trade) / 1_000_000_000,
              "receipt_to_callback_ms": summary(callback_lag), "observed_exchange_to_callback_ms": summary(exchange_lag),
              "latency_note": "Observed during this test; exchange clock/provider delay included, not a latency guarantee",
              "exchange_replay_or_lossless_claim": False,
              "source": "Existing local native Futu SSE provider; no REST market-data polling"}
    write_json(root / "verification.json", result)
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", required=True)
    p.add_argument("--require-reconnect", action="store_true")
    print(json.dumps(verify(**vars(p.parse_args())), indent=2))


if __name__ == "__main__":
    main()
