"""Native order-type and rejection matrix over genuine captured HK minute opens.

Derived BBO and integral 100-share liquidity are explicit simulation assumptions.
Auction TIF rejection is recorded as unsupported functionality, never counted as
an implemented auction. The native engine remains responsible for all matching.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import pandas as pd
from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.config import BacktestEngineConfig, LoggingConfig, StrategyConfig
from nautilus_trader.model.currencies import HKD
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.enums import AccountType, BookType, OmsType, OrderSide, TimeInForce, TriggerType
from nautilus_trader.model.identifiers import ClientOrderId, InstrumentId, Symbol, Venue
from nautilus_trader.model.instruments import Equity
from nautilus_trader.model.objects import Money, Quantity
from nautilus_trader.trading.strategy import Strategy

from .lifecycle import quotes
from .lifecycle_nautilus import CumulativeFees
from .nautilus_features_contingent import source_quotes
from .nautilus_order_extensions import feed, observations, price
from .paper_common import capture, utc_ns
from .session import write_report

CASES = ("market_to_limit", "market_if_touched_buy", "market_if_touched_sell", "limit_if_touched_buy",
         "limit_if_touched_sell", "trailing_stop_limit_buy", "trailing_stop_limit_sell", "post_only_reject",
         "auction_open_reject", "auction_close_reject")


def task_for(frame: pd.DataFrame, case: str) -> dict[str, object]:
    """Price each order from observations available at its scheduled submission."""
    if case not in CASES:
        raise ValueError("Unknown native order-matrix case")
    points, day = quotes(frame), str(frame.index[0].date())
    sell = case.endswith("sell")
    submit = day + " 10:00:00" if sell else points[0]["time"]
    current = next(x for x in points if x["time"] == submit)
    side = Decimal(current["bid" if sell else "ask"])
    direction = Decimal(-1 if sell else 1)
    trigger = side + direction * Decimal(".4") if case.startswith("trailing") else side - direction * Decimal(".4")
    return {"case": case, "quotes": points, "submit": submit, "seed": points[0]["time"] if sell else None,
            "modify": day + " 10:00:00", "cancel": day + " 15:29:00", "side": "sell" if sell else "buy",
            "price": str(trigger + direction * Decimal(".4")), "trigger": str(trigger),
            "initial": str(side), "shares": 200 if case == "market_to_limit" else 100}


class MatrixStrategy(Strategy):
    """Submit only native offline orders and retain fills, triggers, updates and expected rejections."""

    def __init__(self, identity: InstrumentId, task: dict[str, object]) -> None:
        """Bind one independently checked command scenario."""
        super().__init__(StrategyConfig(strategy_id="MATRIX"))
        self.identity, self.task = identity, task
        self.orders: dict[str, object] = {}
        self.events: list[dict[str, object]] = []
        self.rejections: list[dict[str, object]] = []
        self.failures: list[str] = []

    def on_start(self) -> None:
        """Subscribe to locally replayed quotes only."""
        self.subscribe_quote_ticks(self.identity)

    def on_quote_tick(self, tick: QuoteTick) -> None:
        """Submit causal native factory orders and explicitly cancel any remaining target."""
        time = str(pd.Timestamp(tick.ts_event, unit="ns", tz="UTC").tz_convert("Asia/Hong_Kong")
                   .tz_localize(None).floor("s"))
        if time == self.task["seed"]:
            seed = self.order_factory.market(self.identity, OrderSide.BUY, Quantity.from_int(100),
                                             client_order_id=ClientOrderId("SEED"))
            self.orders["SEED"] = seed
            self.submit_order(seed)
        if time == self.task["submit"]:
            order = self.create_order()
            self.orders["TARGET"] = order
            self.submit_order(order)
        if time == self.task["cancel"] and not self.orders["TARGET"].is_closed:
            self.cancel_order(self.orders["TARGET"])

    def create_order(self) -> object:
        """Choose the exact native factory and declared bid/ask trigger type for each supported case."""
        case = self.task["case"]
        common = (self.identity, OrderSide.SELL if self.task["side"] == "sell" else OrderSide.BUY,
                  Quantity.from_int(self.task["shares"]))
        extras = {"client_order_id": ClientOrderId("TARGET")}
        if case == "market_to_limit":
            return self.order_factory.market_to_limit(*common, **extras)
        if case.startswith("market_if_touched"):
            return self.order_factory.market_if_touched(*common, price(self.task["trigger"]),
                                                        trigger_type=TriggerType.BID_ASK, **extras)
        if case.startswith("limit_if_touched"):
            return self.order_factory.limit_if_touched(*common, price(self.task["price"]),
                price(self.task["trigger"]), trigger_type=TriggerType.BID_ASK, **extras)
        if case.startswith("trailing_stop_limit"):
            return self.order_factory.trailing_stop_limit(*common, limit_offset=Decimal(".4"),
                trailing_offset=Decimal(".4"), trigger_price=price(self.task["trigger"]),
                price=price(self.task["price"]), trigger_type=TriggerType.BID_ASK, **extras)
        if case == "post_only_reject":
            return self.order_factory.limit(*common, price(self.task["initial"]), post_only=True, **extras)
        tif = TimeInForce.AT_THE_OPEN if case == "auction_open_reject" else TimeInForce.AT_THE_CLOSE
        return self.order_factory.market(*common, time_in_force=tif, **extras)

    def on_order_filled(self, event: object) -> None:
        """Capture actual native fill price, quantity, timing and debited costs."""
        self.events.append({"type": "fill", "id": str(event.client_order_id), "ns": event.ts_event,
                            "shares": int(event.last_qty.as_decimal()), "price": str(event.last_px.as_decimal()),
                            "fee": str(event.commission.as_decimal())})

    def on_order_triggered(self, event: object) -> None:
        """Capture native trigger time independently of a later fill."""
        self.events.append({"type": "trigger", "id": str(event.client_order_id), "ns": event.ts_event})

    def on_order_updated(self, event: object) -> None:
        """Retain native trailing price changes and market-to-limit conversion prices."""
        self.events.append({"type": "update", "id": str(event.client_order_id), "ns": event.ts_event,
                            "price": str(event.price) if event.price is not None else None,
                            "trigger": str(event.trigger_price) if event.trigger_price is not None else None})

    def on_order_rejected(self, event: object) -> None:
        """Keep expected unsupported/invalid semantics visible rather than swallowing rejections."""
        self.rejections.append({"id": str(event.client_order_id), "reason": event.reason, "ns": event.ts_event})

    def on_order_denied(self, event: object) -> None:
        """Treat unplanned risk failures as failed validation."""
        self.failures.append(str(event))


def execute(task: dict[str, object]) -> dict[str, object]:
    """Run one native order matrix case with the existing documented cash and fee assumptions."""
    venue, identity = Venue("HKCONTINGENT"), InstrumentId.from_str("00700.HKCONTINGENT")
    engine = BacktestEngine(BacktestEngineConfig(logging=LoggingConfig(bypass_logging=True)))
    try:
        engine.add_venue(venue, oms_type=OmsType.NETTING, account_type=AccountType.CASH,
            base_currency=HKD, starting_balances=[Money(100000, HKD)], book_type=BookType.L2_MBP,
            liquidity_consumption=True, fee_model=CumulativeFees())
        engine.add_instrument(Equity(identity, Symbol("00700"), HKD, 1, price(".2"), Quantity.from_int(100), 0, 0))
        strategy = MatrixStrategy(identity, task)
        engine.add_strategy(strategy)
        feed(engine, observations(task))
        result = {"events": strategy.events, "rejections": strategy.rejections, "failures": strategy.failures,
            "orders": {k: {"status": v.status.name, "filled": int(v.filled_qty.as_decimal()),
                           "price": str(v.price) if v.has_price else None,
                           "trigger": str(v.trigger_price) if v.has_trigger_price else None}
                       for k, v in strategy.orders.items()},
            "cash": str(engine.cache.account_for_venue(venue).balance_total(HKD).as_decimal()),
            "holding": sum(p.signed_qty for p in engine.cache.positions_open()), "real_orders_enabled": False}
        engine.end()
        return result
    finally:
        engine.dispose()


def reference(task: dict[str, object], frame: pd.DataFrame) -> tuple[list[dict[str, object]], list[int]]:
    """Derive every target fill and passive trigger from source quotes, without reading native output."""
    source, case = source_quotes(frame), task["case"]
    start, end = task["submit"], task["cancel"]
    rows = [(time, value["bid" if task["side"] == "sell" else "ask"])
            for time, value in source.items() if start < time <= end]
    buy = task["side"] == "buy"
    trigger, initial = Decimal(task["trigger"]), Decimal(task["initial"])
    expected, triggers = [], []
    if case == "market_to_limit":
        expected.append({"ns": utc_ns(start) + 1, "price": initial, "shares": 100})
        second = next((time for time, value in rows if value <= initial), None)
        if second:
            expected.append({"ns": utc_ns(second), "price": initial, "shares": 100})
    elif case.startswith(("market_if_touched", "limit_if_touched")):
        touched = next(((time, value) for time, value in rows
                        if (value <= trigger if buy else value >= trigger)), None)
        if touched:
            time, value = touched
            expected.append({"ns": utc_ns(time), "price": trigger if case.startswith("limit") else value,
                             "shares": 100})
            if case.startswith("limit"):
                triggers.append(utc_ns(time))
    elif case.startswith("trailing"):
        active = False
        for time, value in rows:
            if not active and (value >= trigger if buy else value <= trigger):
                active = True
                triggers.append(utc_ns(time))
            if active:
                if value <= trigger if buy else value >= trigger:
                    expected.append({"ns": utc_ns(time), "price": trigger, "shares": 100})
                    break
            else:
                trigger = min(trigger, value + Decimal(".4")) if buy else max(trigger, value - Decimal(".4"))
    return expected, triggers


def audit(task: dict[str, object], actual: dict[str, object], frame: pd.DataFrame) -> dict[str, object]:
    """Validate native trigger causality, fill bounds, quantity and independent Decimal cash accounting."""
    if actual["failures"] or actual["real_orders_enabled"] is not False:
        raise ValueError("Native order matrix failed or enabled real routing")
    case, source = task["case"], source_quotes(frame)
    if case.endswith("reject"):
        reason = "POST_ONLY" if case == "post_only_reject" else "is not currently supported"
        if (len(actual["rejections"]) != 1 or reason not in actual["rejections"][0]["reason"]
                or actual["orders"]["TARGET"]["status"] != "REJECTED"
                or actual["orders"]["TARGET"]["filled"] != 0 or actual["cash"] != "100000"):
            raise ValueError("Native rejection did not match its specific unsupported/invalid contract")
        return {"passed": True, "supported": False if case.startswith("auction") else True,
                "expected_rejection": actual["rejections"][0]["reason"]}
    if actual["rejections"]:
        raise ValueError("A supported native order was rejected")
    wanted, triggers = reference(task, frame)
    fills = [x for x in actual["events"] if x["type"] == "fill" and x["id"] == "TARGET"]
    observed = [{"ns": x["ns"], "price": Decimal(x["price"]), "shares": x["shares"]} for x in fills]
    if observed != wanted:
        raise ValueError(f"Native target fills differ from the source-derived oracle: {observed} != {wanted}")
    if [x["ns"] for x in actual["events"] if x["type"] == "trigger"] != triggers:
        raise ValueError("Native passive trigger timing differs from the first causal source threshold crossing")
    cash, holding, filled, notionals, charged = Decimal(100000), 0, {}, {}, {}
    for event in actual["events"]:
        if event["type"] != "fill":
            continue
        key, shares, value = event["id"], event["shares"], Decimal(event["price"])
        stamp = str(pd.Timestamp(event["ns"], unit="ns", tz="UTC").tz_convert("Asia/Hong_Kong")
                    .tz_localize(None).floor("s"))
        if stamp not in source or shares != 100:
            raise ValueError("Native fill lacks a real observation or violates the integral-lot liquidity model")
        side = "buy" if key == "SEED" else task["side"]
        notional = notionals.get(key, Decimal(0)) + value * shares
        total = (max(Decimal(18), notional * Decimal(".0005")) + notional * Decimal(".000127")
                 + (notional * Decimal(".001")).to_integral_value(rounding="ROUND_CEILING")).quantize(
                     Decimal(".01"), rounding=ROUND_HALF_UP)
        fee = total - charged.get(key, Decimal(0))
        if Decimal(event["fee"]) != fee:
            raise ValueError("Native order fees differ from independent cumulative Decimal charges")
        filled[key] = filled.get(key, 0) + shares
        notionals[key], charged[key] = notional, total
        sign = 1 if side == "buy" else -1
        cash -= sign * value * shares + fee
        holding += sign * shares
        if holding < 0 or cash < 0 or filled[key] > (100 if key == "SEED" else task["shares"]):
            raise ValueError("Native order exceeded cash, inventory or requested quantity")
        if key == "SEED" and value != source[stamp]["ask"]:
            raise ValueError("Seed market order differs from real source ask")
        if key == "TARGET" and case.startswith("limit_if_touched"):
            if value > Decimal(task["price"]) if side == "buy" else value < Decimal(task["price"]):
                raise ValueError("Native touched limit filled outside its limit")
    if cash != Decimal(actual["cash"]) or holding != actual["holding"]:
        raise ValueError("Native matrix cash/inventory differs from the independent fill ledger")
    if filled.get("TARGET", 0) != actual["orders"]["TARGET"]["filled"]:
        raise ValueError("Native final quantity disagrees with actual fill events")
    return {"passed": True, "supported": True, "fills": sum(x["type"] == "fill" for x in actual["events"]),
            "target_filled": filled.get("TARGET", 0), "cash": str(cash), "holding": holding}


def run(response: Path, output: Path, repo: Path) -> dict[str, object]:
    """Exercise all newly covered native types and preserve explicit unsupported auction outcomes."""
    data = capture(response)
    output.mkdir(parents=True, exist_ok=False)
    results = {}
    for day in data.partitions["test"]:
        for case in CASES:
            task = task_for(data.minutes.loc[day], case)
            actual = execute(task)
            key = day + "_" + case
            write_report(output / (key + ".json"), {"task": task, "native": actual})
            results[key] = audit(task, actual, data.minutes.loc[day])
    report = {"passed": True, "cases": results, "scenarios": len(results), "data": data.provenance,
              "real_orders_enabled": False, "providers_modified": False,
              "unsupported": ["Native AT_THE_OPEN/AT_THE_CLOSE matching; requires auction data/model"],
              "limitations": ["Quote-driven BID_ASK triggers only; historical last-trade trigger parity is unverified",
                              "Integral 100-share assumed liquidity and derived spread from real minute opens"]}
    write_report(output / "report.json", report)
    return report
