"""Independent decision, lot, fee and cash audit of the daily ML comparison.

This does not invoke the production simulator, decision mapper or fee helper.
It reads exact source prices and the persisted model probabilities, independently
applies the declared next-session threshold rule, and audits every filled and
unfilled day, including fee-free controls. Historical execution is assumed.
"""

from __future__ import annotations

import argparse
from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP
import hashlib
import json
import math
from pathlib import Path
from typing import Any

import pandas as pd

from .session import write_report


def fees(notional: Decimal, charged: bool) -> dict[str, Decimal]:
    """Recompute the declared IBKR Pro SEHK Tier-I components without production fee code."""
    if not charged:
        return {"total": Decimal(0)}
    parts = {"commission": max(Decimal(18), notional / 2000),
             "exchange": notional * Decimal(".0000565"), "clearing": notional * Decimal(".000042"),
             "sfc": notional * Decimal(".000027"), "frc": notional * Decimal(".0000015"),
             "stamp": (notional / 1000).quantize(Decimal(1), rounding=ROUND_CEILING)}
    return {**parts, "total": sum(parts.values()).quantize(Decimal(".01"), rounding=ROUND_HALF_UP)}


def prices(root: Path, report: dict[str, Any]) -> dict[str, dict[str, Decimal]]:
    """Read source prices independently, including complete multi-capture union lineage."""
    raw = (root / "provider_response.json").read_bytes()
    if hashlib.sha256(raw).hexdigest() != report["response_sha256"]:
        raise ValueError("Comparison original archive anchor changed")
    source = report.get("dataset_lineage", {})
    if source.get("analytical_source") == "parquet":
        from .storage import SnapshotStore
        from .qlib_parquet_lineage import verify as verify_sources

        store = SnapshotStore(Path(source["snapshot_root"]))
        snapshot = store.snapshot(source["snapshot"])
        original = verify_sources(store, snapshot.identity)
        if (snapshot.sha256 != source["sha256"] or original.evidence() != source["capture_lineage"]
                or snapshot.metadata.get("capture_sha256") != report["response_sha256"]):
            raise ValueError("Comparison canonical source lineage changed")
        bars = original.bars()
    else:
        bars = json.loads(raw, parse_float=Decimal)["data"]["bars"]
    result = {bar["date"]: {name: Decimal(str(bar[name])) for name in ("open", "close")} for bar in bars}
    if len(result) != len(bars) or len(result) != report["raw_bars"]:
        raise ValueError("Daily comparison source dates are duplicate or incomplete")
    return result


def audit(source: dict[str, dict[str, Decimal]], decisions: pd.Series, result: dict[str, Any], *,
          threshold: float, bps: int, charged: bool) -> dict[str, Any]:
    """Rebuild funded lots and all daily accounts from exact next-session source prices."""
    dates = list(source)
    cash, total, slippage, peak, drawdown = Decimal(100000), Decimal(0), Decimal(0), Decimal(100000), Decimal(0)
    spread, offset, returns, turnover = Decimal(bps) / 10000, 0, [], {}
    if (result["capital_hkd"] != 100000 or result["threshold"] != threshold
            or result["slippage_bps_per_side"] != bps or result["real_orders_enabled"] is not False
            or result["days"] != len(decisions) or len(result["equity"]) != len(decisions)):
        raise ValueError("Daily simulation configuration or observation count differs")
    for ordinal, (date, probability) in enumerate(decisions.items()):
        day = str(date.date())
        position = dates.index(day)
        if position + 1 >= len(dates) or not math.isfinite(probability) or not 0 <= probability <= 1:
            raise ValueError("Invalid probability or unavailable next observed session")
        following, before = dates[position + 1], cash
        buy, sell = source[following]["open"] * (1 + spread), source[following]["close"] * (1 - spread)
        quantity = int(cash / (buy * 100)) * 100 if probability >= threshold else 0
        while quantity and quantity * buy + fees(quantity * buy, charged)["total"] > cash:
            quantity -= 100
        if quantity:
            month = following[:7]
            turnover[month] = turnover.get(month, Decimal(0)) + (buy + sell) * quantity
            if turnover[month] > 15_000_000:
                raise ValueError("Daily simulation exceeds the implemented monthly commission tier")
            for side, price, sign in (("buy", buy, -1), ("sell", sell, 1)):
                if offset >= len(result["ledger"]):
                    raise ValueError("Threshold decision requires an omitted simulated fill")
                fill = result["ledger"][offset]
                parts = fees(quantity * price, charged)
                cash += sign * quantity * price - parts["total"]
                total += parts["total"]
                expected = {"signal_date": day, "date": following, "side": side, "shares": quantity}
                if (any(fill.get(key) != value for key, value in expected.items()) or cash < 0
                        or Decimal(fill["price"]) != price or Decimal(fill["notional"]) != quantity * price
                        or Decimal(fill["cash_after"]) != cash
                        or {key: Decimal(value) for key, value in fill["fees"].items()} != parts):
                    raise ValueError("Daily simulated decision, source price, fee or cash differs independently")
                offset += 1
            slippage += quantity * (source[following]["open"] + source[following]["close"]) * spread
        point = result["equity"][ordinal]
        if point != {"date": following, "equity_hkd": float(cash), "shares_at_close": 0}:
            raise ValueError("Daily closing inventory or equity differs independently")
        returns.append(float(cash / before - 1))
        peak, drawdown = max(peak, cash), min(drawdown, cash / max(peak, cash) - 1)
    summary = {"ending_equity_hkd": float(cash), "fees_hkd": float(total), "slippage_hkd": float(slippage),
               "net_return": float(cash / 100000 - 1), "max_drawdown": float(drawdown),
               "orders": offset, "round_trips": offset // 2,
               "max_monthly_turnover_hkd": float(max(turnover.values(), default=Decimal(0)))}
    if offset != len(result["ledger"]) or any(result.get(key) != value for key, value in summary.items()):
        raise ValueError("Daily simulation summary does not reconcile to its independently rebuilt journal")
    mean = sum(returns) / len(returns) if returns else 0
    variance = sum((value - mean) ** 2 for value in returns) / max(1, len(returns) - 1)
    sharpe = mean / math.sqrt(variance) * math.sqrt(252) if variance else None
    if result.get("annualized_sharpe_zero_cash_rate") != sharpe:
        raise ValueError("Daily simulated Sharpe diagnostic differs independently")
    return {"passed": True, "decisions_checked": len(decisions), "fills_checked": offset, **summary}


def verify(root: Path, report: dict[str, Any] | None = None) -> dict[str, Any]:
    """Audit all declared daily ML scenarios against their recorded probabilities and original prices."""
    if report is None:
        report = json.loads((root / "report.json").read_text(encoding="utf-8"))
    source = prices(root, report)
    prediction_path = root / "predictions.csv"
    predictions = pd.read_csv(prediction_path, index_col=0, parse_dates=True, float_precision="round_trip")
    if not predictions.index.is_unique or not predictions.index.is_monotonic_increasing:
        raise ValueError("Persisted predictions have duplicate or unordered feature dates")
    test = predictions[predictions.partition == "test"]
    plan = report["plan"]["splits"]["test"]
    if (len(test) != plan["n"] or str(test.index[0].date()) != plan["start"]
            or str(test.index[-1].date()) != plan["end"]):
        raise ValueError("Daily probability journal changed the declared held-out window")
    streams = {"baseline": test.baseline, "selected": test[report["selected"]],
               "always_long_intraday": pd.Series(1., index=test.index), "cash": pd.Series(0., index=test.index)}
    checks = {}
    for name, stream in streams.items():
        for threshold in (.5, .55):
            for bps in (0, 5, 10):
                key = f"{name}_threshold{threshold}_slippage{bps}"
                simulation = json.loads((root / f"simulation_{key}.json").read_text())
                summary = {k: v for k, v in simulation.items() if k not in ("ledger", "equity")}
                if summary != report["simulations"][key]:
                    raise ValueError("Saved daily simulation summary differs from its report")
                checks[key] = audit(source, stream, simulation, threshold=threshold, bps=bps, charged=True)
        key = f"{name}_frictionless"
        simulation = json.loads((root / f"simulation_{key}.json").read_text())
        if {k: v for k, v in simulation.items() if k not in ("ledger", "equity")} != report["simulations"][key]:
            raise ValueError("Saved frictionless simulation summary differs from its report")
        checks[key] = audit(source, stream, simulation, threshold=.5, bps=0, charged=False)
    if set(checks) != set(report["simulations"]):
        raise ValueError("Missing or additional unverified daily simulation scenarios")
    result = {"passed": True, "scenarios": len(checks), "checks": checks,
              "decisions_checked": sum(item["decisions_checked"] for item in checks.values()),
              "fills_checked": sum(item["fills_checked"] for item in checks.values()),
              "decision_rule": "Persisted model probability threshold; following observed open and close",
              "predictions_sha256": hashlib.sha256(prediction_path.read_bytes()).hexdigest(),
              "verifier_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "source_archive_sha256": report["response_sha256"], "real_orders_enabled": False,
              "limitations": ["Retrospective fee schedule and assumed fills, not exchange execution evidence"]}
    write_report(root / "verification.json", result)
    return result


def main() -> None:
    """Recheck a completed model comparison without refitting or submitting any orders."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(verify(args.run), indent=2))


if __name__ == "__main__":
    main()
