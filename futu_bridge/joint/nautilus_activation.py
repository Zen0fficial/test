"""Native activation-price trailing orders on captured prices, with a causal Decimal oracle.

This extends the existing offline factory adapter. Quotes are derived from real
minute opens; 100-share depth and the five-bps spread remain simulation assumptions.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import pandas as pd
from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.config import BacktestEngineConfig, LoggingConfig
from nautilus_trader.model.currencies import HKD
from nautilus_trader.model.enums import AccountType, BookType, OmsType, OrderSide, TriggerType
from nautilus_trader.model.identifiers import ClientOrderId, InstrumentId, Symbol, Venue
from nautilus_trader.model.instruments import Equity
from nautilus_trader.model.objects import Money, Quantity

from .lifecycle import quotes
from .lifecycle_nautilus import CumulativeFees
from .nautilus_features_contingent import source_quotes
from .nautilus_order_extensions import feed, observations, price
from .nautilus_order_matrix import MatrixStrategy
from .paper_common import capture, utc_ns
from .session import write_report
from .storage.snapshots import file_hash


class ActivationStrategy(MatrixStrategy):
    """Use native activation gates while retaining the existing native event recorder."""

    def create_order(self) -> object:
        """Build a price-activated native trailing market or trailing limit ticket."""
        common = (self.identity, OrderSide.BUY if self.task["side"] == "buy" else OrderSide.SELL,
                  Quantity.from_int(100))
        if self.task["kind"] == "market_to_limit":
            return self.order_factory.market_to_limit(*common, client_order_id=ClientOrderId("TARGET"))
        extras = {"activation_price": price(self.task["activation"]), "trailing_offset": Decimal(".4"),
                  "trigger_type": TriggerType.BID_ASK, "client_order_id": ClientOrderId("TARGET")}
        if self.task["kind"] == "market":
            return self.order_factory.trailing_stop_market(*common, **extras)
        return self.order_factory.trailing_stop_limit(*common, limit_offset=Decimal(".4"), **extras)


def task_for(frame: pd.DataFrame, side: str, kind: str, dormant: bool = False) -> dict[str, object]:
    """Choose gates only from the quote available at the prescribed submission time."""
    if side not in ("buy", "sell") or kind not in ("market", "limit", "market_to_limit"):
        raise ValueError("Unsupported activation-price scenario")
    points, day = quotes(frame), str(frame.index[0].date())
    start = day + " 10:00:00" if side == "sell" else points[0]["time"]
    current = next(p for p in points if p["time"] == start)
    value = Decimal(current["ask" if side == "buy" else "bid"])
    distance = Decimal(100) if dormant else Decimal(".4")
    return {"quotes": points, "side": side, "kind": kind, "dormant": dormant,
            "submit": start, "seed": points[0]["time"] if side == "sell" else None,
            "modify": day + " 10:00:00", "cancel": day + " 15:29:00",
            "activation": str(value - distance if side == "buy" else value + distance)}


def execute(task: dict[str, object]) -> dict[str, object]:
    """Run native activation and matching only in BacktestEngine's simulated cash account."""
    venue, identity = Venue("HKCONTINGENT"), InstrumentId.from_str("00700.HKCONTINGENT")
    engine = BacktestEngine(BacktestEngineConfig(logging=LoggingConfig(bypass_logging=True)))
    try:
        engine.add_venue(venue, oms_type=OmsType.NETTING, account_type=AccountType.CASH,
            base_currency=HKD, starting_balances=[Money(100000, HKD)], book_type=BookType.L2_MBP,
            liquidity_consumption=True, fee_model=CumulativeFees())
        engine.add_instrument(Equity(identity, Symbol("00700"), HKD, 1, price(".2"), Quantity.from_int(100), 0, 0))
        strategy = ActivationStrategy(identity, task)
        engine.add_strategy(strategy)
        feed(engine, observations(task))
        order = strategy.orders["TARGET"]
        result = {"events": strategy.events, "rejections": strategy.rejections, "failures": strategy.failures,
                  "activated": getattr(order, "is_activated", False), "status": order.status.name,
                  "filled": int(order.filled_qty.as_decimal()),
                  "cash": str(engine.cache.account_for_venue(venue).balance_total(HKD).as_decimal()),
                  "holding": sum(p.signed_qty for p in engine.cache.positions_open()), "real_orders_enabled": False}
        engine.end()
        return result
    finally:
        engine.dispose()


def expected(task: dict[str, object], frame: pd.DataFrame) -> tuple[list[dict[str, object]], int | None]:
    """Reconstruct activation, favorable trailing and fills without reading native state."""
    source = source_quotes(frame)
    buy = task["side"] == "buy"
    activation, stop, activated, triggered = Decimal(task["activation"]), None, None, False
    wanted = []
    if task["seed"] is not None:
        wanted.append({"id": "SEED", "ns": utc_ns(task["seed"]) + 1,
                       "price": source[task["seed"]]["ask"], "shares": 100})
    if task["kind"] == "market_to_limit":
        wanted.append({"id": "TARGET", "ns": utc_ns(task["submit"]) + 1,
                       "price": source[task["submit"]]["ask" if buy else "bid"], "shares": 100})
        return wanted, None
    for time, point in source.items():
        if not task["submit"] < time <= task["cancel"]:
            continue
        value = point["ask" if buy else "bid"]
        if activated is None:
            if value <= activation if buy else value >= activation:
                activated = utc_ns(time)
                stop = value + Decimal(".4") if buy else value - Decimal(".4")
            continue
        if not triggered and (value >= stop if buy else value <= stop):
            triggered = True
        if triggered:
            if task["kind"] == "market":
                wanted.append({"id": "TARGET", "ns": utc_ns(time), "price": value, "shares": 100})
                break
            if value <= stop if buy else value >= stop:
                wanted.append({"id": "TARGET", "ns": utc_ns(time), "price": stop, "shares": 100})
                break
        else:
            candidate = value + Decimal(".4") if buy else value - Decimal(".4")
            stop = min(stop, candidate) if buy else max(stop, candidate)
    return wanted, activated


def audit(task: dict[str, object], actual: dict[str, object], frame: pd.DataFrame) -> dict[str, object]:
    """Check every fill, no pre-activation updates, and fully funded costs and final holdings."""
    wanted, activated = expected(task, frame)
    if actual["failures"] or actual["rejections"] or actual["real_orders_enabled"]:
        raise ValueError("Native activation scenario failed or enabled real execution")
    fills = [e for e in actual["events"] if e["type"] == "fill"]
    observed = [{"id": e["id"], "ns": e["ns"], "price": Decimal(e["price"]), "shares": e["shares"]}
                for e in fills]
    if observed != wanted or (task["kind"] != "market_to_limit" and actual["activated"] != (activated is not None)):
        raise ValueError(f"Native activation/fills differ from source oracle: {observed} != {wanted}")
    updates = [e for e in actual["events"] if e["type"] == "update"]
    if task["kind"] != "market_to_limit" and any(activated is None or e["ns"] < activated for e in updates):
        raise ValueError("Dormant trailing order changed before its real activation crossing")
    cash, holding = Decimal(100000), 0
    for event in fills:
        side = "buy" if event["id"] == "SEED" else task["side"]
        value = Decimal(event["price"]) * 100
        fee = (max(Decimal(18), value * Decimal(".0005")) + value * Decimal(".000127") +
               (value * Decimal(".001")).to_integral_value(rounding="ROUND_CEILING")).quantize(
                   Decimal(".01"), rounding=ROUND_HALF_UP)
        if Decimal(event["fee"]) != fee:
            raise ValueError("Native activation fill fee differs from independent Decimal charges")
        sign = 1 if side == "buy" else -1
        cash -= sign * value + fee
        holding += sign * 100
        if cash < 0 or holding < 0:
            raise ValueError("Activation ticket exceeded funded long-only exposure")
    filled = sum(e["shares"] for e in fills if e["id"] == "TARGET")
    if (cash != Decimal(actual["cash"]) or holding != actual["holding"] or filled != actual["filled"]
            or actual["status"] != ("FILLED" if filled else "CANCELED")):
        raise ValueError("Native activation account or terminal order state differs from independent oracle")
    return {"passed": True, "activation_ns": activated, "fills": len(fills), "cash": str(cash), "holding": holding}


def run(response: Path, output: Path, repo: Path) -> dict[str, object]:
    """Verify both native trailing factories and unactivated cancellation across five real sessions."""
    data = capture(response)
    output.mkdir(parents=True, exist_ok=False)
    sources = [Path(__file__), Path(__file__).with_name("nautilus_order_matrix.py"),
               Path(__file__).with_name("nautilus_order_extensions.py"),
               repo / "requirements/joint-py312-windows.lock"]
    bindings = {str(p.resolve()): file_hash(p) for p in sources}
    results = {}
    for day in data.partitions["test"]:
        for side in ("buy", "sell"):
            for kind in ("market", "limit"):
                for dormant in (False, True):
                    task = task_for(data.minutes.loc[day], side, kind, dormant)
                    actual = execute(task)
                    name = f"{day}_{side}_{kind}_{'dormant' if dormant else 'active'}"
                    write_report(output / (name + ".json"), {"task": task, "native": actual})
                    results[name] = audit(task, actual, data.minutes.loc[day])
        task = task_for(data.minutes.loc[day], "sell", "market_to_limit")
        actual = execute(task)
        name = f"{day}_funded_sell_market_to_limit"
        write_report(output / (name + ".json"), {"task": task, "native": actual})
        results[name] = audit(task, actual, data.minutes.loc[day])
    if any(file_hash(Path(p)) != value for p, value in bindings.items()):
        raise ValueError("Activation adapter sources changed during acceptance")
    result = {"passed": True, "scenarios": len(results), "cases": results, "data": data.provenance,
              "source_bindings": bindings, "real_orders_enabled": False, "providers_modified": False,
              "scope": "Native activation gates and funded sell market-to-limit; derived BBO and100-share depth"}
    write_report(output / "report.json", result)
    return result


def main() -> None:
    """Expose activation acceptance over fixed captures without any live execution client."""
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--response", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = run(args.response, args.output, Path.cwd())
    print(f"Verified {result['scenarios']} native activation scenarios")


if __name__ == "__main__":
    main()
