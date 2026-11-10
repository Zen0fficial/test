"""Independent source-price, order-lifecycle and native-account conformance oracle."""

from __future__ import annotations

from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
import pandas as pd


def audit(task: dict[str, object], result: dict[str, object], frame: pd.DataFrame) -> dict[str, object]:
    """Enumerate eligible source instants independently and require every expected event."""
    if task.get("extended"):
        from .lifecycle_extended_verify import audit as audit_extended

        return audit_extended(task, result, frame)
    if result.get("real_orders_enabled") is not False or result.get("failures"):
        raise ValueError("Native lifecycle execution failed or enabled real orders")
    prices = {}
    for time, row in frame.iterrows():
        raw = Decimal(str(row.open))
        prices[str(time)] = {
            "buy": (raw * Decimal("1.0005") / Decimal(".2")).to_integral_value(
                rounding=ROUND_CEILING) * Decimal(".2"),
            "sell": (raw * Decimal(".9995") / Decimal(".2")).to_integral_value(
                rounding=ROUND_FLOOR) * Decimal(".2")}
    expected, final = [], {}
    for order in task["orders"]:
        eligible = [t for t in prices if order["time"] <= t <= order["cancel"]]
        # Native LEAN BacktestingBrokerage deliberately defers non-market orders
        # until a later observation. Preserve that behavior instead of bypassing it.
        if result["platform"] == "lean":
            eligible = [t for t in eligible if t > order["time"]]
        side, bound = order["side"], Decimal(order["price"])
        crossing = [t for t in eligible if (
            prices[t][side] >= bound if (side == "buy") == (order["kind"] == "stop")
            else prices[t][side] <= bound)]
        if order["kind"] == "stop" and crossing:
            crossing = [t for t in eligible if t >= crossing[0]]
        selected = crossing[:order["shares"] // 100]
        if order["cancel_partial"]:
            selected = selected[:1]
        cumulative, charged = Decimal(0), Decimal(0)
        for index, time in enumerate(selected):
            price = bound if order["kind"] == "limit" and time != order["time"] else prices[time][side]
            cumulative += price * 100
            fee = (max(Decimal(18), cumulative * Decimal(".0005")) + cumulative * Decimal(".000127")
                   + (cumulative * Decimal(".001")).to_integral_value(rounding=ROUND_CEILING)).quantize(
                       Decimal(".01"), rounding=ROUND_HALF_UP)
            expected.append({"type": "fill", "id": order["id"], "time": time, "shares": 100,
                             "price": str(price), "fee": str(fee - charged), "filled": 100 * (index + 1)})
            charged = fee
        filled = 100 * len(selected)
        status = "FILLED" if filled == order["shares"] else "CANCELED"
        final[order["id"]] = {"filled": filled, "status": status}
        if status == "CANCELED":
            time = selected[0] if order["cancel_partial"] and selected else order["cancel"]
            expected.append({"type": "cancel", "id": order["id"], "time": time})
    expected.sort(key=lambda e: e["time"])
    events = result["events"]
    if len(events) != len(expected) or set(result["orders"]) != set(final):
        raise ValueError("Missing or extra native lifecycle events/orders")
    cash, holding = Decimal(100000), 0
    sides = {o["id"]: o["side"] for o in task["orders"]}
    for observed, reference in zip(events, expected):
        for field in reference:
            if field in ("price", "fee", "shares", "filled"):
                equal = abs(Decimal(str(observed[field])) - Decimal(str(reference[field]))) < Decimal(".000001")
            else:
                equal = observed[field] == reference[field]
            if not equal:
                raise ValueError(f"Lifecycle mismatch {result['platform']} {task['case']}: {observed} != {reference}")
        if reference["type"] == "fill":
            sign = 1 if sides[reference["id"]] == "buy" else -1
            cash -= sign * 100 * Decimal(reference["price"]) + Decimal(reference["fee"])
            holding += sign * 100
            if cash < 0 or holding < 0:
                raise ValueError("Execution exceeded funded long-only inventory/cash")
    for key, state in final.items():
        actual = result["orders"][key]
        if actual["filled"] != state["filled"] or actual["status"].replace("_", "") != state["status"]:
            raise ValueError("Native final order state differs from the independent lifecycle")
    if abs(Decimal(str(result["cash"])) - cash) > Decimal(".000001") or result["holding"] != holding:
        raise ValueError("Native final account differs from independent fills and fee debits")
    return {"passed": True, "fills": sum(e["type"] == "fill" for e in expected),
            "cancels": sum(e["type"] == "cancel" for e in expected), "cash": str(cash), "holding": holding}
