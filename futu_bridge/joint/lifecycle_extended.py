"""Causal stop-limit, amendment and intraday expiry scenarios over fixed real quotes."""

from __future__ import annotations

from decimal import Decimal
import pandas as pd

from .lifecycle import scenario

CASES = ("stop_limit_buy", "stop_limit_sell", "stop_limit_gap", "stop_limit_partial", "amend_price", "amend_quantity",
         "amend_stop", "amend_partial", "amend_stop_limit", "gtd_unfilled", "gtd_partial",
         "day_unfilled", "day_partial", "gtc_open", "amend_trigger", "amend_increase", "amend_resume", "gtd_boundary")


def make(frame: pd.DataFrame, case: str) -> dict[str, object]:
    """Use current submission/amendment quotes only; never select dates by future returns."""
    if case not in CASES:
        raise ValueError("Unknown extended lifecycle case")
    task = scenario(frame, "stop_sell" if case == "stop_limit_sell" else "limit_cancel")
    task.update(case=case, extended=True)
    order = task["orders"][-1]
    day = task["day"]
    point = next(p for p in task["quotes"] if p["time"] == order["time"])
    ask, bid = Decimal(point["ask"]), Decimal(point["bid"])
    order.update(cancel=day + " 15:29:00", tif="GTC", amendments=[])
    if case.startswith("stop_limit") or case == "amend_stop_limit":
        trigger = bid - Decimal(".4") if order["side"] == "sell" else ask + Decimal(".4")
        order.update(kind="stop_limit", trigger=str(trigger), price=str(trigger))
        if case in ("stop_limit_gap", "amend_stop_limit"):
            order["price"] = str(bid - 20)
        if case == "stop_limit_partial":
            order["shares"] = 200
    if case in ("amend_price", "amend_quantity", "amend_stop", "amend_stop_limit", "amend_increase", "amend_resume"):
        time = day + " 10:00:00"
        current = next(p for p in task["quotes"] if p["time"] == time)
        change = {"time": time, "price": str(Decimal(current["ask"]) + Decimal(".4"))}
        if case == "amend_quantity":
            order["shares"] = 200
            change["shares"] = 100
        if case == "amend_increase":
            change["shares"] = 200
        if case == "amend_stop":
            order.update(kind="stop", price="499.8")
        order["amendments"] = [change]
    if case == "amend_trigger":
        time = day + " 10:00:00"
        current = next(p for p in task["quotes"] if p["time"] == time)
        bound = str(Decimal(current["ask"]) + Decimal(".4"))
        order.update(kind="stop_limit", trigger="499.8", price="499.8",
                     amendments=[{"time": time, "trigger": bound, "price": bound}])
    if case in ("amend_partial", "amend_resume"):
        order.update(shares=200, price=str(ask + 10), after_partial={"price": str(bid - 20)})
    if case in ("gtd_partial", "day_partial"):
        # Leave a remainder on all engines despite LEAN's next-observation activation.
        point = task["quotes"][-2]
        order.update(time=point["time"], price=str(Decimal(point["ask"]) + 10), shares=200,
                     after_partial={"price": str(Decimal(point["bid"]) - 20)})
    if case.startswith(("gtd_", "day_")):
        expiry = day + (" 10:00:00" if case == "gtd_unfilled" else " 16:00:00")
        order.update(tif="DAY" if case.startswith("day") else "GTD", expiry=expiry, cancel=None)
    if case == "gtd_boundary":
        order.update(price=str(ask + 10), shares=200, expiry=day + " 09:31:00")
    if case == "gtc_open":
        order["cancel"] = None
    return task
