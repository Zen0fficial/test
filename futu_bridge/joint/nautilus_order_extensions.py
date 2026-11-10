"""Bounded native PARTIAL OTO brackets and observable replay state.

The public bracket factory gives every child the full parent quantity. In the
pinned cash-account simulator this is rejected before its native resize pass
when only one lot has filled. Initialize children at the declared minimum fill
lot instead; native OTO activation and subsequent reduce-only resizing remain
authoritative. This adapter requires the explicit integral-lot liquidity model.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pandas as pd
from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.backtest.models import LatencyModel
from nautilus_trader.config import BacktestEngineConfig, LoggingConfig
from nautilus_trader.model.currencies import HKD
from nautilus_trader.model.data import BookOrder, CustomData, DataType, OrderBookDelta, OrderBookDeltas, QuoteTick
from nautilus_trader.model.enums import (
    AccountType, BookAction, BookType, ContingencyType, OmsType, OrderSide, OrderType, OtoTriggerMode,
)
from nautilus_trader.model.events import OrderInitialized
from nautilus_trader.model.identifiers import ClientId, ClientOrderId, InstrumentId, Symbol, Venue
from nautilus_trader.model.instruments import Equity
from nautilus_trader.model.objects import Money, Price, Quantity
from nautilus_trader.model.orders import OrderList
from nautilus_trader.model.orders.unpacker import OrderUnpacker

from .lifecycle_nautilus import CumulativeFees, SessionClock
from .nautilus_features_contingent import ContingentStrategy, ExpectedLedger, make_task, source_quotes
from .paper_common import capture, utc_ns
from .session import write_report


def price(value: object) -> Price:
    """Create the simulator's one-decimal HK price without float round trips."""
    return Price.from_str(format(Decimal(str(value)), ".1f"))


class PartialBracketStrategy(ContingentStrategy):
    """Use native linked children with bounded initial size and record exposure checks."""

    def __init__(self, identity: InstrumentId, task: dict[str, object]) -> None:
        """Extend the native journal with deterministic command and exposure state."""
        super().__init__(identity, task)
        self.commands: list[dict[str, object]] = []
        self.exposure: list[dict[str, object]] = []

    def on_start(self) -> None:
        """Subscribe native cache books as well as quotes so recovery compares complete market state."""
        super().on_start()
        self.subscribe_order_book_deltas(self.identity, book_type=BookType.L2_MBP)

    def on_quote_tick(self, tick: QuoteTick) -> None:
        """Construct partial-mode brackets or delegate existing command cases unchanged."""
        if self.task.get("partial_oto") is not True:
            super().on_quote_tick(tick)
            if self.task["case"] == "timer_cancel" and self.stamp(tick.ts_event) == self.task["submit"]:
                self.clock.set_time_alert_ns("CANCEL_ENTRY", tick.ts_event + 30_000_000_000, self.cancel_timer)
            return
        time = self.stamp(tick.ts_event)
        if time == self.task["submit"]:
            bracket = self.order_factory.bracket(
                self.identity, OrderSide.BUY, Quantity.from_int(self.task["shares"]),
                contingency_type=ContingencyType.OUO, entry_order_type=OrderType.LIMIT,
                entry_price=price(self.task["entry"]), tp_price=price(self.task["tp"]), tp_post_only=False,
                sl_trigger_price=price(self.task["sl"]), entry_client_order_id=ClientOrderId("ENTRY"),
                tp_client_order_id=ClientOrderId("TP"), sl_client_order_id=ClientOrderId("SL"))
            bounded = []
            for order in bracket.orders:
                if order.parent_order_id is not None:
                    initial = OrderInitialized.to_dict(order.init_event)
                    initial["quantity"] = "100"
                    order = OrderUnpacker.unpack(initial)
                key = str(order.client_order_id)
                self.orders[key] = order
                self.links[key] = {"parent": str(order.parent_order_id) if order.parent_order_id else None,
                    "linked": [str(x) for x in order.linked_order_ids or []],
                    "contingency": order.contingency_type.name, "list": str(order.order_list_id),
                    "reduce_only": order.is_reduce_only}
                bounded.append(order)
            self.commands.append({"type": "submit_bracket", "ns": tick.ts_event, "parent": 200, "child": 100})
            self.submit_order_list(OrderList(bracket.id, bounded))
        if time == self.task["modify"] and not self.orders["TP"].is_closed:
            self.commands.append({"type": "amend", "id": "TP", "ns": tick.ts_event,
                                  "price": self.task["modified_tp"]})
            self.modify_order(self.orders["TP"], price=price(self.task["modified_tp"]))
        if time == self.task["cancel"]:
            for order in self.orders.values():
                if not order.is_closed:
                    self.commands.append({"type": "cancel", "id": str(order.client_order_id), "ns": tick.ts_event})
                    self.cancel_order(order)
                    break

    def cancel_timer(self, event: object) -> None:
        """Journal a native virtual-clock alert and enqueue its simulated cancellation."""
        self.events.append({"type": "timer", "id": "CANCEL_ENTRY", "ns": event.ts_event,
                            "time": self.stamp(event.ts_event)})
        order = self.orders["ENTRY"]
        if not order.is_closed:
            self.commands.append({"type": "cancel", "id": "ENTRY", "ns": event.ts_event})
            self.cancel_order(order)

    def on_order_accepted(self, event: object) -> None:
        """Verify each child reaches native matching with no excess inventory exposure."""
        self.check_exposure(event)

    def on_order_updated(self, event: object) -> None:
        """Retain native child growth/OUO changes and independently check their inventory bounds."""
        super().on_order_updated(event)
        self.check_exposure(event)

    def check_exposure(self, event: object) -> None:
        """Reject an accepted child whose remaining quantity exceeds the current long position."""
        order = self.orders.get(str(event.client_order_id))
        if order is None or order.parent_order_id is None:
            return
        holding = sum(p.signed_qty for p in self.cache.positions_open())
        leaves = int(order.leaves_qty.as_decimal())
        record = {"id": str(event.client_order_id), "ns": event.ts_event, "leaves": leaves,
                  "holding": holding, "parent_filled": int(self.orders["ENTRY"].filled_qty.as_decimal())}
        self.exposure.append(record)
        if leaves > holding or holding < 0:
            self.failures.append("Native child exposure exceeds filled long inventory: " + str(record))


def build(task: dict[str, object]) -> tuple[BacktestEngine, PartialBracketStrategy, CumulativeFees]:
    """Construct one deterministic cash engine; all observations are supplied by its caller."""
    venue, identity = Venue("HKCONTINGENT"), InstrumentId.from_str("00700.HKCONTINGENT")
    engine = BacktestEngine(BacktestEngineConfig(logging=LoggingConfig(bypass_logging=True)))
    fees = CumulativeFees()
    engine.add_venue(venue, oms_type=OmsType.NETTING, account_type=AccountType.CASH,
        base_currency=HKD, starting_balances=[Money(100000, HKD)], book_type=BookType.L2_MBP,
        liquidity_consumption=True, fee_model=fees, support_contingent_orders=True,
        oto_trigger_mode=OtoTriggerMode.PARTIAL if task.get("partial_oto") else OtoTriggerMode.FULL,
        latency_model=LatencyModel(base_latency_nanos=0,
            insert_latency_nanos=int(task["insert_delay"]) * 1_000_000_000,
            update_latency_nanos=int(task.get("update_delay", 0)) * 1_000_000_000,
            cancel_latency_nanos=int(task["cancel_delay"]) * 1_000_000_000))
    engine.add_instrument(Equity(identity, Symbol("00700"), HKD, 1, price(".2"), Quantity.from_int(100), 0, 0))
    strategy = PartialBracketStrategy(identity, task)
    engine.add_strategy(strategy)
    return engine, strategy, fees


def observations(task: dict[str, object]) -> list[object]:
    """Build real-price observations and clock-only latency delivery events in stable time order."""
    identity = InstrumentId.from_str("00700.HKCONTINGENT")
    values = []
    for point in task["quotes"]:
        stamp, size = utc_ns(point["time"]), Quantity.from_int(100)
        bid, ask = price(point["bid"]), price(point["ask"])
        values.append(OrderBookDeltas(identity, [
            OrderBookDelta(identity, BookAction.CLEAR, None, 32, 0, stamp, stamp),
            OrderBookDelta(identity, BookAction.ADD, BookOrder(OrderSide.BUY, bid, size, 1), 32, 0, stamp, stamp),
            OrderBookDelta(identity, BookAction.ADD, BookOrder(OrderSide.SELL, ask, size, 2), 160, 0, stamp, stamp)]))
        values.append(QuoteTick(identity, bid, ask, size, size, stamp + 1, stamp + 1))
    for origin in (task["submit"], task["modify"]):
        for seconds in (1, 30, 61, 91, 120):
            stamp = utc_ns(origin) + seconds * 1_000_000_000 + 1
            values.append(CustomData(DataType(SessionClock), SessionClock(stamp)))
    return sorted(values, key=lambda x: x.ts_init)


def feed(engine: BacktestEngine, values: list[object]) -> None:
    """Feed one immutable batch without stopping native engines, timers or command queues."""
    if not values:
        raise ValueError("Recovery batches must contain observations")
    for kind in (OrderBookDeltas, QuoteTick, CustomData):
        batch = [x for x in values if isinstance(x, kind)]
        if batch:
            if kind is CustomData:
                engine.add_data(batch, client_id=ClientId("CLOCK"))
            else:
                engine.add_data(batch)
    engine.run(streaming=True)
    engine.clear_data()


def state(engine: BacktestEngine, strategy: PartialBracketStrategy, fees: CumulativeFees) -> dict[str, object]:
    """Export deterministic economic state; random event UUIDs and wall-clock timings are excluded."""
    venue = Venue("HKCONTINGENT")
    book = engine.cache.order_book(strategy.identity)
    positions = [{"id": str(p.id), "signed_qty": p.signed_qty, "avg_open": p.avg_px_open,
                  "realized_pnl": str(p.realized_pnl), "is_closed": p.is_closed}
                 for p in sorted(engine.cache.positions(), key=lambda x: str(x.id))]
    account = engine.cache.account_for_venue(venue)
    balance = account.balance(HKD)
    return {"events": strategy.events, "updates": strategy.updates, "links": strategy.links,
        "cancel_rejections": strategy.rejections, "failures": strategy.failures, "commands": strategy.commands,
        "exposure": strategy.exposure,
        "orders": {k: {"status": o.status.name, "filled": int(o.filled_qty.as_decimal()),
                       "shares": int(o.quantity.as_decimal()), "leaves": int(o.leaves_qty.as_decimal()),
                       "price": str(o.price) if o.has_price else None,
                       "trigger": str(o.trigger_price) if o.has_trigger_price else None}
                   for k, o in strategy.orders.items()},
        "cash": str(balance.total.as_decimal()), "cash_free": str(balance.free.as_decimal()),
        "cash_locked": str(balance.locked.as_decimal()), "positions": positions,
        "holding": sum(p.signed_qty for p in engine.cache.positions_open()),
        "book": {"bid": str(book.best_bid_price()), "ask": str(book.best_ask_price()),
                 "bid_size": str(book.best_bid_size()), "ask_size": str(book.best_ask_size())},
        "fees": {"notional": {k: str(v) for k, v in fees.fees.notionals.items()},
                 "charged": {k: str(v) for k, v in fees.fees.charged.items()}},
        "timers": strategy.clock.timer_names,
        "real_orders_enabled": False}


def partial_task(frame: pd.DataFrame, cancel_parent: bool = False, delayed_amend: bool = False) -> dict[str, object]:
    """Declare a 200-share parent with native partial child activation under integral-lot fills."""
    task = make_task(frame, "ouo_partial_exit")
    task["partial_oto"] = True
    task["case"] = "partial_parent_cancel" if cancel_parent else "partial_child_growth"
    task["update_delay"] = 61 if delayed_amend else 0
    if cancel_parent:
        task["cancel"] = task["submit"]
    return task


def execute(task: dict[str, object]) -> dict[str, object]:
    """Execute a complete native offline case while preserving state until after capture."""
    engine, strategy, fees = build(task)
    try:
        feed(engine, observations(task))
        result = state(engine, strategy, fees)
        engine.end()
        return result
    finally:
        engine.dispose()


def audit_partial(task: dict[str, object], actual: dict[str, object], frame: pd.DataFrame) -> dict[str, object]:
    """Derive fills, fees and residual inventory independently from real source prices and command times."""
    source, ledger = source_quotes(frame), ExpectedLedger()
    start = task["submit"]
    ledger.fill("ENTRY", start, source[start]["ask"], after_command=True)
    if task["case"] == "partial_parent_cancel":
        for key in ("ENTRY", "SL", "TP"):
            ledger.cancel(key, start, after_command=True)
    else:
        second = next(t for t, p in source.items() if start < t < task["modify"]
                      and p["ask"] <= Decimal(task["entry"]))
        ledger.fill("ENTRY", second, Decimal(task["entry"]))
        arrival = pd.Timestamp(task["modify"]) + pd.Timedelta(seconds=task.get("update_delay", 0))
        before = [(t, p) for t, p in source.items() if pd.Timestamp(t) <= arrival]
        observed = before[-1][1]
        if any(p["bid"] >= Decimal(task["tp"]) or p["bid"] <= Decimal(task["sl"])
               for t, p in source.items() if start < t <= str(arrival)):
            raise ValueError("Partial bracket oracle requires the distant initial exits to remain inactive")
        target, count = Decimal(task["modified_tp"]), 0
        if observed["bid"] >= target:
            ledger.fill("TP", str(arrival), observed["bid"], after_command=True)
            count += 1
        for time, quote in source.items():
            if pd.Timestamp(time) <= arrival or time > task["cancel"]:
                continue
            if quote["bid"] <= Decimal(task["sl"]):
                raise ValueError("Unexpected protective stop reached in partial bracket oracle")
            if quote["bid"] >= target:
                ledger.fill("TP", time, target)
                count += 1
                if count == 2:
                    ledger.cancel("SL", time)
                    break
        if count < 2:
            for key in ("SL", "TP"):
                ledger.cancel(key, task["cancel"], after_command=True)
    if len(ledger.events) != len(actual["events"]):
        raise ValueError("Native partial bracket event count differs from independent oracle")
    for wanted, observed in zip(ledger.events, actual["events"]):
        for key, value in wanted.items():
            same = Decimal(observed[key]) == Decimal(value) if key in ("price", "fee") else observed[key] == value
            if not same:
                raise ValueError(f"Native partial bracket differs: {observed} != {wanted}")
    if Decimal(actual["cash"]) != ledger.cash or actual["holding"] != ledger.holding:
        raise ValueError("Native partial bracket account differs from independent Decimal fee ledger")
    if actual["failures"] or actual["cancel_rejections"] or actual["real_orders_enabled"] is not False:
        raise ValueError("Unexpected native failures or routing in the partial bracket")
    links = actual["links"]
    if links["ENTRY"]["contingency"] != "OTO" or set(links["ENTRY"]["linked"]) != {"TP", "SL"}:
        raise ValueError("Partial bracket lost native OTO parent linkage")
    for child, sibling in (("TP", "SL"), ("SL", "TP")):
        if (links[child]["parent"] != "ENTRY" or links[child]["linked"] != [sibling]
                or links[child]["contingency"] != "OUO" or links[child]["reduce_only"] is not True):
            raise ValueError("Partial bracket lost native reduce-only OUO child linkage")
    return {"passed": True, "fills": sum(x["type"] == "fill" for x in ledger.events),
            "cash": str(ledger.cash), "holding": ledger.holding}


def run(response: Path, output: Path, repo: Path) -> dict[str, object]:
    """Validate native partial activation, growth, cancellation and delayed sibling amendments."""
    data = capture(response)
    output.mkdir(parents=True, exist_ok=False)
    results = {}
    for day in data.partitions["test"]:
        for case in ("growth", "cancel_parent", "delayed_amend"):
            task = partial_task(data.minutes.loc[day], case == "cancel_parent", case == "delayed_amend")
            actual = execute(task)
            oracle = audit_partial(task, actual, data.minutes.loc[day])
            if actual["failures"] or actual["holding"] < 0 or Decimal(actual["cash"]) < 0:
                raise ValueError("Partial native bracket failed: " + str(actual["failures"]))
            if not any(x["parent_filled"] == 100 and x["leaves"] == 100 for x in actual["exposure"]):
                raise ValueError("No child was accepted while the parent was only partially filled")
            if case != "cancel_parent" and not any(x["shares"] == 200 for x in actual["updates"]):
                raise ValueError("Native children did not grow after the second parent fill")
            if case == "cancel_parent" and (actual["holding"] != 100 or any(
                    x["status"] != "CANCELED" for x in actual["orders"].values())):
                raise ValueError("Whole-bracket cancellation differs from the explicit residual-position contract")
            key = day + "_" + case
            write_report(output / (key + ".json"), {"task": task, "native": actual})
            results[key] = {"passed": True, "exposure_checks": len(actual["exposure"]),
                            "holding": actual["holding"], "cash": actual["cash"],
                            "independent_oracle": oracle,
                            "protection_active": any(x["status"] == "ACCEPTED" for x in actual["orders"].values())}
    report = {"passed": True, "scenarios": len(results), "cases": results, "data": data.provenance,
        "oto_trigger_mode": "PARTIAL", "real_orders_enabled": False, "providers_modified": False,
        "limitations": ["Minimum native fill is the declared 100-share lot; fractional fills are unsupported",
                        "Canceling the complete bracket leaves its already filled inventory explicitly unprotected",
                        "Real captured opens drive assumed spread and depth; no historical order queue is inferred"]}
    write_report(output / "report.json", report)
    return report
