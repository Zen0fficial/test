"""Native offline Nautilus limit/stop/cancel execution with finite simulated depth."""

from __future__ import annotations

from decimal import Decimal
import pandas as pd
from nautilus_trader.core.data import Data
from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.backtest.models import FeeModel, FillModel
from nautilus_trader.config import BacktestEngineConfig, LoggingConfig, StrategyConfig
from nautilus_trader.model.currencies import HKD
from nautilus_trader.model.book import OrderBook
from nautilus_trader.model.data import BookOrder, CustomData, DataType, OrderBookDelta, OrderBookDeltas, QuoteTick
from nautilus_trader.model.enums import AccountType, BookAction, BookType, OmsType, OrderSide, OrderType, TimeInForce
from nautilus_trader.model.identifiers import ClientId, ClientOrderId, InstrumentId, Symbol, TraderId, Venue
from nautilus_trader.model.instruments import Equity
from nautilus_trader.model.objects import Money, Price, Quantity
from nautilus_trader.trading.strategy import Strategy

from .lifecycle import OrderFees, validate
from .paper_common import utc_ns


class SessionClock(Data):
    """Advance the engine clock without modifying the instrument's market data."""

    def __init__(self, stamp: int) -> None:
        """Set one clock-only observation after the HK close deadline."""
        self.stamp = stamp

    @property
    def ts_event(self) -> int:
        """Return the control event instant."""
        return self.stamp

    @property
    def ts_init(self) -> int:
        """Return the same control availability instant."""
        return self.stamp


class CumulativeFees(FeeModel):
    """Charge actual partial fills cumulatively inside native cash accounting."""

    def __init__(self) -> None:
        """Start fresh fee state for this engine."""
        self.fees = OrderFees()

    def get_commission(self, order: object, fill_qty: Quantity, fill_px: Price, instrument: Equity) -> Money:
        """Apply one parent minimum under the documented intraday fee convention."""
        return Money(self.fees.charge(str(order.client_order_id), fill_qty.as_decimal() * fill_px.as_decimal()), HKD)


class StopLimitQuotes(FillModel):
    """Use the supported native hook to prevent stale-trigger stop-limit price improvement.

    Native 1.231 may reuse the trigger as a fill price after a later amendment.
    This hook supplies current derived quotes and counts actual fills per minute;
    native matching, triggers, amendments, tickets and account debits remain native.
    """

    def __init__(self) -> None:
        """Keep actual executed capacity shared across all orders in this replay."""
        super().__init__()
        self.clock: object = None
        self.used: dict[tuple[int, object], int] = {}

    def record(self, event: object) -> None:
        """Consume capacity only upon an authoritative native fill callback."""
        key = (event.ts_event // 60_000_000_000, event.order_side)
        self.used[key] = self.used.get(key, 0) + int(event.last_qty.as_decimal())

    def get_orderbook_for_fill_simulation(self, instrument: Equity, order: object,
                                         best_bid: Price, best_ask: Price) -> OrderBook | None:
        """Return a bounded current-quote book only for native stop-limit matching."""
        stamp = self.clock.timestamp_ns()
        # Native matching can precede the GTD expiry check on a quote at the cutoff.
        # Suppress execution there; the native expiry acknowledgement still follows.
        if getattr(order, "expire_time_ns", 0) and stamp >= order.expire_time_ns:
            return OrderBook(instrument.id, BookType.L2_MBP)
        if order.order_type != OrderType.STOP_LIMIT:
            return None
        book = OrderBook(instrument.id, BookType.L2_MBP)
        for side, price in ((OrderSide.BUY, best_bid), (OrderSide.SELL, best_ask)):
            taker = OrderSide.SELL if side == OrderSide.BUY else OrderSide.BUY
            remaining = max(0, 100 - self.used.get((stamp // 60_000_000_000, taker), 0))
            if remaining:
                book.add(BookOrder(side, price, Quantity.from_int(remaining), int(side)), stamp)
        return book


class LifecycleStrategy(Strategy):
    """Submit native orders and cancellations solely inside the offline engine."""

    def __init__(self, instrument: InstrumentId, task: dict[str, object], fills: StopLimitQuotes) -> None:
        """Bind immutable requests before processing quotes."""
        super().__init__(StrategyConfig(strategy_id="LIFECYCLE", manage_gtd_expiry=True))
        self.instrument = instrument
        self.fills = fills
        self.specs = {c["id"]: c for c in task["orders"]}
        self.orders: dict[str, object] = {}
        self.events: list[dict[str, object]] = []
        self.failures: list[str] = []
        self.partial_amended: set[str] = set()
        self.pending_changes: dict[str, dict[str, object]] = {}
        self.native_notices: list[dict[str, object]] = []

    def stamp(self, ns: int) -> str:
        """Normalize native UTC instants to the source HK minute/second clock."""
        return str(pd.Timestamp(ns, unit="ns", tz="UTC").tz_convert("Asia/Hong_Kong").tz_localize(None).floor("s"))

    def on_start(self) -> None:
        """Subscribe to local derived quote observations."""
        self.subscribe_quote_ticks(self.instrument)

    def on_quote_tick(self, tick: QuoteTick) -> None:
        """Process requests after matching this observation against resting orders."""
        time = self.stamp(tick.ts_event)
        for key, spec in self.specs.items():
            if time == spec["time"]:
                args = [self.instrument, OrderSide.BUY if spec["side"] == "buy" else OrderSide.SELL,
                        Quantity.from_int(spec["shares"]), Price.from_str(format(Decimal(spec["price"]), ".1f"))]
                factory = {"limit": self.order_factory.limit, "stop": self.order_factory.stop_market,
                           "stop_limit": self.order_factory.stop_limit}[spec["kind"]]
                if spec["kind"] == "stop_limit":
                    args.append(Price.from_str(format(Decimal(spec["trigger"]), ".1f")))
                options = {"client_order_id": ClientOrderId(key)}
                if spec.get("tif", "GTC") != "GTC":
                    options.update(time_in_force=TimeInForce.GTD,
                                   expire_time=pd.Timestamp(spec["expiry"]).tz_localize(
                                       "Asia/Hong_Kong").tz_convert("UTC").to_pydatetime())
                order = factory(*args, **options)
                self.orders[key] = order
                self.submit_order(order)
            for change in spec.get("amendments", []):
                if time == change["time"]:
                    self.amend(key, change)
            if time == spec["cancel"] and key in self.orders and not self.orders[key].is_closed:
                self.cancel_order(self.orders[key])

    def amend(self, key: str, change: dict[str, object]) -> None:
        """Update the original native order without resetting fills or parent fees."""
        order = self.orders[key]
        if order.is_closed or ("shares" in change and change["shares"] <= int(order.filled_qty.as_decimal())):
            raise ValueError("Amendment must leave positive remaining quantity on an open order; otherwise cancel")
        args = {}
        if "shares" in change:
            args["quantity"] = Quantity.from_int(change["shares"])
        for field in ("price", "trigger"):
            if field in change:
                native = "trigger_price" if field == "trigger" or self.specs[key]["kind"] == "stop" else "price"
                args[native] = Price.from_str(format(Decimal(change[field]), ".1f"))
        self.pending_changes[key] = {k: v for k, v in change.items() if k != "time"}
        self.modify_order(order, **args)

    def on_order_updated(self, event: object) -> None:
        """Journal only acknowledged user amendments, not internal trigger updates."""
        key = str(event.client_order_id)
        change = self.pending_changes.pop(key, None)
        if change is not None:
            self.events.append({"id": key, "time": self.stamp(event.ts_event), "type": "amend", **change})

    def on_order_modify_rejected(self, event: object) -> None:
        """Retain native amendment rejections as explicit conformance failures."""
        self.failures.append(str(event))

    def on_order_filled(self, event: object) -> None:
        """Record native partial/full fills and optionally cancel the remainder."""
        key = str(event.client_order_id)
        self.fills.record(event)
        order = self.orders[key]
        self.events.append({"id": key, "time": self.stamp(event.ts_event), "type": "fill",
                            "shares": int(event.last_qty.as_decimal()), "price": str(event.last_px.as_decimal()),
                            "fee": str(event.commission.as_decimal()), "filled": int(order.filled_qty.as_decimal())})
        if self.specs[key]["cancel_partial"] and not order.is_closed:
            self.cancel_order(order)
        change = self.specs[key].get("after_partial")
        if change and not order.is_closed and key not in self.partial_amended:
            self.partial_amended.add(key)
            self.amend(key, change)

    def on_order_canceled(self, event: object) -> None:
        """Record the authoritative native cancellation acknowledgement."""
        key, time = str(event.client_order_id), self.stamp(event.ts_event)
        if self.orders[key].is_closed and self.orders[key].status.name == "FILLED":
            self.native_notices.append({"id": key, "time": time, "type": "cancel_after_fill", "status": "FILLED"})
            return
        kind = "expire" if self.specs[key].get("expiry") == time else "cancel"
        self.events.append({"id": key, "time": time, "type": kind})

    def on_order_expired(self, event: object) -> None:
        """Retain venue-originated native expiry acknowledgements."""
        self.events.append({"id": str(event.client_order_id), "time": self.stamp(event.ts_event), "type": "expire"})

    def on_order_denied(self, event: object) -> None:
        """Retain native risk failures rather than silently dropping requests."""
        self.failures.append(str(event))

    def on_order_rejected(self, event: object) -> None:
        """Retain native venue failures."""
        self.failures.append(str(event))

    def on_order_cancel_rejected(self, event: object) -> None:
        """Retain rejected cancellation requests as explicit failures."""
        self.failures.append(str(event))


def run(task: dict[str, object]) -> dict[str, object]:
    """Match native orders against refreshed 100-share L2 snapshots, never live data."""
    validate(task)
    venue = Venue("HKPAPER")
    identity = InstrumentId(Symbol("00700"), venue)
    instrument = Equity(identity, Symbol("00700"), HKD, 1, Price.from_str(".2"), Quantity.from_int(100), 0, 0)
    engine = BacktestEngine(BacktestEngineConfig(trader_id=TraderId("LIFE-001"),
                                               logging=LoggingConfig(bypass_logging=True)))
    try:
        fills = StopLimitQuotes()
        engine.add_venue(venue=venue, oms_type=OmsType.NETTING, account_type=AccountType.CASH,
                         base_currency=HKD, starting_balances=[Money(100000, HKD)], fee_model=CumulativeFees(),
                         fill_model=fills, book_type=BookType.L2_MBP, liquidity_consumption=True)
        engine.add_instrument(instrument)
        books, ticks = [], []
        for point in task["quotes"]:
            stamp = utc_ns(point["time"])
            bid = Price.from_str(format(Decimal(point["bid"]), ".1f"))
            ask, size = Price.from_str(format(Decimal(point["ask"]), ".1f")), Quantity.from_int(100)
            books.append(OrderBookDeltas(identity, [
                OrderBookDelta(identity, BookAction.CLEAR, None, 32, 0, stamp, stamp + 1),
                OrderBookDelta(identity, BookAction.ADD, BookOrder(OrderSide.BUY, bid, size, 1),
                               32, 0, stamp, stamp + 1),
                OrderBookDelta(identity, BookAction.ADD, BookOrder(OrderSide.SELL, ask, size, 2),
                               160, 0, stamp, stamp + 1)]))
            ticks.append(QuoteTick(identity, bid, ask, size, size, stamp + 2, stamp + 2))
        engine.add_data(books)
        engine.add_data(ticks)
        if task.get("extended"):
            deadlines = {s["expiry"] for s in task["orders"] if s.get("tif", "GTC") != "GTC"}
            deadlines.add(task["day"] + " 16:00:00")
            controls = [CustomData(DataType(SessionClock), SessionClock(utc_ns(t))) for t in sorted(deadlines)]
            engine.add_data(controls, client_id=ClientId("CLOCK"))
        strategy = LifecycleStrategy(identity, task, fills)
        engine.add_strategy(strategy)
        fills.clock = strategy.clock
        engine.run()
        return {"platform": "nautilus", "events": strategy.events, "failures": strategy.failures,
                "native_notices": strategy.native_notices,
                "orders": {key: {"status": str(o.status.name), "filled": int(o.filled_qty.as_decimal()),
                                 "triggered": bool(getattr(o, "is_triggered", False))}
                           for key, o in strategy.orders.items()},
                "cash": str(engine.cache.account_for_venue(venue).balance_total(HKD).as_decimal()),
                "holding": sum(p.signed_qty for p in engine.cache.positions_open()), "real_orders_enabled": False}
    finally:
        engine.dispose()
