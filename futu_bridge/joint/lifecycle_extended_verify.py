"""Independent Decimal oracle for amended, triggered and expiring simulated orders.

Reconstruct quotes directly from real source opens. Never import engine adapters,
their fee calculator, their mutable order state or their produced fill decisions.
"""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
import pandas as pd


def source_prices(frame: pd.DataFrame) -> dict[str, dict[str, Decimal]]:
    """Reconstruct the declared five-bps adverse, 0.2-HKD rounded quote assumption."""
    return {str(t): {"buy": (Decimal(str(r.open)) * Decimal("1.0005") / Decimal(".2")).to_integral_value(
        rounding=ROUND_CEILING) * Decimal(".2"),
        "sell": (Decimal(str(r.open)) * Decimal(".9995") / Decimal(".2")).to_integral_value(
            rounding=ROUND_FLOOR) * Decimal(".2")} for t, r in frame.iterrows()}


def reference(task: dict[str, object], platform: str, frame: pd.DataFrame) -> tuple[list, dict]:
    """Enumerate each order's quote eligibility between instruction changes and its deadline."""
    prices = source_prices(frame)
    if len(task["quotes"]) != len(prices) or any(
            p["time"] not in prices or Decimal(p["ask"]) != prices[p["time"]]["buy"]
            or Decimal(p["bid"]) != prices[p["time"]]["sell"] for p in task["quotes"]):
        raise ValueError("Lifecycle quotes differ from independent real-source reconstruction")
    events, final, capacity = [], {}, {}
    for original in task["orders"]:
        order = deepcopy(original)
        key, side = order["id"], order["side"]
        filled, triggered, closed = 0, False, False
        notional, charged = Decimal(0), Decimal(0)
        changes = {c["time"]: c for c in order.get("amendments", [])}
        expiry = order.get("expiry") if order.get("tif", "GTC") != "GTC" else None
        for time, sides in prices.items():
            if time < order["time"] or closed or (expiry and time >= expiry):
                continue
            if order["cancel"] and time > order["cancel"]:
                continue
            # Separate pre-command matching from the acknowledgement/rematch phase.
            for amended in (False, True):
                if amended:
                    if time not in changes:
                        continue
                    if closed:
                        raise ValueError("Reference cannot amend a terminal order")
                    change = {k: v for k, v in changes[time].items() if k != "time"}
                    order.update(change)
                    events.append({"id": key, "time": time, "type": "amend", **change})
                if platform == "lean" and time == order["time"]:
                    continue
                quote, limit = sides[side], Decimal(order["price"])
                trigger = Decimal(order.get("trigger", order["price"]))
                if order["kind"] in ("stop", "stop_limit"):
                    triggered |= quote >= trigger if side == "buy" else quote <= trigger
                marketable = quote <= limit if side == "buy" else quote >= limit
                eligible = triggered if order["kind"] == "stop" else marketable
                if order["kind"] == "stop_limit":
                    eligible &= triggered
                room = 100 - capacity.get((time, side), 0)
                if not eligible or room <= 0 or closed:
                    continue
                amount = min(room, order["shares"] - filled)
                price = quote
                if order["kind"] == "limit" and time > order["time"] and not (amended and platform != "lean"):
                    price = limit
                notional += amount * price
                total = (max(Decimal(18), notional * Decimal(".0005")) + notional * Decimal(".000127")
                         + (notional * Decimal(".001")).to_integral_value(rounding=ROUND_CEILING)).quantize(
                             Decimal(".01"), rounding=ROUND_HALF_UP)
                filled += amount
                capacity[time, side] = capacity.get((time, side), 0) + amount
                events.append({"id": key, "time": time, "type": "fill", "shares": amount,
                               "filled": filled, "price": str(price), "fee": str(total - charged)})
                charged, closed = total, filled == order["shares"]
                if not closed and order.get("after_partial"):
                    change = order.pop("after_partial")
                    order.update(change)
                    events.append({"id": key, "time": time, "type": "amend", **change})
            if time == order["cancel"] and not closed:
                events.append({"id": key, "time": time, "type": "cancel"})
                closed = True
        status = "FILLED" if filled == order["shares"] else "OPEN"
        if status != "FILLED":
            if expiry and (not order["cancel"] or expiry <= order["cancel"]):
                events.append({"id": key, "time": expiry, "type": "expire"})
                status = "EXPIRED"
            elif order["cancel"]:
                status = "CANCELED"
        final[key] = {"filled": filled, "status": status, "triggered": triggered}
    return sorted(events, key=lambda e: e["time"]), final


def audit(task: dict[str, object], result: dict[str, object], frame: pd.DataFrame) -> dict[str, object]:
    """Require every event, final native state, trigger latch, cash debit and inventory change."""
    if result.get("real_orders_enabled") is not False or result.get("failures"):
        raise ValueError("Advanced execution failed or enabled real orders")
    expected, final = reference(task, result["platform"], frame)
    notices = []
    if result["platform"] == "nautilus":
        for index, event in enumerate(expected):
            if (event["type"] == "fill" and event["filled"] > event["shares"]
                    and final[event["id"]]["status"] == "FILLED"
                    and index and expected[index - 1]["type"] == "amend"
                    and expected[index - 1]["time"] == event["time"]):
                notices.append({"id": event["id"], "time": event["time"],
                                "type": "cancel_after_fill", "status": "FILLED"})
    if result.get("native_notices", []) != notices:
        raise ValueError("Unexpected or missing native post-fill cancellation notice")
    if len(expected) != len(result["events"]) or set(final) != set(result["orders"]):
        raise ValueError(f"Missing/extra advanced events or orders: {task['case']} {result['events']} != {expected}")
    cash, holding = Decimal(100000), 0
    specs = {o["id"]: o for o in task["orders"]}
    for observed, wanted in zip(result["events"], expected):
        for field, value in wanted.items():
            numeric = field in ("price", "trigger", "shares", "filled", "fee")
            equal = (abs(Decimal(str(observed.get(field))) - Decimal(str(value))) < Decimal(".000001")
                     if numeric else observed.get(field) == value)
            if not equal:
                raise ValueError(f"Advanced {result['platform']} {task['case']} mismatch: {observed} != {wanted}")
        if wanted["type"] == "fill":
            signed = wanted["shares"] * (1 if specs[wanted["id"]]["side"] == "buy" else -1)
            cash -= signed * Decimal(wanted["price"]) + Decimal(wanted["fee"])
            holding += signed
            if cash < 0 or holding < 0:
                raise ValueError("Advanced fill exceeded funded long-only cash/inventory")
    statuses = {"OPEN": {"ACCEPTED", "SUBMITTED", "PARTIALLYFILLED"}, "FILLED": {"FILLED"},
                "CANCELED": {"CANCELED"}, "EXPIRED": {"EXPIRED", "CANCELED"}}
    for key, wanted in final.items():
        actual = result["orders"][key]
        if actual["filled"] != wanted["filled"] or actual["status"].replace("_", "") not in statuses[wanted["status"]]:
            raise ValueError(f"Advanced final state mismatch: {key}: {actual} != {wanted}")
        if specs[key]["kind"] == "stop_limit" and actual.get("triggered") != wanted["triggered"]:
            raise ValueError("Stop-limit trigger latch was lost or invented")
    if abs(Decimal(str(result["cash"])) - cash) > Decimal(".000001") or result["holding"] != holding:
        raise ValueError("Advanced native account differs from independent fills and fees")
    return {"passed": True, "fills": sum(e["type"] == "fill" for e in expected),
            "cancels": sum(e["type"] == "cancel" for e in expected),
            "amendments": sum(e["type"] == "amend" for e in expected),
            "expiries": sum(e["type"] == "expire" for e in expected),
            "triggered": sum(s["triggered"] for k, s in final.items() if specs[k]["kind"] == "stop_limit"),
            "native_notices": len(notices),
            "open_orders": sum(s["status"] == "OPEN" for s in final.values()), "cash": str(cash), "holding": holding}
