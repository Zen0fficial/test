"""Explicit source-selective trailing conditions with native simulated execution on genuine live captures.

The pinned native trailing calculator lacks double-confirmation and midpoint
policies. This converter implements their declared causal semantics and sends
only triggered market tickets to BacktestEngine. It never labels this controller
as an upstream native trigger implementation. Real exchange trades and observed
BBO are preserved, ordered by availability, with gaps explicitly possible.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
from pathlib import Path

from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.config import BacktestEngineConfig, LoggingConfig, StrategyConfig
from nautilus_trader.model.currencies import HKD
from nautilus_trader.model.data import BookOrder, OrderBookDelta, OrderBookDeltas, QuoteTick, TradeTick
from nautilus_trader.model.enums import (
    AccountType, AggressorSide, BookAction, BookType, OmsType, OrderSide, TrailingOffsetType, TriggerType,
)
from nautilus_trader.model.identifiers import ClientOrderId, InstrumentId, Symbol, TradeId, Venue
from nautilus_trader.model.instruments import Equity
from nautilus_trader.model.objects import Money, Quantity
from nautilus_trader.trading.strategy import Strategy

from .lifecycle_nautilus import CumulativeFees
from .nautilus_order_extensions import price
from .session import write_report

TRIGGERS = ("LAST_PRICE", "LAST_OR_BID_ASK", "BID_ASK", "DOUBLE_LAST", "DOUBLE_BID_ASK", "MID_POINT")
OFFSETS = {"PRICE": Decimal(".2"), "BASIS_POINTS": Decimal(5), "TICKS": Decimal(1)}
IDENTITY = InstrumentId.from_str("00700.HKTRIGGERS")


def read(raw: Path) -> list[dict[str, object]]:
    """Keep genuine BBO observations and new, non-bootstrap trades; reject revision ambiguity."""
    values, identities = [], {}
    for line in raw.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        body, available = row.get("body", {}), int(row["received_ns"])
        data = body.get("data", {})
        source = body.get("source", {})
        if row.get("type") not in ("trades", "orderbook"):
            continue
        if data.get("symbol") != "HK.00700" or source.get("provider") != "futu-desktop-session":
            raise ValueError("Real trigger replay requires genuine HK.00700 Futu observations")
        if row["type"] == "orderbook" and data.get("book_kind") == "top_of_book":
            record = {"type": "quote", "ns": available + 1}
            for side, levels in (("bid", data["bids"]), ("ask", data["asks"])):
                if len(levels) != 1:
                    raise ValueError("Top-of-book capture must contain exactly one level per side")
                level = levels[0]
                amount = Decimal(level["price_raw"]) / Decimal(1_000_000_000)
                quantity = Decimal(level["volume_raw"]) / Decimal(10) ** int(level["volume_precision"])
                if amount != Decimal(str(level["price"])) or quantity != Decimal(str(level["volume"])):
                    raise ValueError("Captured BBO raw fields disagree with decoded values")
                if amount <= 0 or quantity < 100 or quantity != int(quantity):
                    raise ValueError("Observed BBO cannot support the explicit one-lot simulated order")
                record[side], record[side + "_size"] = str(amount), int(quantity)
            if Decimal(record["bid"]) >= Decimal(record["ask"]):
                raise ValueError("Crossed real BBO is outside the declared replay contract")
            values.append(record)
        if row["type"] == "trades" and not data.get("initial_snapshot") and not data.get("backfill"):
            if data.get("revised_trade_ids"):
                raise ValueError("Trade revisions require an explicit correction policy before trigger replay")
            for index, trade in enumerate(data["trades"]):
                signature = (str(trade["price"]), str(trade["size_decimal"]), int(trade["exchange_timestamp_ms"]))
                if trade["trade_id"] in identities:
                    if identities[trade["trade_id"]] != signature:
                        raise ValueError("A repeated real trade ID changed its economic payload")
                    continue
                identities[trade["trade_id"]] = signature
                if Decimal(signature[0]) <= 0 or Decimal(signature[1]) <= 0:
                    raise ValueError("Invalid real trade observation")
                values.append({"type": "trade", "ns": available + index, "event_ns": signature[2] * 1_000_000,
                               "price": signature[0], "size": signature[1], "id": trade["trade_id"]})
    values.sort(key=lambda x: x["ns"])
    if len({x["ns"] for x in values}) != len(values) or not identities:
        raise ValueError("Real trigger replay needs distinct availability times and new trades")
    return values


def candidate(reference: Decimal, side: str, offset: str) -> Decimal:
    """Match pinned native price-precision rounding for price, basis-point and tick trailing offsets."""
    distance = OFFSETS[offset]
    if offset == "BASIS_POINTS":
        distance *= reference / Decimal(10000)
    elif offset == "TICKS":
        distance *= Decimal(".2")
    return (reference + distance if side == "buy" else reference - distance).quantize(
        Decimal(".1"), rounding=ROUND_HALF_UP)


class TriggerPolicy:
    """Source-selective causal trigger state independent of native order transmission."""

    def __init__(self, trigger: str, offset: str, side: str) -> None:
        """Require a declared policy; unsupported mark/index/tier meanings are never silently substituted."""
        if trigger not in TRIGGERS or offset not in OFFSETS or side not in ("buy", "sell"):
            raise ValueError("Unsupported source-selective trailing policy")
        self.trigger, self.offset, self.side = trigger, offset, side
        self.bid: Decimal | None = None
        self.ask: Decimal | None = None
        self.last: Decimal | None = None
        self.stop: Decimal | None = None
        self.confirmations = 0
        self.fired = False

    def observe(self, row: dict[str, object]) -> bool:
        """Evaluate only fresh relevant observations, then move an untriggered stop in its favorable direction."""
        if row["type"] == "quote":
            self.bid, self.ask = Decimal(row["bid"]), Decimal(row["ask"])
        else:
            self.last = Decimal(row["price"])
        if self.fired or self.bid is None or self.ask is None or self.last is None:
            return False
        last_only = self.trigger in ("LAST_PRICE", "DOUBLE_LAST")
        quote_only = self.trigger in ("BID_ASK", "DOUBLE_BID_ASK", "MID_POINT")
        if (last_only and row["type"] != "trade") or (quote_only and row["type"] != "quote"):
            return False
        side = self.ask if self.side == "buy" else self.bid
        sources = [self.last] if last_only else [side]
        if self.trigger == "MID_POINT":
            sources = [(self.bid + self.ask) / 2]
        elif self.trigger == "LAST_OR_BID_ASK":
            sources = [self.last, side]
        best = min(sources) if self.side == "buy" else max(sources)
        if self.stop is None:
            # The real capture's BBO is constant. An already-eligible quote threshold
            # checks confirmation semantics without fabricating a price movement.
            self.stop = best if quote_only else candidate(best, self.side, self.offset)
            return False
        crossed = any(value >= self.stop if self.side == "buy" else value <= self.stop for value in sources)
        self.confirmations = self.confirmations + 1 if crossed else 0
        needed = 2 if self.trigger.startswith("DOUBLE") else 1
        if self.confirmations >= needed:
            self.fired = True
            return True
        following = candidate(best, self.side, self.offset)
        self.stop = min(self.stop, following) if self.side == "buy" else max(self.stop, following)
        return False


class TriggerStrategy(Strategy):
    """Route only a satisfied joint condition to native offline cash-account market execution."""

    def __init__(self, trigger: str, offset: str, side: str) -> None:
        """Bind one policy and start deterministic native fill/condition journals."""
        super().__init__(StrategyConfig(strategy_id="TRIGGERS"))
        self.policy = TriggerPolicy(trigger, offset, side)
        self.seeded = False
        self.fills: list[dict[str, object]] = []
        self.failures: list[str] = []
        self.conditions: list[dict[str, object]] = []

    def on_start(self) -> None:
        """Subscribe only to genuine captured quotes/trades supplied to the offline engine."""
        self.subscribe_quote_ticks(IDENTITY)
        self.subscribe_trade_ticks(IDENTITY)

    def on_quote_tick(self, tick: QuoteTick) -> None:
        """Fund sell tests once, then evaluate the observed bid and ask at their availability instant."""
        if not self.seeded and self.policy.side == "sell":
            self.seeded = True
            self.submit_order(self.order_factory.market(IDENTITY, OrderSide.BUY, Quantity.from_int(100),
                                                        client_order_id=ClientOrderId("SEED")))
        self.evaluate({"type": "quote", "ns": tick.ts_init,
                       "bid": str(tick.bid_price.as_decimal()), "ask": str(tick.ask_price.as_decimal())})

    def on_trade_tick(self, tick: TradeTick) -> None:
        """Evaluate actual new trades without treating bootstrap or backfill prints as fresh activity."""
        self.evaluate({"type": "trade", "ns": tick.ts_init, "price": str(tick.price.as_decimal())})

    def evaluate(self, row: dict[str, object]) -> None:
        """Journal each policy transition before native order creation and execution."""
        fire = self.policy.observe(row)
        self.conditions.append({"ns": row["ns"], "stop": str(self.policy.stop), "fired": fire,
                                "confirmations": self.policy.confirmations})
        if fire:
            self.submit_order(self.order_factory.market(IDENTITY,
                OrderSide.BUY if self.policy.side == "buy" else OrderSide.SELL,
                Quantity.from_int(100), client_order_id=ClientOrderId("TARGET")))

    def on_order_filled(self, event: object) -> None:
        """Retain authoritative native fill quantities, prices and fee debits."""
        self.fills.append({"id": str(event.client_order_id), "ns": event.ts_event,
                           "price": str(event.last_px.as_decimal()), "shares": int(event.last_qty.as_decimal()),
                           "fee": str(event.commission.as_decimal())})

    def on_order_rejected(self, event: object) -> None:
        """Reject unexpected native matching failures."""
        self.failures.append(str(event))

    def on_order_denied(self, event: object) -> None:
        """Reject native cash or long-only risk failures."""
        self.failures.append(str(event))


class NativeOffsetStrategy(TriggerStrategy):
    """Observe native trailing updates under distant nonmarketable stops for arithmetic equivalence."""

    def __init__(self, trigger: str, offset: str, side: str) -> None:
        """Use native-supported sources and far offsets so genuine observed prices never execute the target."""
        super().__init__(trigger, offset, side)
        self.order: object | None = None

    def evaluate(self, row: dict[str, object]) -> None:
        """Submit a native trailing order once both real source families are available, then inspect its state."""
        if row["type"] == "quote":
            self.policy.bid, self.policy.ask = Decimal(row["bid"]), Decimal(row["ask"])
        else:
            self.policy.last = Decimal(row["price"])
        if self.order is None and all(x is not None for x in (self.policy.bid, self.policy.ask, self.policy.last)):
            self.order = self.order_factory.trailing_stop_market(IDENTITY,
                OrderSide.BUY if self.policy.side == "buy" else OrderSide.SELL, Quantity.from_int(100),
                trailing_offset=OFFSETS[self.policy.offset] * 100,
                trailing_offset_type=TrailingOffsetType[self.policy.offset],
                trigger_type=TriggerType[self.policy.trigger], client_order_id=ClientOrderId("TARGET"))
            self.submit_order(self.order)
        self.conditions.append({"ns": row["ns"],
                                "stop": str(self.order.trigger_price)
                                if self.order is not None and self.order.trigger_price is not None else None})


def execute(rows: list[dict[str, object]], trigger: str, offset: str, side: str,
            native_offsets: bool = False) -> dict[str, object]:
    """Preserve real trade/BBO values and native market fills with no inferred historical order queue."""
    engine = BacktestEngine(BacktestEngineConfig(logging=LoggingConfig(bypass_logging=True)))
    venue = Venue("HKTRIGGERS")
    try:
        engine.add_venue(venue, oms_type=OmsType.NETTING, account_type=AccountType.CASH,
            base_currency=HKD, starting_balances=[Money(100000, HKD)], book_type=BookType.L2_MBP,
            fee_model=CumulativeFees(), trade_execution=False, liquidity_consumption=True)
        engine.add_instrument(Equity(IDENTITY, Symbol("00700"), HKD, 1, price(".2"), Quantity.from_int(100), 0, 0))
        books, quotes, trades = [], [], []
        for row in rows:
            stamp = row["ns"]
            if row["type"] == "quote":
                bid, ask = price(row["bid"]), price(row["ask"])
                bid_size, ask_size = Quantity.from_int(row["bid_size"]), Quantity.from_int(row["ask_size"])
                books.append(OrderBookDeltas(IDENTITY, [
                    OrderBookDelta(IDENTITY, BookAction.CLEAR, None, 32, 0, stamp - 1, stamp - 1),
                    OrderBookDelta(IDENTITY, BookAction.ADD, BookOrder(OrderSide.BUY, bid, bid_size, 1),
                                   32, 0, stamp - 1, stamp - 1),
                    OrderBookDelta(IDENTITY, BookAction.ADD, BookOrder(OrderSide.SELL, ask, ask_size, 2),
                                   160, 0, stamp - 1, stamp - 1)]))
                quotes.append(QuoteTick(IDENTITY, bid, ask, bid_size, ask_size, stamp, stamp))
            else:
                trades.append(TradeTick(IDENTITY, price(row["price"]), Quantity.from_str(row["size"]),
                    AggressorSide.NO_AGGRESSOR, TradeId(row["id"]), row["event_ns"], stamp))
        for batch in (books, quotes, trades):
            engine.add_data(batch)
        strategy = (NativeOffsetStrategy(trigger, offset, side) if native_offsets
                    else TriggerStrategy(trigger, offset, side))
        engine.add_strategy(strategy)
        engine.run()
        return {"fills": strategy.fills, "failures": strategy.failures, "conditions": strategy.conditions,
                "cash": str(engine.cache.account_for_venue(venue).balance_total(HKD).as_decimal()),
                "holding": sum(p.signed_qty for p in engine.cache.positions_open()), "real_orders_enabled": False}
    finally:
        engine.dispose()


def audit_native_offsets(rows: list[dict[str, object]], actual: dict[str, object], trigger: str,
                         offset: str, side: str) -> dict[str, object]:
    """Compare every native trailing stop update to independently calculated Decimal offset arithmetic."""
    bid = ask = last = stop = None
    if actual["failures"] or any(x["id"] == "TARGET" for x in actual["fills"]):
        raise ValueError("Distant native trailing offset case unexpectedly failed or filled")
    if len(rows) != len(actual["conditions"]):
        raise ValueError("Native trailing arithmetic lost an observed source event")
    for row, observed in zip(rows, actual["conditions"]):
        previous_stop = stop
        if row["type"] == "quote":
            bid, ask = Decimal(row["bid"]), Decimal(row["ask"])
        else:
            last = Decimal(row["price"])
        if all(x is not None for x in (bid, ask, last)) and (row["type"] == "quote" or stop is None):
            market = ask if side == "buy" else bid
            reference = last if trigger == "LAST_PRICE" else market
            if trigger == "LAST_OR_BID_ASK":
                reference = min(last, market) if side == "buy" else max(last, market)
            distance = OFFSETS[offset] * 100
            if offset == "BASIS_POINTS":
                distance = reference * distance / 10000
            elif offset == "TICKS":
                distance *= Decimal(".2")
            following = (reference + distance if side == "buy" else reference - distance).quantize(
                Decimal(".1"), rounding=ROUND_HALF_UP)
            stop = following if stop is None else min(stop, following) if side == "buy" else max(stop, following)
        actual_stop = Decimal(observed["stop"]) if observed["stop"] is not None else None
        if previous_stop is None and stop is not None and actual_stop is None:
            # Submission occurs in the callback; native matching processes the
            # queued command after it returns. Subsequent callbacks must agree.
            continue
        if actual_stop != stop or observed["ns"] != row["ns"]:
            raise ValueError(f"Native trailing offset differs at {row['ns']}: {actual_stop} != {stop}")
    return {"passed": True, "observations": len(rows), "final_stop": str(stop), "target_executed": False}


def oracle(rows: list[dict[str, object]], trigger: str, offset: str, side: str) -> list[dict[str, object]]:
    """Compute expected condition receipt and execution side using a separate integer-price scan."""
    bid = ask = last = bound = None
    hits, seeded, result = 0, False, []
    double = trigger.startswith("DOUBLE")
    for row in rows:
        if row["type"] == "quote":
            bid, ask = (int(Decimal(row[k]) * 10) for k in ("bid", "ask"))
            if side == "sell" and not seeded:
                result.append({"id": "SEED", "ns": row["ns"], "price": Decimal(ask) / 10, "shares": 100})
                seeded = True
        else:
            last = int(Decimal(row["price"]) * 10)
        if bid is None or ask is None or last is None:
            continue
        selected = (trigger in ("LAST_PRICE", "DOUBLE_LAST") and row["type"] == "trade"
                    or trigger in ("BID_ASK", "DOUBLE_BID_ASK", "MID_POINT") and row["type"] == "quote"
                    or trigger == "LAST_OR_BID_ASK")
        if not selected:
            continue
        quote = ask if side == "buy" else bid
        bases = [last] if trigger in ("LAST_PRICE", "DOUBLE_LAST") else [quote]
        if trigger == "MID_POINT":
            bases = [Decimal(bid + ask) / 2]
        if trigger == "LAST_OR_BID_ASK":
            bases = [last, quote]
        best = min(bases) if side == "buy" else max(bases)
        addition = Decimal(2) if offset in ("PRICE", "TICKS") else Decimal(best) * Decimal(5) / 10000
        following = (best + addition if side == "buy" else best - addition).quantize(
            Decimal(1), rounding=ROUND_HALF_UP)
        if bound is None:
            bound = best if trigger in ("BID_ASK", "DOUBLE_BID_ASK", "MID_POINT") else following
            continue
        crossed = max(bases) >= bound if side == "buy" else min(bases) <= bound
        hits = hits + 1 if crossed else 0
        if hits >= (2 if double else 1):
            result.append({"id": "TARGET", "ns": row["ns"], "price": Decimal(quote) / 10, "shares": 100})
            break
        bound = min(bound, following) if side == "buy" else max(bound, following)
    return result


def audit(rows: list[dict[str, object]], actual: dict[str, object], trigger: str,
          offset: str, side: str) -> dict[str, object]:
    """Require native execution timing and economic state to match independent real-observation expectations."""
    wanted = oracle(rows, trigger, offset, side)
    observed = [{k: Decimal(v) if k == "price" else v for k, v in x.items() if k != "fee"}
                for x in actual["fills"]]
    if actual["failures"] or actual["real_orders_enabled"] is not False or observed != wanted:
        raise ValueError("Native real-capture conditional execution differs from the independent receipt-order oracle")
    cash, holding = Decimal(100000), 0
    for event in actual["fills"]:
        notional = Decimal(event["price"]) * 100
        expected_fee = (max(Decimal(18), notional * Decimal(".0005")) + notional * Decimal(".000127")
                        + (notional * Decimal(".001")).to_integral_value(rounding="ROUND_CEILING")).quantize(
                            Decimal(".01"), rounding=ROUND_HALF_UP)
        if Decimal(event["fee"]) != expected_fee:
            raise ValueError("Real-capture trigger fee differs from independent Decimal cost accounting")
        sign = 1 if event["id"] == "SEED" or side == "buy" else -1
        cash -= sign * notional + expected_fee
        holding += sign * 100
    if Decimal(actual["cash"]) != cash or actual["holding"] != holding or cash < 0 or holding < 0:
        raise ValueError("Real-capture trigger cash/holding differs from independent actual-fill accounting")
    return {"passed": True, "fills": len(wanted), "triggered": any(x["id"] == "TARGET" for x in wanted),
            "cash": str(cash), "holding": holding}


def run(raw: Path, output: Path, repo: Path) -> dict[str, object]:
    """Validate 36 source/offset/direction combinations on real live-captured BBO and trades."""
    rows = read(raw)
    output.mkdir(parents=True, exist_ok=False)
    results = {}
    for trigger in TRIGGERS:
        for offset in OFFSETS:
            for side in ("buy", "sell"):
                actual = execute(rows, trigger, offset, side)
                key = trigger + "_" + offset + "_" + side
                results[key] = audit(rows, actual, trigger, offset, side)
                write_report(output / (key + ".json"), actual)
    native_offsets = {}
    for trigger in ("LAST_PRICE", "LAST_OR_BID_ASK", "BID_ASK"):
        for offset in OFFSETS:
            for side in ("buy", "sell"):
                actual = execute(rows, trigger, offset, side, native_offsets=True)
                key = trigger + "_" + offset + "_" + side
                native_offsets[key] = audit_native_offsets(rows, actual, trigger, offset, side)
                write_report(output / ("native_offset_" + key + ".json"), actual)
    report = {"passed": True, "scenarios": len(results), "cases": results,
        "native_offset_scenarios": len(native_offsets), "native_offsets": native_offsets,
        "source_sha256": hashlib.sha256(raw.read_bytes()).hexdigest(), "source_path": str(raw.resolve()),
        "quotes": sum(x["type"] == "quote" for x in rows), "trades": sum(x["type"] == "trade" for x in rows),
        "real_orders_enabled": False, "providers_modified": False,
        "implementation": "Joint source-selective trailing policy; native execution and cash accounting",
        "limitations": ["Availability ordering; exchange sequence and losslessness are not verified",
                        "BBO is constant in this capture; quote-source cases use initially eligible causal thresholds",
                        "Double means two consecutive fresh relevant observations; this is a joint-owned policy",
                        "Native mark/index/tier meanings are not inferred or substituted",
                        "No broker orders, external routing, synthetic trades or synthetic BBO are used"]}
    write_report(output / "report.json", report)
    return report
