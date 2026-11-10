"""Independent source-data and native Algorithm Framework accounting verification.

No native execution, indicator or fee implementation is imported by this oracle.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
import json
from pathlib import Path
from statistics import median
import pandas as pd


def number(value: object) -> Decimal:
    """Read finite JSON numbers without binary floating-point accounting."""
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("Non-finite native feature result")
    return result


def same(actual: object, expected: Decimal, name: str) -> None:
    """Reject precision loss beyond the documented native-JSON tolerance."""
    if abs(number(actual) - expected) > Decimal(".000000001"):
        raise ValueError(f"Native LEAN {name} mismatch: {actual} != {expected}")


def fee(notional: Decimal) -> Decimal:
    """Compute the existing HK Tier I charge scenario independently."""
    return (max(Decimal(18), notional * Decimal(".0005")) + notional * Decimal(".000127")
            + (notional * Decimal(".001")).to_integral_value(rounding=ROUND_CEILING)).quantize(
                Decimal(".01"), rounding=ROUND_HALF_UP)


def quotes(price: Decimal) -> tuple[Decimal, Decimal]:
    """Reconstruct declared adverse derived quotes from a real captured open."""
    return ((price * Decimal(".9995") / Decimal(".2")).to_integral_value(rounding=ROUND_FLOOR) * Decimal(".2"),
            (price * Decimal("1.0005") / Decimal(".2")).to_integral_value(rounding=ROUND_CEILING) * Decimal(".2"))


def expiry(time: pd.Timestamp) -> pd.Timestamp:
    """Count 45 open-market minutes on the adapter's documented weekday HK calendar."""
    remaining, value = 45, time
    while remaining:
        if value.weekday() >= 5 or value.hour >= 16:
            value = (value.normalize() + pd.Timedelta(days=1)) + pd.Timedelta(hours=9, minutes=30)
            continue
        if value.hour == 12:
            value = value.normalize() + pd.Timedelta(hours=13)
            continue
        value += pd.Timedelta(minutes=1)
        remaining -= 1
    return value


def portfolio_target(nav: Decimal, holding: int, raw: Decimal, direction: str) -> int:
    """Independently size a 45% long target in whole lots with the native fee reservation policy."""
    if direction == "Flat":
        return 0
    weight = Decimal(".45")
    target = int((nav * weight / (raw * 100)).to_integral_value(rounding=ROUND_FLOOR)) * 100
    delta = target - holding
    if delta == 0:
        return holding
    bid, ask = quotes(raw)
    charge = fee(abs(delta) * (ask if delta > 0 else bid))
    return int(((nav - charge) * weight / (raw * 100)).to_integral_value(rounding=ROUND_FLOOR)) * 100


def audit_features(frame: pd.DataFrame, result: dict[str, object]) -> dict[str, tuple[Decimal, Decimal]]:
    """Require every indicator value, source timestamp, readiness flag and selected batch value."""
    rows = result["features"]
    if len(rows) != len(frame):
        raise ValueError("Missing/extra source observations in LEAN feature stream")
    values, state, expected = [], Decimal(0), {}
    fast, slow = result["fast_period"], result["slow_period"]
    alpha = Decimal(2) / (fast + 1)
    for index, ((time, row), native) in enumerate(zip(frame.iterrows(), rows), 1):
        raw = number(row.open)
        values.append(raw)
        if native["time"] != str(time):
            raise ValueError("LEAN feature timestamps differ from source")
        sma_fast = sum(values[-fast:]) / min(index, fast)
        sma_slow = sum(values[-slow:]) / min(index, slow)
        state = sma_fast if index == fast else raw * alpha + state * (1 - alpha) if index > fast else Decimal(0)
        for name, value in (("source", raw), ("fast", sma_fast), ("slow", sma_slow), ("ema", state)):
            same(native[name], value, name)
        if native["ready"] != (index >= slow):
            raise ValueError("Native indicator readiness differs from required sample count")
        expected[str(time)] = (sma_fast, sma_slow)
    benchmark = result["benchmark"]
    if benchmark.get("parity") is not True or len(benchmark["selected_series"]) != len(values):
        raise ValueError("Missing verified implementation selection output")
    for i, observed in enumerate(benchmark["selected_series"], 1):
        same(observed, sum(values[max(0, i - 60):i]) / min(i, 60), "selected batch mean")
    medians = {name: median(benchmark[key]) for name, key in (
        ("native", "native_ms"), ("window_scan", "window_scan_ms"), ("rolling_sum", "rolling_sum_ms"))}
    if any(len(benchmark[k]) != 7 or min(benchmark[k]) <= 0
           for k in ("native_ms", "window_scan_ms", "rolling_sum_ms")):
        raise ValueError("Benchmark requires seven positive post-warmup measurements")
    if benchmark["selected_batch_implementation"] != min(medians, key=medians.get):
        raise ValueError("Batch backend selection ignored measured medians")
    return expected


def audit_data_operations(frame: pd.DataFrame, result: dict[str, object]) -> None:
    """Verify native history and five-minute aggregation against independently grouped source opens."""
    groups = list(frame.groupby(frame.index.floor("5min")))
    if len(groups) != len(result["consolidated"]):
        raise ValueError("Native consolidator lost or added a source bucket")
    for (time, rows), bar in zip(groups, result["consolidated"]):
        end = time + pd.Timedelta(minutes=5)
        if bar["time"] != str(time) or bar["end"] != str(end) or pd.Timestamp(bar["available"]) < end:
            raise ValueError("Consolidated bar timestamp or availability violates causality")
        expected = {"open": rows.open.iloc[0], "close": rows.open.iloc[-1],
                    "high": rows.open.max(), "low": rows.open.min(), "volume": len(rows)}
        for field, value in expected.items():
            same(bar[field], number(value), "consolidated " + field)
    days = sorted(set(str(t.date()) for t in frame.index))
    if result.get("clocks") != [day + " 16:00:00" for day in days]:
        raise ValueError("Missing or misdated close control observations")
    if len(result["history"]) != len(days):
        raise ValueError("A daily native History probe is missing")
    for day, query in zip(days, result["history"]):
        requested = day + " 10:00:00"
        source = frame.loc[day + " 09:30:00":requested]
        if query["requested"] != requested or len(query["rows"]) != len(source):
            raise ValueError("Native tick History inclusive endpoint/count differs from source")
        for (time, row), actual in zip(source.iterrows(), query["rows"]):
            if actual["time"] != str(time) or actual["time"] > requested:
                raise ValueError("History returned an unavailable future observation")
            same(actual["value"], number(row.open), "history price")
    expected_universe = [str(t + pd.Timedelta(hours=9, minutes=30))
                         for t in pd.bdate_range(days[0], days[-1])]
    if [x["time"] for x in result["universes"]] != expected_universe:
        raise ValueError("Native scheduled universe did not follow the configured weekday HK calendar")
    if any(x["symbol"] != "00700" for x in result["universes"]):
        raise ValueError("Universe unexpectedly selected an unavailable instrument")


def audit(frame: pd.DataFrame, result: dict[str, object]) -> dict[str, object]:
    """Validate the entire native framework path, costed simulated fills and final portfolio."""
    if result.get("passed") is not True or result.get("real_orders_enabled") is not False or result.get("failures"):
        raise ValueError("LEAN feature run failed or enabled real orders")
    indicators = audit_features(frame, result)
    audit_data_operations(frame, result)
    prices = {str(t): number(row.open) for t, row in frame.iterrows()}
    expected_insights = [t for i, t in enumerate(prices, 1)
                         if i >= result["slow_period"] and pd.Timestamp(t).minute % 30 == 0]
    observed_insights = [e["time"] for e in result["trace"] if e["type"] == "insight"]
    if observed_insights != expected_insights:
        raise ValueError("Missing/extra native Insight lifecycle events")
    cash, average, holding, desired = Decimal(100000), Decimal(0), 0, 0
    active, fills, overrides, targets, daily = [], 0, 0, 0, {}
    previous_time = ""
    for event in result["trace"]:
        time = event["time"]
        if time < previous_time:
            raise ValueError("Native framework event journal is not chronological")
        if time != previous_time and holding != desired:
            raise ValueError("Native immediate execution did not achieve the previous effective target")
        previous_time = time
        raw = prices.get(time, prices[str(pd.Timestamp(time).normalize() + pd.Timedelta(hours=15, minutes=59))])
        bid, ask = quotes(raw)
        if event["type"] == "insight":
            fast, slow = indicators[time]
            direction = "Flat" if pd.Timestamp(time).time() >= pd.Timestamp("15:30").time() or fast <= slow else "Up"
            if (event["direction"] != direction or event["generated"] != time
                    or number(event["weight"]) != Decimal(".45")):
                raise ValueError("Native Insight differs from causal observed alpha")
            if event["expires"] != str(expiry(pd.Timestamp(time))):
                raise ValueError("Native Insight expiry differs from configured trading-time horizon")
            active.append(event)
        elif event["type"] == "target":
            current = [i for i in active if i["expires"] >= time]
            direction = current[-1]["direction"] if current else "Flat"
            wanted = portfolio_target(cash + holding * raw, holding, raw, direction)
            if number(event["quantity"]) != wanted:
                raise ValueError(f"Native target mismatch at {time}: {event['quantity']} != {wanted}")
            desired, targets = wanted, targets + 1
        elif event["type"] == "risk":
            if holding <= 0 or number(event["holding"]) != holding:
                raise ValueError("Native risk state differs from independent holdings")
            same(event["average"], average, "risk average entry")
            same(event["bid"], bid, "risk liquidation quote")
            pnl = ((bid - average) * holding - fee(bid * holding)) / (average * holding)
            same(event["pnl"], pnl, "risk liquidation PnL")
            expected_override = 0 if pnl < -number(result["risk_limit"]) else None
            if event["override_quantity"] != expected_override:
                raise ValueError("Native risk override disagrees with independent threshold check")
            if expected_override == 0:
                active, desired, overrides = [], 0, overrides + 1
        elif event["type"] == "fill":
            quantity = int(number(event["quantity"]))
            if quantity != desired - holding or quantity == 0 or quantity % 100:
                raise ValueError("Native execution quantity differs from effective whole-lot target")
            price = ask if quantity > 0 else bid
            charge = fee(abs(quantity) * price)
            same(event["price"], price, "fill quote")
            same(event["fee"], charge, "fill fee")
            cash -= quantity * price + charge
            if quantity > 0:
                average = (average * holding + quantity * price) / (holding + quantity)
            holding += quantity
            if holding == 0:
                average = Decimal(0)
            if cash < 0 or holding < 0:
                raise ValueError("Native fill violated funded long-only inventory/cash")
            same(event["cash"], cash, "native cash after fill")
            if number(event["holding"]) != holding:
                raise ValueError("Native holding differs after simulated fill")
            fills += 1
        else:
            raise ValueError("Unknown native framework journal event")
        daily[time[:10]] = str(cash)
    same(result["cash"], cash, "final cash")
    if result["holding"] != holding or holding != desired or fills == 0 or overrides == 0 or targets == 0:
        raise ValueError("Missing native execution/risk coverage or mismatched final holdings")
    if set(result.get("daily_cash", {})) != set(daily):
        raise ValueError("Native daily account snapshots are incomplete")
    for day, cash_at_close in daily.items():
        same(result["daily_cash"][day], number(cash_at_close), "daily cash snapshot")
    benchmark = result["benchmark"]
    return {"passed": True, "observations": len(frame), "consolidated_bars": len(result["consolidated"]),
            "history_observations": sum(len(x["rows"]) for x in result["history"]),
            "universe_selections": len(result["universes"]), "insights": len(observed_insights),
            "portfolio_targets": targets, "risk_overrides": overrides, "fills": fills,
            "cash": str(cash), "holding": holding,
            "daily_cash": daily,
            "selected_batch_implementation": benchmark["selected_batch_implementation"],
            "benchmark_median_ms": {k: median(benchmark[k])
                                    for k in ("native_ms", "window_scan_ms", "rolling_sum_ms")},
            "real_orders_enabled": False}


def audit_optimizer(optimizer: dict[str, object], candidates: dict[str, object], cutoff: str) -> dict[str, object]:
    """Require native grid enumeration and selection to use only independently verified validation balances."""
    if (optimizer.get("native_strategy") != "GridSearchOptimizationStrategy"
            or optimizer.get("holdout_used_for_selection") is not False or optimizer["validation_end"] != cutoff
            or optimizer["expected_backtests"] != 3):
        raise ValueError("Native optimizer scope or validation cutoff changed")
    scores = {int(name.split("_")[1]): number(value["daily_cash"][cutoff]) for name, value in candidates.items()}
    if (len(optimizer["scores"]) != 3 or len(scores) != 3
            or {int(s["fast"]): number(s["validation_cash"]) for s in optimizer["scores"]} != scores):
        raise ValueError("Native optimizer consumed unverified or holdout scores")
    generated = optimizer["generated"]
    expected = [{"fast-period": str(fast), "slow-period": "60", "risk-limit": ".004"} for fast in (10, 20, 30)]
    if [p["value"] for p in generated] != expected or [p["id"] for p in generated] != [1, 2, 3]:
        raise ValueError("Native grid generation omitted, duplicated or changed a candidate")
    selected = max(scores, key=scores.get)
    if optimizer["selected_parameters"] != expected[(10, 20, 30).index(selected)]:
        raise ValueError("Native optimizer selected a non-maximal validation objective")
    same(optimizer["validation_cash"], scores[selected], "native optimizer objective")
    return {"passed": True, "native_strategy": optimizer["native_strategy"], "candidates": len(scores),
            "selected_fast_period": selected, "validation_end": cutoff, "validation_cash": str(scores[selected]),
            "holdout_used_for_selection": False}


def audit_research(frame: pd.DataFrame, result: dict[str, object], repo: Path) -> dict[str, object]:
    """Check native QuantBook History and pandas serialization against original captured prices and times."""
    if (result.get("passed") is not True or result.get("python_finalized") is not True
            or result.get("real_orders_enabled") is not False):
        raise ValueError("Incomplete or unsafe QuantBook lifecycle")
    book = result["quantbook"]
    shared = (repo / ".venv-joint").resolve()
    if (book.get("history_only") is not True or Path(book["shared_prefix"]).resolve() != shared
            or not Path(book["pandas_file"]).resolve().is_relative_to(shared)):
        raise ValueError("QuantBook bypassed the shared environment or research-only mode")
    days = [str(frame.index.min().date()), str(frame.index.max().date())]
    if [q["date"] for q in book["queries"]] != days:
        raise ValueError("QuantBook omitted or changed a requested real-data interval")
    total = 0
    for query in book["queries"]:
        # Native local Tick History retains observations at both requested boundaries.
        expected = frame.loc[query["date"]].between_time("09:30", "10:00", inclusive="both")
        rows = query["rows"]
        if len(rows) != len(expected) or query["dataframe_rows"] != len(expected) or not rows:
            raise ValueError("QuantBook native/pandas row count differs from source")
        flat = json.loads(query["dataframe"])
        indices = [str(i) for i in range(len(expected))]
        if any(list(flat[column]) != indices for column in ("time", "lastprice")):
            raise ValueError("QuantBook pandas row identity differs from native History")
        for index, ((stamp, row), native) in enumerate(zip(expected.iterrows(), rows)):
            if native["time"] != str(stamp) or flat["time"][str(index)] != stamp.value // 1_000_000:
                raise ValueError("QuantBook native/pandas timestamp differs from capture")
            same(native["price"], number(row.open), "QuantBook History price")
            same(flat["lastprice"][str(index)], number(row.open), "QuantBook pandas price")
        total += len(rows)
    return {"passed": True, "queries": len(days), "source_observations_verified": total,
            "native_history_and_pandas_match": True, "python_finalized": True,
            "shared_python": str(shared), "real_orders_enabled": False}
