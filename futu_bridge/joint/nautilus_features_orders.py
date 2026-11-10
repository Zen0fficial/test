"""Native offline IOC/FOK, trailing-stop and overnight GTC acceptance cases.

Only the native BacktestEngine is constructed. Real source opens drive declared
derived quotes and assumed 100-share depth; no brokerage routing is available.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
from pathlib import Path

import pandas as pd
from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.config import BacktestEngineConfig, LoggingConfig
from nautilus_trader.model.currencies import HKD
from nautilus_trader.model.data import BookOrder, OrderBookDelta, OrderBookDeltas, QuoteTick
from nautilus_trader.model.enums import AccountType, BookAction, BookType, OmsType, OrderSide, TimeInForce
from nautilus_trader.model.identifiers import ClientOrderId, InstrumentId, Symbol, Venue
from nautilus_trader.model.instruments import Equity
from nautilus_trader.model.objects import Money, Price, Quantity

from .lifecycle import quotes
from .lifecycle_nautilus import CumulativeFees, LifecycleStrategy, StopLimitQuotes
from .paper_common import utc_ns
from .qlib_rl_data import ExecutionData
from .session import write_report

CASES = ("ioc_full", "ioc_partial", "ioc_unfilled", "fok_full", "fok_partial", "fok_unfilled",
         "trailing_buy", "trailing_sell")


def task_for(frame: pd.DataFrame, case: str) -> dict[str, object]:
    """Create causally priced, prescribed native order instructions from observed quotes."""
    if case not in (*CASES, "overnight_partial", "overnight_unfilled"):
        raise ValueError("Unknown native order acceptance case")
    points = quotes(frame)
    overnight = case.startswith("overnight")
    day = str(frame.index[0].date())
    point = next(p for p in points if p["time"] == day + " 15:59:00") if overnight else points[0]
    side = "sell" if case == "trailing_sell" else "buy"
    orders = []
    if side == "sell":
        orders.append({"id": "seed", "kind": "market", "side": "buy", "shares": 100,
                       "time": points[0]["time"], "cancel": day + " 09:59:00", "cancel_partial": False})
        point = next(p for p in points if p["time"] == day + " 10:00:00")
    quantity = 200 if case.endswith("partial") else 100
    price = Decimal(point["bid"]) - 20 if case.endswith("unfilled") else Decimal(point["ask"]) + 10
    orders.append({"id": "target", "kind": "trailing" if case.startswith("trailing") else "limit",
                   "side": side, "shares": quantity, "price": str(price), "time": point["time"],
                   "cancel": str(frame.index[-1].date()) + " 15:29:00", "cancel_partial": False,
                   "tif": "IOC" if case.startswith("ioc") else "FOK" if case.startswith("fok") else "GTC",
                   "trigger": str(Decimal(point["bid" if side == "sell" else "ask"])
                                  + (Decimal("-.4") if side == "sell" else Decimal(".4")))})
    return {"case": case, "quotes": points, "orders": orders}


class ExtraOrders(LifecycleStrategy):
    """Use native factories for additional order kinds while retaining tested event/account hooks."""

    def on_quote_tick(self, tick: QuoteTick) -> None:
        """Submit only prescribed offline orders and cancel any end-of-case remainder."""
        time = self.stamp(tick.ts_event)
        for key, spec in self.specs.items():
            if time == spec["time"]:
                common = (self.instrument, OrderSide.BUY if spec["side"] == "buy" else OrderSide.SELL,
                          Quantity.from_int(spec["shares"]))
                if spec["kind"] == "market":
                    order = self.order_factory.market(*common, client_order_id=ClientOrderId(key))
                elif spec["kind"] == "trailing":
                    order = self.order_factory.trailing_stop_market(*common, trailing_offset=Decimal(".4"),
                        trigger_price=Price.from_str(format(Decimal(spec["trigger"]), ".1f")),
                        client_order_id=ClientOrderId(key))
                else:
                    order = self.order_factory.limit(*common,
                        price=Price.from_str(format(Decimal(spec["price"]), ".1f")),
                        time_in_force=TimeInForce[spec["tif"]], client_order_id=ClientOrderId(key))
                self.orders[key] = order
                self.submit_order(order)
            if time == spec["cancel"] and key in self.orders and not self.orders[key].is_closed:
                self.cancel_order(self.orders[key])


def execute(task: dict[str, object]) -> dict[str, object]:
    """Run prescribed native orders on finite derived L2 snapshots across observed sessions."""
    venue, identity = Venue("HKEXTRA"), InstrumentId.from_str("00700.HKEXTRA")
    instrument = Equity(identity, Symbol("00700"), HKD, 1, Price.from_str(".2"), Quantity.from_int(100), 0, 0)
    engine = BacktestEngine(BacktestEngineConfig(logging=LoggingConfig(bypass_logging=True)))
    try:
        fill_model = StopLimitQuotes()
        engine.add_venue(venue, oms_type=OmsType.NETTING, account_type=AccountType.CASH,
            base_currency=HKD, starting_balances=[Money(100000, HKD)], book_type=BookType.L2_MBP,
            liquidity_consumption=True, fee_model=CumulativeFees(), fill_model=fill_model)
        engine.add_instrument(instrument)
        books, ticks = [], []
        for point in task["quotes"]:
            stamp = utc_ns(point["time"])
            bid, ask = (Price.from_str(format(Decimal(point[k]), ".1f")) for k in ("bid", "ask"))
            size = Quantity.from_int(100)
            books.append(OrderBookDeltas(identity, [
                OrderBookDelta(identity, BookAction.CLEAR, None, 32, 0, stamp, stamp),
                OrderBookDelta(identity, BookAction.ADD, BookOrder(OrderSide.BUY, bid, size, 1),
                               32, 0, stamp, stamp),
                OrderBookDelta(identity, BookAction.ADD, BookOrder(OrderSide.SELL, ask, size, 2),
                               160, 0, stamp, stamp)]))
            ticks.append(QuoteTick(identity, bid, ask, size, size, stamp + 1, stamp + 1))
        engine.add_data(books)
        engine.add_data(ticks)
        strategy = ExtraOrders(identity, task, fill_model)
        engine.add_strategy(strategy)
        fill_model.clock = strategy.clock
        engine.run()
        return {"events": strategy.events, "failures": strategy.failures, "native_notices": strategy.native_notices,
                "orders": {k: {"status": o.status.name, "filled": int(o.filled_qty.as_decimal())}
                           for k, o in strategy.orders.items()},
                "cash": str(engine.cache.account_for_venue(venue).balance_total(HKD).as_decimal()),
                "holding": sum(p.signed_qty for p in engine.cache.positions_open()), "real_orders_enabled": False}
    finally:
        engine.dispose()


def audit(task: dict[str, object], actual: dict[str, object], frame: pd.DataFrame) -> dict[str, object]:
    """Enumerate independent native IOC/FOK/trailing/GTC semantics from real open prices."""
    if actual.get("real_orders_enabled") is not False or actual["failures"] or actual["native_notices"]:
        raise ValueError("Native additional order acceptance failed")
    source = {}
    for time, row in frame.iterrows():
        raw = Decimal(str(row.open))
        source[str(time)] = {
            "buy": (raw * Decimal("1.0005") / Decimal(".2")).to_integral_value(
                rounding=ROUND_CEILING) * Decimal(".2"),
            "sell": (raw * Decimal(".9995") / Decimal(".2")).to_integral_value(
                rounding=ROUND_FLOOR) * Decimal(".2")}
    expected, final = [], {}
    for order in task["orders"]:
        key, side = order["id"], order["side"]
        eligible = [t for t in source if order["time"] <= t <= order["cancel"]]
        selected = []
        if order["kind"] == "market":
            selected = [order["time"]]
        elif order["kind"] == "trailing":
            trigger = Decimal(order["trigger"])
            for time in eligible[1:]:
                quote = source[time][side]
                if quote >= trigger if side == "buy" else quote <= trigger:
                    selected = [time]
                    break
                trigger = min(trigger, quote + Decimal(".4")) if side == "buy" else max(
                    trigger, quote - Decimal(".4"))
        else:
            bound = Decimal(order["price"])
            crossing = [t for t in eligible if source[t][side] <= bound]
            if order["tif"] in ("IOC", "FOK"):
                crossing = [t for t in crossing if t == order["time"]]
                if order["tif"] == "FOK" and order["shares"] > 100:
                    crossing = []
            selected = crossing[:order["shares"] // 100]
        notional, charged = Decimal(0), Decimal(0)
        for i, time in enumerate(selected):
            price = source[time][side]
            if order["kind"] == "limit" and time != order["time"]:
                price = Decimal(order["price"])
            notional += price * 100
            total = (max(Decimal(18), notional * Decimal(".0005")) + notional * Decimal(".000127")
                     + (notional * Decimal(".001")).to_integral_value(rounding=ROUND_CEILING)).quantize(
                         Decimal(".01"), rounding=ROUND_HALF_UP)
            expected.append({"id": key, "time": time, "type": "fill", "shares": 100,
                             "filled": (i + 1) * 100, "price": str(price), "fee": str(total - charged)})
            charged = total
        filled = len(selected) * 100
        status = "FILLED" if filled == order["shares"] else "CANCELED"
        final[key] = {"filled": filled, "status": status}
        if status == "CANCELED":
            when = order["time"] if order.get("tif") in ("IOC", "FOK") else order["cancel"]
            expected.append({"id": key, "time": when, "type": "cancel"})
    expected.sort(key=lambda e: e["time"])
    if len(expected) != len(actual["events"]) or final != actual["orders"]:
        raise ValueError(f"Native additional state/count mismatch: {task['case']} {actual} != {expected}, {final}")
    cash, holding = Decimal(100000), 0
    sides = {o["id"]: o["side"] for o in task["orders"]}
    for event, target in zip(actual["events"], expected):
        for field, value in target.items():
            equal = (Decimal(str(event[field])) == Decimal(str(value)) if field in
                     ("shares", "filled", "price", "fee") else event[field] == value)
            if not equal:
                raise ValueError(f"Native additional event mismatch {task['case']}: {event} != {target}")
        if target["type"] == "fill":
            sign = 1 if sides[target["id"]] == "buy" else -1
            cash -= sign * 100 * Decimal(target["price"]) + Decimal(target["fee"])
            holding += sign * 100
            if cash < 0 or holding < 0:
                raise ValueError("Native additional orders exceeded funded long-only balances")
    if cash != Decimal(actual["cash"]) or holding != actual["holding"]:
        raise ValueError("Native additional final account mismatch")
    return {"passed": True, "fills": sum(e["type"] == "fill" for e in expected),
            "cancels": sum(e["type"] == "cancel" for e in expected), "cash": str(cash), "holding": holding}


def run_orders(data: ExecutionData, output: Path) -> dict[str, object]:
    """Test extra native orders on five held-out sessions and four overnight boundaries."""
    output.mkdir(parents=True, exist_ok=False)
    results = {}
    days = data.partitions["test"]
    for index, day in enumerate(days):
        selected = list(CASES)
        if index < len(days) - 1:
            selected += ["overnight_partial", "overnight_unfilled"]
        for case in selected:
            frame = data.minutes.loc[day:days[index + 1]] if case.startswith("overnight") else data.minutes.loc[day]
            task = task_for(frame, case)
            native = execute(task)
            key = day + "_" + case
            write_report(output / (key + ".json"), {"task": task, "native": native})
            results[key] = audit(task, native, frame)
    report = {"passed": True, "cases": results, "scenarios": len(results), "real_orders_enabled": False,
              "limits": ["Native single-process GTC persistence, not restart recovery",
                         "PRICE trailing offset .4 HKD; no percentage/activation-price matrix",
                         "Derived five-bps quotes and 100-share capacity are explicit simulation assumptions",
                         "Parent fee aggregation across the overnight case is not broker cross-day invoice fidelity"]}
    write_report(output / "report.json", report)
    return report
