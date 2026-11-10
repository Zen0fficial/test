"""Converter-owned persistent orders backed by Qlib's native Exchange and Account.

Qlib 0.9.7 Order has no limit/stop/status fields. This explicit lifecycle adapter
retains native lot/cash/inventory clipping and account debits for each child.
It does not claim that Qlib itself has native persistent limit or stop orders.
"""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
import pandas as pd
from qlib.backtest.account import Account
from qlib.backtest.decision import Order
from qlib.backtest.exchange import Exchange

from .lifecycle import OrderFees, validate
from .paper_common import SYMBOL
from .paper_qlib import HKExchange


class LifecycleExchange(HKExchange):
    """Use native clipping/account updates with an explicit selected execution price."""

    def __init__(self, frame: pd.DataFrame) -> None:
        """Initialize real quote data and independent intraday parent fee state."""
        self.selected_price: float | None = None
        self.parent = ""
        self.fees = OrderFees()
        super().__init__(frame)

    def get_deal_price(self, *args: object, **kwargs: object) -> object:
        """Expose the bounded lifecycle price through the native execution hook."""
        return self.selected_price if self.selected_price is not None else super().get_deal_price(*args, **kwargs)

    def _calc_trade_info_by_order(self, order: Order, position: object,
                                  dealt_order_amount: dict) -> tuple[float, float, float]:
        """Let Qlib clip, reserve incremental parent fees, then commit only real fills."""
        price, _, _ = Exchange._calc_trade_info_by_order(self, order, position, dealt_order_amount)
        quantity = int(order.deal_amount)
        while quantity:
            notional = Decimal(str(price)) * quantity
            fee = self.fees.preview(self.parent, notional)
            if order.direction == Order.SELL or notional + fee <= Decimal(str(position.get_cash())):
                order.deal_amount = quantity
                self.fees.charge(self.parent, notional)
                return price, float(notional), float(fee)
            quantity -= 100
        order.deal_amount = 0
        return price, 0., 0.


def amend(spec: dict[str, object], state: dict[str, object], change: dict[str, object],
          time: str, events: list[dict[str, object]]) -> None:
    """Change the remaining parent instruction without resetting executions or fees."""
    if (state["status"] in ("FILLED", "CANCELED", "EXPIRED")
            or ("shares" in change and change["shares"] <= state["filled"])):
        raise ValueError("Amendment must leave positive remaining quantity on an open order; otherwise cancel")
    fields = {k: v for k, v in change.items() if k != "time"}
    spec.update(fields)
    events.append({"type": "amend", "id": spec["id"], "time": time, **fields})


def expire(specs: dict[str, dict[str, object]], states: dict[str, dict[str, object]],
           time: str, events: list[dict[str, object]]) -> None:
    """Expire before matching at the cutoff; never synthesize a closing quote."""
    for key, state in states.items():
        spec = specs[key]
        if (spec.get("tif", "GTC") != "GTC" and time >= spec["expiry"]
                and state["status"] not in ("FILLED", "CANCELED", "EXPIRED")):
            state["status"] = "EXPIRED"
            events.append({"type": "expire", "id": key, "time": spec["expiry"]})


def match(spec: dict[str, object], state: dict[str, object], point: dict[str, object],
          capacity: dict[str, int], exchange: LifecycleExchange, account: Account,
          events: list[dict[str, object]], *, aggressive: bool = False) -> None:
    """Execute an eligible remainder through Qlib's native exchange and account."""
    if state["status"] in ("FILLED", "CANCELED", "EXPIRED"):
        return
    time, key, buy = point["time"], spec["id"], spec["side"] == "buy"
    quote, bound = Decimal(point["ask" if buy else "bid"]), Decimal(spec["price"])
    trigger = Decimal(spec.get("trigger", spec["price"]))
    if spec["kind"] in ("stop", "stop_limit") and (quote >= trigger if buy else quote <= trigger):
        state["triggered"] = True
    eligible = state["triggered"] if spec["kind"] == "stop" else (quote <= bound if buy else quote >= bound)
    if spec["kind"] == "stop_limit":
        eligible = eligible and state["triggered"]
    quantity = min(spec["shares"] - state["filled"], capacity[spec["side"]]) if eligible else 0
    if not quantity:
        return
    price = bound if spec["kind"] == "limit" and time > spec["time"] and not aggressive else quote
    exchange.selected_price, exchange.parent = float(price), key
    order = Order(SYMBOL, quantity, Order.BUY if buy else Order.SELL,
                  start_time=pd.Timestamp(time), end_time=pd.Timestamp(time))
    _, fee, actual_price = exchange.deal_order(order, trade_account=account)
    filled = int(order.deal_amount)
    if not filled:
        return
    state["filled"] += filled
    capacity[spec["side"]] -= filled
    state["status"] = "FILLED" if state["filled"] == spec["shares"] else "PARTIALLY_FILLED"
    events.append({"type": "fill", "id": key, "time": time, "shares": filled,
                   "price": str(actual_price), "fee": str(fee), "filled": state["filled"]})
    if spec["cancel_partial"] and state["status"] != "FILLED":
        state["status"] = "CANCELED"
        events.append({"type": "cancel", "id": key, "time": time})
    if spec.get("after_partial") and state["status"] == "PARTIALLY_FILLED":
        change = spec.pop("after_partial")
        amend(spec, state, change, time, events)


def run(task: dict[str, object], frame: pd.DataFrame) -> dict[str, object]:
    """Maintain pending orders; match quote, submit requests, then acknowledge cancels."""
    validate(task)
    exchange = LifecycleExchange(frame)
    account = Account(init_cash=100000, position_dict={}, port_metr_enabled=False)
    states: dict[str, dict[str, object]] = {}
    events: list[dict[str, object]] = []
    specs = {o["id"]: deepcopy(o) for o in task["orders"]}
    for point in task["quotes"]:
        capacity = {"buy": 100, "sell": 100}
        time = point["time"]
        expire(specs, states, time, events)
        # Old resting orders match before new commands on the same observation.
        pending = list(states)
        for key, spec in specs.items():
            if spec["time"] == time:
                states[key] = {"status": "ACCEPTED", "filled": 0, "triggered": False}
                pending.append(key)
        for key in pending:
            match(specs[key], states[key], point, capacity, exchange, account, events)
        for key, state in states.items():
            for change in specs[key].get("amendments", []):
                if time == change["time"]:
                    amend(specs[key], state, change, time, events)
                    match(specs[key], state, point, capacity, exchange, account, events, aggressive=True)
            if time == specs[key]["cancel"] and state["status"] not in ("FILLED", "CANCELED", "EXPIRED"):
                state["status"] = "CANCELED"
                events.append({"type": "cancel", "id": key, "time": time})
    expire(specs, states, task["day"] + " 16:00:00", events)
    return {"platform": "qlib", "events": events, "orders": states, "failures": [],
            "cash": str(account.current_position.get_cash()),
            "holding": account.current_position.get_stock_amount(SYMBOL), "real_orders_enabled": False}
