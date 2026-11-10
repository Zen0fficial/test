"""Explicit offline order-lifecycle scenarios over real HK minute-open observations.

All prices are causal at submission. Displayed liquidity (100 shares per side
per observed minute) is a simulation assumption, not reconstructed market depth.
The intraday fee convention aggregates notional per parent order, charging only
the incremental difference; it is not a claim about individual broker invoices.
"""

from __future__ import annotations

from decimal import Decimal
import pandas as pd

from .costs import tiered_fee
from .paper_common import execution_price

CASES = ("limit_partial", "partial_cancel", "limit_cancel", "limit_resting",
         "stop_buy", "stop_cancel", "limit_sell", "stop_sell")


class OrderFees:
    """Reserve one commission minimum and rounded cumulative charges per order."""

    def __init__(self) -> None:
        """Begin a fresh intraday account without cross-run fee state."""
        self.notionals: dict[str, Decimal] = {}
        self.charged: dict[str, Decimal] = {}

    def preview(self, identity: str, notional: Decimal) -> Decimal:
        """Return the next fill's incremental debit without mutating state."""
        if not isinstance(notional, Decimal) or not notional.is_finite() or notional <= 0:
            raise ValueError("Incremental fill notional must be a positive finite Decimal")
        return tiered_fee(self.notionals.get(identity, Decimal(0)) + notional).total - self.charged.get(
            identity, Decimal(0))

    def charge(self, identity: str, notional: Decimal) -> Decimal:
        """Advance only after a fill is actually committed by the simulation."""
        fee = self.preview(identity, notional)
        self.notionals[identity] = self.notionals.get(identity, Decimal(0)) + notional
        self.charged[identity] = self.charged.get(identity, Decimal(0)) + fee
        return fee


def quotes(frame: pd.DataFrame) -> list[dict[str, object]]:
    """Preserve observed times and derive adverse five-bps, tick-rounded quotes."""
    return [{"time": str(time), "bid": str(execution_price(Decimal(str(row.open)), "sell")),
             "ask": str(execution_price(Decimal(str(row.open)), "buy"))}
            for time, row in frame.iterrows()]


def scenario(frame: pd.DataFrame, case: str) -> dict[str, object]:
    """Build a deterministic task using only quotes available at each submission."""
    if case not in CASES:
        raise ValueError("Unknown offline lifecycle case")
    points = quotes(frame)
    day = str(frame.index[0].date())
    start = points[0]
    sell = case.endswith("sell")
    observed = next(p for p in points if p["time"] == day + " 10:00:00") if sell else start
    ask, bid = Decimal(observed["ask"]), Decimal(observed["bid"])
    orders = []
    if sell:
        orders.append({"id": "seed", "time": start["time"], "side": "buy", "kind": "limit",
                       "shares": 200, "price": str(Decimal(start["ask"]) + 10),
                       "cancel": day + " 09:59:00", "cancel_partial": False})
    kind = "stop" if case.startswith("stop") else "limit"
    price = (bid - Decimal(".4") if sell else ask + Decimal(".4")) if kind == "stop" else (
        bid + Decimal(".4") if sell else ask + Decimal(".4"))
    if case == "limit_resting":
        price = bid - Decimal(".4")
    if case == "limit_cancel":
        price = bid - 20
    if case == "stop_cancel":
        price = Decimal("499.8")
    orders.append({"id": "target", "time": observed["time"], "side": "sell" if sell else "buy", "kind": kind,
                   "shares": 200 if case in ("limit_partial", "partial_cancel", "limit_sell") else 100,
                   "price": str(price), "cancel": day + (" 09:31:00" if case in ("limit_cancel", "stop_cancel")
                                                          else " 15:29:00"),
                   "cancel_partial": case == "partial_cancel"})
    return {"case": case, "day": day, "quotes": points, "orders": orders}


def validate(task: dict[str, object]) -> None:
    """Reject malformed orders, duplicate identities and out-of-session requests."""
    times = [p["time"] for p in task["quotes"]]
    if len(times) != len(set(times)) or times != sorted(times):
        raise ValueError("Quotes must be unique and chronological")
    for point in task["quotes"]:
        bid, ask = Decimal(point["bid"]), Decimal(point["ask"])
        if (not bid.is_finite() or not ask.is_finite() or not 200 < bid <= ask <= 500
                or bid % Decimal(".2") or ask % Decimal(".2")):
            raise ValueError("Invalid simulated HK bid/ask quote")
    identities = set()
    for order in task["orders"]:
        kinds = ("limit", "stop", "stop_limit") if task.get("extended") else ("limit", "stop")
        if (order["id"] in identities or order["kind"] not in kinds
                or order["side"] not in ("buy", "sell") or type(order["shares"]) is not int
                or order["shares"] <= 0 or order["shares"] % 100
                or order["time"] not in times
                or (order["cancel"] is not None and (order["cancel"] not in times
                                                      or order["cancel"] < order["time"]))):
            raise ValueError("Invalid offline lifecycle order")
        price = Decimal(order["price"])
        if not price.is_finite() or not 200 < price <= 500 or price % Decimal(".2"):
            raise ValueError("Lifecycle limit/trigger must be a supported HK tick")
        if order["kind"] == "stop_limit":
            trigger = Decimal(order["trigger"])
            if not trigger.is_finite() or not 200 < trigger <= 500 or trigger % Decimal(".2"):
                raise ValueError("Invalid stop-limit trigger")
        tif = order.get("tif", "GTC")
        close = task["day"] + " 16:00:00"
        if tif not in ("GTC", "DAY", "GTD"):
            raise ValueError("Only GTC, DAY and intraday timestamp GTD are supported")
        if tif != "GTC":
            expiry = order.get("expiry")
            if (not expiry or not order["time"] < expiry <= close
                    or (expiry not in times and expiry != close) or (tif == "DAY" and expiry != close)):
                raise ValueError("Expiry must be a later observed instant or the HK session close")
        changes = order.get("amendments", [])
        if [c["time"] for c in changes] != sorted({c["time"] for c in changes}):
            raise ValueError("Amendments must have unique chronological times")
        for change in [*changes, *([order["after_partial"]] if order.get("after_partial") else [])]:
            if not set(change) <= {"time", "price", "trigger", "shares"} or not set(change) - {"time"}:
                raise ValueError("Unsupported or empty amendment")
            if "time" in change and (change["time"] not in times or change["time"] <= order["time"]
                                     or (order["cancel"] and change["time"] >= order["cancel"])
                                     or (tif != "GTC" and change["time"] >= order["expiry"])):
                raise ValueError("Amendment must follow submission and precede cancellation/expiry")
            if "shares" in change and (type(change["shares"]) is not int
                                       or change["shares"] <= 0 or change["shares"] % 100):
                raise ValueError("Amended total quantity must be positive whole lots")
            for field in ("price", "trigger"):
                if field in change:
                    value = Decimal(change[field])
                    if not value.is_finite() or not 200 < value <= 500 or value % Decimal(".2"):
                        raise ValueError("Amended prices must remain supported HK ticks")
                    if field == "trigger" and order["kind"] != "stop_limit":
                        raise ValueError("A separate trigger field belongs only to stop-limit orders")
        identities.add(order["id"])
