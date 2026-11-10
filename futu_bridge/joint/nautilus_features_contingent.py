"""Native offline brackets, contingent child updates and deterministic latency races.

Only real captured opens determine market observations. Clock-only data advance
simulated command delivery between observations without inventing market ticks.
"""

from __future__ import annotations

import argparse
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
import json
from pathlib import Path
import shutil

import pandas as pd
from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.backtest.models import LatencyModel
from nautilus_trader.config import BacktestEngineConfig, LoggingConfig, StrategyConfig
from nautilus_trader.model.currencies import HKD
from nautilus_trader.model.data import BookOrder, CustomData, DataType, OrderBookDelta, OrderBookDeltas, QuoteTick
from nautilus_trader.model.enums import (
    AccountType, BookAction, BookType, ContingencyType, OmsType, OrderSide, OrderType, OtoTriggerMode,
)
from nautilus_trader.model.identifiers import ClientId, ClientOrderId, InstrumentId, Symbol, Venue
from nautilus_trader.model.instruments import Equity
from nautilus_trader.model.objects import Money, Price, Quantity
from nautilus_trader.trading.strategy import Strategy

from .lifecycle import quotes
from .lifecycle_nautilus import CumulativeFees, SessionClock
from .paper_common import capture, utc_ns
from .qlib_rl_data import ExecutionData
from .session import write_report

CASES = ("bracket_tp", "bracket_sl", "parent_cancel", "ouo_partial_exit",
         "cancel_before_next_quote", "fill_before_delayed_cancel", "delayed_insert")


def make_task(frame: pd.DataFrame, case: str) -> dict[str, object]:
    """Price prescribed commands only from information available when each is submitted."""
    if case not in CASES:
        raise ValueError("Unknown contingent acceptance case")
    points, day = quotes(frame), str(frame.index[0].date())
    first = points[0]
    ask, bid = Decimal(first["ask"]), Decimal(first["bid"])
    return {"case": case, "quotes": points, "day": day, "submit": first["time"],
            "cancel": day + (" 09:31:00" if case == "parent_cancel" else " 15:29:00"),
            "entry": str(bid - 20 if case == "parent_cancel" else ask + 10),
            "tp": str(ask + Decimal(".4") if case == "bracket_tp" else Decimal("499.8")),
            "sl": str(bid - Decimal(".4") if case == "bracket_sl" else bid - 40),
            "shares": 200 if case in ("ouo_partial_exit", "cancel_before_next_quote", "fill_before_delayed_cancel")
            else 100,
            "modify": day + " 10:00:00", "modified_tp": str(Decimal(next(
                p["bid"] for p in points if p["time"] == day + " 10:00:00")) - Decimal(".4")),
            "insert_delay": 120 if case == "delayed_insert" else 0,
            "cancel_delay": 1 if case == "cancel_before_next_quote" else 61 if case == "fill_before_delayed_cancel"
            else 0}


class ContingentStrategy(Strategy):
    """Record native bracket linkage, effective events and expected late-cancel rejections."""

    def __init__(self, identity: InstrumentId, task: dict[str, object]) -> None:
        """Bind fixed instructions and start clean callback journals."""
        super().__init__(StrategyConfig(strategy_id="CONTINGENT"))
        self.identity, self.task = identity, task
        self.orders: dict[str, object] = {}
        self.links: dict[str, object] = {}
        self.events: list[dict[str, object]] = []
        self.updates: list[dict[str, object]] = []
        self.rejections: list[dict[str, object]] = []
        self.failures: list[str] = []
        self.cancel_requested = False

    def stamp(self, value: int) -> str:
        """Express native event times on the HK second clock while retaining raw nanoseconds."""
        return str(pd.Timestamp(value, unit="ns", tz="UTC").tz_convert("Asia/Hong_Kong").tz_localize(None).floor("s"))

    def on_start(self) -> None:
        """Subscribe only to the offline instrument's derived quotes."""
        self.subscribe_quote_ticks(self.identity)

    def on_quote_tick(self, tick: QuoteTick) -> None:
        """Submit native order lists or simulated latency commands at their causal times."""
        time, case = self.stamp(tick.ts_event), self.task["case"]
        if time == self.task["submit"]:
            if case in CASES[:4]:
                bracket = self.order_factory.bracket(self.identity, OrderSide.BUY,
                    Quantity.from_int(self.task["shares"]),
                    contingency_type=ContingencyType.OUO if case == "ouo_partial_exit" else ContingencyType.OCO,
                    entry_order_type=OrderType.LIMIT,
                    entry_price=Price.from_str(format(Decimal(self.task["entry"]), ".1f")),
                    tp_price=Price.from_str(format(Decimal(self.task["tp"]), ".1f")), tp_post_only=False,
                    sl_trigger_price=Price.from_str(format(Decimal(self.task["sl"]), ".1f")),
                    entry_client_order_id=ClientOrderId("ENTRY"), tp_client_order_id=ClientOrderId("TP"),
                    sl_client_order_id=ClientOrderId("SL"))
                for order in bracket.orders:
                    key = str(order.client_order_id)
                    self.orders[key] = order
                    self.links[key] = {"parent": str(order.parent_order_id) if order.parent_order_id else None,
                        "linked": [str(x) for x in order.linked_order_ids or []],
                        "contingency": order.contingency_type.name, "list": str(order.order_list_id),
                        "reduce_only": order.is_reduce_only}
                self.submit_order_list(bracket)
            else:
                if case == "delayed_insert":
                    order = self.order_factory.market(self.identity, OrderSide.BUY,
                        Quantity.from_int(100), client_order_id=ClientOrderId("ENTRY"))
                else:
                    order = self.order_factory.limit(self.identity, OrderSide.BUY,
                        Quantity.from_int(200), Price.from_str(format(Decimal(self.task["entry"]), ".1f")),
                        client_order_id=ClientOrderId("ENTRY"))
                self.orders["ENTRY"] = order
                self.submit_order(order)
        if case == "ouo_partial_exit" and time == self.task["modify"]:
            self.events.append({"type": "amend_request", "id": "TP", "time": time})
            self.modify_order(self.orders["TP"],
                price=Price.from_str(format(Decimal(self.task["modified_tp"]), ".1f")))
        if time == self.task["cancel"]:
            for order in list(self.orders.values()):
                if not order.is_closed:
                    self.cancel_order(order)
                    if case in CASES[:4]:
                        break  # Native contingency handling cancels the linked siblings.

    def on_order_filled(self, event: object) -> None:
        """Record authoritative native fills and queue the partial remainder cancellation."""
        key = str(event.client_order_id)
        self.events.append({"type": "fill", "id": key, "time": self.stamp(event.ts_event),
                            "ns": event.ts_event, "shares": int(event.last_qty.as_decimal()),
                            "filled": int(self.orders[key].filled_qty.as_decimal()),
                            "price": str(event.last_px.as_decimal()), "fee": str(event.commission.as_decimal())})
        if (self.task["case"] in ("cancel_before_next_quote", "fill_before_delayed_cancel")
                and not self.orders[key].is_closed and not self.cancel_requested):
            self.cancel_requested = True
            self.events.append({"type": "cancel_request", "id": key, "time": self.stamp(event.ts_event)})
            self.cancel_order(self.orders[key])

    def on_order_canceled(self, event: object) -> None:
        """Retain native sibling and parent cancellation acknowledgements."""
        self.events.append({"type": "cancel", "id": str(event.client_order_id),
                            "time": self.stamp(event.ts_event), "ns": event.ts_event})

    def on_order_updated(self, event: object) -> None:
        """Retain all native OUO child quantity adjustments and amendment acknowledgements."""
        self.updates.append({"id": str(event.client_order_id), "time": self.stamp(event.ts_event),
                             "shares": int(event.quantity.as_decimal()), "ns": event.ts_event})

    def on_order_cancel_rejected(self, event: object) -> None:
        """Record a late cancel independently; expected races must be verified, not hidden."""
        self.rejections.append({"id": str(event.client_order_id), "time": self.stamp(event.ts_event),
                                "ns": event.ts_event, "reason": event.reason})

    def on_order_denied(self, event: object) -> None:
        """Treat unplanned risk denials as failed evidence."""
        self.failures.append(str(event))

    def on_order_rejected(self, event: object) -> None:
        """Treat unplanned order rejections as failed evidence."""
        self.failures.append(str(event))

    def on_order_modify_rejected(self, event: object) -> None:
        """Do not silently accept failed contingent amendments."""
        self.failures.append(str(event))


def execute(task: dict[str, object]) -> dict[str, object]:
    """Run the native simulated exchange with explicitly configured message latency."""
    venue, identity = Venue("HKCONTINGENT"), InstrumentId.from_str("00700.HKCONTINGENT")
    engine = BacktestEngine(BacktestEngineConfig(logging=LoggingConfig(bypass_logging=True)))
    try:
        engine.add_venue(venue, oms_type=OmsType.NETTING, account_type=AccountType.CASH,
            base_currency=HKD, starting_balances=[Money(100000, HKD)], book_type=BookType.L2_MBP,
            liquidity_consumption=True, fee_model=CumulativeFees(), support_contingent_orders=True,
            oto_trigger_mode=OtoTriggerMode.FULL,
            latency_model=LatencyModel(base_latency_nanos=0,
                insert_latency_nanos=task["insert_delay"] * 1_000_000_000,
                cancel_latency_nanos=task["cancel_delay"] * 1_000_000_000))
        engine.add_instrument(Equity(identity, Symbol("00700"), HKD, 1, Price.from_str(".2"),
                                     Quantity.from_int(100), 0, 0))
        books, ticks = [], []
        for point in task["quotes"]:
            stamp, size = utc_ns(point["time"]), Quantity.from_int(100)
            bid, ask = (Price.from_str(format(Decimal(point[k]), ".1f")) for k in ("bid", "ask"))
            books.append(OrderBookDeltas(identity, [
                OrderBookDelta(identity, BookAction.CLEAR, None, 32, 0, stamp, stamp),
                OrderBookDelta(identity, BookAction.ADD, BookOrder(OrderSide.BUY, bid, size, 1),
                               32, 0, stamp, stamp),
                OrderBookDelta(identity, BookAction.ADD, BookOrder(OrderSide.SELL, ask, size, 2),
                               160, 0, stamp, stamp)]))
            ticks.append(QuoteTick(identity, bid, ask, size, size, stamp + 1, stamp + 1))
        engine.add_data(books)
        engine.add_data(ticks)
        controls = [CustomData(DataType(SessionClock), SessionClock(utc_ns(task["submit"])
            + seconds * 1_000_000_000 + 1)) for seconds in (1, 61, 120)]
        engine.add_data(controls, client_id=ClientId("CLOCK"))
        strategy = ContingentStrategy(identity, task)
        engine.add_strategy(strategy)
        engine.run()
        return {"events": strategy.events, "updates": strategy.updates, "links": strategy.links,
                "cancel_rejections": strategy.rejections, "failures": strategy.failures,
                "orders": {k: {"status": o.status.name, "filled": int(o.filled_qty.as_decimal()),
                                "shares": int(o.quantity.as_decimal())} for k, o in strategy.orders.items()},
                "cash": str(engine.cache.account_for_venue(venue).balance_total(HKD).as_decimal()),
                "holding": sum(p.signed_qty for p in engine.cache.positions_open()), "real_orders_enabled": False}
    finally:
        engine.dispose()


def source_quotes(frame: pd.DataFrame) -> dict[str, dict[str, Decimal]]:
    """Independently reconstruct adverse tick-rounded side prices from real opens."""
    return {str(t): {"ask": (Decimal(str(r.open)) * Decimal("1.0005") / Decimal(".2")).to_integral_value(
        rounding=ROUND_CEILING) * Decimal(".2"),
        "bid": (Decimal(str(r.open)) * Decimal(".9995") / Decimal(".2")).to_integral_value(
            rounding=ROUND_FLOOR) * Decimal(".2")} for t, r in frame.iterrows()}


class ExpectedLedger:
    """Independent Decimal event ledger; it never reads engine-generated decisions."""

    def __init__(self) -> None:
        """Begin a funded long-only account with independent per-order fee state."""
        self.events: list[dict[str, object]] = []
        self.notionals: dict[str, Decimal] = {}
        self.fees: dict[str, Decimal] = {}
        self.filled: dict[str, int] = {}
        self.cash, self.holding = Decimal(100000), 0

    def fill(self, key: str, time: str, price: Decimal, *, after_command: bool = False) -> None:
        """Commit one independently selected100-share fill and incremental charges."""
        notional = self.notionals.get(key, Decimal(0)) + price * 100
        total = (max(Decimal(18), notional * Decimal(".0005")) + notional * Decimal(".000127")
                 + (notional * Decimal(".001")).to_integral_value(rounding=ROUND_CEILING)).quantize(
                     Decimal(".01"), rounding=ROUND_HALF_UP)
        fee = total - self.fees.get(key, Decimal(0))
        self.filled[key] = self.filled.get(key, 0) + 100
        self.notionals[key], self.fees[key] = notional, total
        sign = 1 if key == "ENTRY" else -1
        self.cash -= sign * price * 100 + fee
        self.holding += sign * 100
        if self.cash < 0 or self.holding < 0:
            raise ValueError("Contingent reference exceeds cash or creates short inventory")
        self.events.append({"type": "fill", "id": key, "time": time, "ns": utc_ns(time) + int(after_command),
                            "shares": 100, "filled": self.filled[key], "price": str(price), "fee": str(fee)})

    def cancel(self, key: str, time: str, *, after_command: bool = False) -> None:
        """Record effective cancellation at the independent native matching/delivery phase."""
        self.events.append({"type": "cancel", "id": key, "time": time, "ns": utc_ns(time) + int(after_command)})


def reference(task: dict[str, object], frame: pd.DataFrame) -> dict[str, object]:
    """Build contingent linkage/state/latency outcomes directly from source-price predicates."""
    prices, ledger = source_quotes(frame), ExpectedLedger()
    case, start, end = task["case"], task["submit"], task["cancel"]
    updates, rejections = [], []
    states = {"ENTRY": {"status": "FILLED", "filled": task["shares"], "shares": task["shares"]}}
    if case in CASES[:4]:
        states.update({key: {"status": "CANCELED", "filled": 0, "shares": task["shares"]} for key in ("SL", "TP")})
    if case == "parent_cancel":
        if any(p["ask"] <= Decimal(task["entry"]) for t, p in prices.items() if start <= t <= end):
            raise ValueError("Parent-cancel scenario became marketable before cancellation")
        for key in ("ENTRY", "SL", "TP"):
            ledger.cancel(key, end, after_command=True)
        states["ENTRY"]["status"], states["ENTRY"]["filled"] = "CANCELED", 0
    elif case in ("bracket_tp", "bracket_sl"):
        ledger.fill("ENTRY", start, prices[start]["ask"], after_command=True)
        winner = None
        for time, quote in prices.items():
            if not start < time <= end:
                continue
            if quote["bid"] >= Decimal(task["tp"]):
                winner, sibling, price = "TP", "SL", Decimal(task["tp"])
            elif quote["bid"] <= Decimal(task["sl"]):
                winner, sibling, price = "SL", "TP", quote["bid"]
            else:
                continue
            ledger.fill(winner, time, price)
            ledger.cancel(sibling, time)
            states[winner].update(status="FILLED", filled=100)
            break
        if winner is None:
            ledger.cancel("SL", end, after_command=True)
            ledger.cancel("TP", end, after_command=True)
    elif case == "ouo_partial_exit":
        entries = [t for t, p in prices.items() if start <= t < task["modify"]
                   and p["ask"] <= Decimal(task["entry"])][:2]
        if len(entries) != 2:
            raise ValueError("OUO test requires the parent to finish before its prescribed exit amendment")
        for time in entries:
            ledger.fill("ENTRY", time, prices[time]["ask"] if time == start else Decimal(task["entry"]),
                         after_command=time == start)
        time = task["modify"]
        ledger.events.append({"type": "amend_request", "id": "TP", "time": time})
        updates.append({"id": "TP", "time": time, "shares": 200, "ns": utc_ns(time) + 1})
        ledger.fill("TP", time, prices[time]["bid"], after_command=True)
        updates.append({"id": "SL", "time": time, "shares": 100, "ns": utc_ns(time) + 1})
        states["SL"]["shares"] = 100
        states["TP"].update(filled=100)
        exit_time = next((t for t, p in prices.items() if time < t <= end
                          and p["bid"] >= Decimal(task["modified_tp"])), None)
        if any(p["bid"] <= Decimal(task["sl"]) for t, p in prices.items()
               if time < t <= (exit_time or end)):
            raise ValueError("OUO test price window unexpectedly reaches the distant protective stop")
        if exit_time:
            ledger.fill("TP", exit_time, Decimal(task["modified_tp"]))
            ledger.cancel("SL", exit_time)
            states["TP"].update(status="FILLED", filled=200)
        else:
            ledger.cancel("SL", end, after_command=True)
            ledger.cancel("TP", end, after_command=True)
    elif case == "delayed_insert":
        arrival = str(pd.Timestamp(start) + pd.Timedelta(seconds=task["insert_delay"]))
        ledger.fill("ENTRY", arrival, prices[arrival]["ask"], after_command=True)
    else:
        ledger.fill("ENTRY", start, prices[start]["ask"], after_command=True)
        ledger.events.append({"type": "cancel_request", "id": "ENTRY", "time": start})
        arrival = str(pd.Timestamp(start) + pd.Timedelta(seconds=task["cancel_delay"]))
        next_fill = next((t for t, p in prices.items() if start < t < arrival
                          and p["ask"] <= Decimal(task["entry"])), None)
        if next_fill:
            ledger.fill("ENTRY", next_fill, Decimal(task["entry"]))
            rejections.append({"id": "ENTRY", "time": arrival, "ns": utc_ns(arrival) + 1})
        else:
            ledger.cancel("ENTRY", arrival, after_command=True)
            states["ENTRY"].update(status="CANCELED", filled=100)
    return {"events": ledger.events, "updates": updates, "cancel_rejections": rejections,
            "orders": states, "cash": str(ledger.cash), "holding": ledger.holding}


def audit(task: dict[str, object], native: dict[str, object], frame: pd.DataFrame) -> dict[str, object]:
    """Require independent linkage, native states, timestamps, fees, inventory and expected races."""
    if native.get("real_orders_enabled") is not False or native["failures"]:
        raise ValueError("Native contingent simulation failed or enabled real routing")
    wanted = reference(task, frame)
    for field in ("events", "updates", "cancel_rejections"):
        if len(wanted[field]) != len(native[field]):
            raise ValueError(f"Contingent {task['case']} {field} count differs: {native[field]} != {wanted[field]}")
        for actual, expected in zip(native[field], wanted[field]):
            for key, value in expected.items():
                equal = (Decimal(str(actual[key])) == Decimal(str(value)) if key in ("price", "fee")
                         else actual[key] == value)
                if not equal:
                    raise ValueError(f"Contingent {task['case']} mismatch: {actual} != {expected}")
            if field == "cancel_rejections" and "not found" not in actual["reason"]:
                raise ValueError("The expected late-cancel rejection was replaced by an unrelated failure")
    if native["orders"] != wanted["orders"]:
        raise ValueError(f"Contingent final native state differs: {native['orders']} != {wanted['orders']}")
    if Decimal(native["cash"]) != Decimal(wanted["cash"]) or native["holding"] != wanted["holding"]:
        raise ValueError("Contingent final account differs from independent actual-fill ledger")
    links = native["links"]
    if task["case"] in CASES[:4]:
        if set(links) != {"ENTRY", "TP", "SL"} or len({v["list"] for v in links.values()}) != 1:
            raise ValueError("Bracket orders lost their shared native list identity")
        if links["ENTRY"]["parent"] is not None or links["ENTRY"]["contingency"] != "OTO":
            raise ValueError("Bracket entry is not the native OTO parent")
        if set(links["ENTRY"]["linked"]) != {"TP", "SL"} or links["ENTRY"]["reduce_only"]:
            raise ValueError("Bracket parent linkage/entry reduction flags differ")
        contingency = "OUO" if task["case"] == "ouo_partial_exit" else "OCO"
        for child, sibling in (("TP", "SL"), ("SL", "TP")):
            if (links[child]["parent"] != "ENTRY" or links[child]["linked"] != [sibling]
                    or links[child]["contingency"] != contingency or not links[child]["reduce_only"]):
                raise ValueError("Bracket child lost its parent, sibling or reduce-only guard")
    elif links:
        raise ValueError("A latency-only order unexpectedly acquired contingent siblings")
    return {"passed": True, "fills": sum(e["type"] == "fill" for e in wanted["events"]),
            "cancels": sum(e["type"] == "cancel" for e in wanted["events"]),
            "child_updates": len(wanted["updates"]), "expected_cancel_rejections": len(wanted["cancel_rejections"]),
            "cash": wanted["cash"], "holding": wanted["holding"]}


def run(response: Path, output: Path, repo: Path) -> dict[str, object]:
    """Execute seven native contingent/latency contracts on all five real holdout sessions."""
    response, output = response.resolve(), output.resolve()
    if response.parent == output or response.parent in output.parents:
        raise ValueError("Contingent evidence must be outside the source capture")
    data = capture(response)
    output.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(response, output / "provider_response.json")
    results = {}
    write_report(output / "status.json", {"state": "running"})
    try:
        for day in data.partitions["test"]:
            frame = data.minutes.loc[day]
            for case in CASES:
                task = make_task(frame, case)
                native = execute(task)
                key = day + "_" + case
                write_report(output / (key + ".json"), {"task": task, "native": native})
                results[key] = audit(task, native, frame)
        report = {"passed": True, "cases": results, "scenarios": len(results), "data": data.provenance,
                  "real_orders_enabled": False, "providers_modified": False, "oto_trigger_mode": "FULL",
                  "limitations": ["Partial OTO release is not certified for native cash-account child checks",
                                  "Latency is prescribed, not calibrated to Futu or any brokerage",
                                  "No restart recovery of orders, books, contingent links or in-flight messages",
                                  "Single instrument, long-only, derived quotes and assumed 100-share liquidity"]}
        write_report(output / "report.json", report)
        write_report(output / "status.json", {"state": "completed"})
        return report
    except Exception:
        write_report(output / "status.json", {"state": "failed"})
        raise


def main() -> None:
    """Expose explicit offline contingent and latency acceptance without live routing."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--response", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    args = parser.parse_args()
    print(json.dumps(run(args.response, args.output, args.repo), indent=2))


if __name__ == "__main__":
    main()
