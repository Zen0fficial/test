"""Native Nautilus data replay and independent audit of saved RL artificial fills."""

from __future__ import annotations

import argparse
import hashlib
from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP
from importlib.metadata import version
from pathlib import Path

import pandas as pd

from .session import read_report, write_report


def native_prices(response: dict[str, object]) -> dict[str, dict[str, Decimal]]:
    """Read actual HK daily bars through Nautilus callbacks, placing no native orders."""
    from nautilus_trader.backtest.engine import BacktestEngine
    from nautilus_trader.config import BacktestEngineConfig, LoggingConfig, StrategyConfig
    from nautilus_trader.model.currencies import HKD
    from nautilus_trader.model.data import Bar, BarType
    from nautilus_trader.model.enums import AccountType, OmsType
    from nautilus_trader.model.identifiers import InstrumentId, Symbol, TraderId, Venue
    from nautilus_trader.model.instruments import Equity
    from nautilus_trader.model.objects import Money, Price, Quantity
    from nautilus_trader.trading.strategy import Strategy

    venue = Venue("FUTU")
    identifier = InstrumentId(Symbol("00700"), venue)
    instrument = Equity(instrument_id=identifier, raw_symbol=Symbol("00700"), currency=HKD,
                        price_precision=9, price_increment=Price.from_str("0.000000001"),
                        lot_size=Quantity.from_int(100), ts_event=0, ts_init=0)
    bar_type = BarType.from_str(f"{identifier}-1-DAY-LAST-EXTERNAL")
    fields = ("open", "high", "low", "close", "volume")
    data, date_map, expected = [], {}, {}
    for row in response["data"]["bars"]:
        # Conservative retrospective after-close delivery, not original receipt evidence.
        stamp = pd.Timestamp(row["date"] + " 17:00:00", tz="Asia/Hong_Kong").value
        date_map[stamp] = row["date"]
        values = {key: Decimal(str(row[key])) for key in fields}
        expected[row["date"]] = values
        prices = {key: Price.from_str(format(values[key], ".9f")) for key in fields[:-1]}
        if values["volume"] != values["volume"].to_integral_value():
            raise ValueError("Nautilus equity replay requires integer volume")
        data.append(Bar(bar_type=bar_type, **prices, volume=Quantity.from_int(int(values["volume"])),
                        ts_event=stamp, ts_init=stamp))

    class Probe(Strategy):
        """Observe real native bars without creating an execution client or an order."""

        def __init__(self) -> None:
            """Configure a data-only callback journal."""
            super().__init__(StrategyConfig(strategy_id="RL-DATA-AUDIT"))
            self.observed: dict[str, dict[str, Decimal]] = {}

        def on_start(self) -> None:
            """Subscribe to the single native daily bar stream."""
            self.subscribe_bars(bar_type)

        def on_bar(self, bar: Bar) -> None:
            """Preserve callback decimals and verify the replay clock is causal."""
            if self.clock.timestamp_ns() < bar.ts_event:
                raise ValueError("Native daily bar delivered before its availability time")
            self.observed[date_map[bar.ts_event]] = {key: getattr(bar, key).as_decimal() for key in fields}

    engine = BacktestEngine(BacktestEngineConfig(trader_id=TraderId("RL-AUDIT-001"),
                                                logging=LoggingConfig(bypass_logging=True)))
    try:
        engine.add_venue(venue=venue, oms_type=OmsType.NETTING, account_type=AccountType.CASH,
                         base_currency=HKD, starting_balances=[Money(100000, HKD)])
        engine.add_instrument(instrument)
        engine.add_data(data)
        probe = Probe()
        engine.add_strategy(probe)
        engine.run()
        if probe.observed != expected or len(engine.cache.orders()) != 0:
            raise ValueError("Nautilus replay changed source bars or created an order")
        return probe.observed
    finally:
        engine.dispose()


def independent_fee(notional: Decimal) -> Decimal:
    """Recalculate the published Tier-I total without calling production fee code."""
    stamp = (notional / 1000).quantize(Decimal(1), rounding=ROUND_CEILING)
    fee = max(Decimal(18), notional / 2000) + notional * Decimal(".000127") + stamp
    return fee.quantize(Decimal(".01"), rounding=ROUND_HALF_UP)


def audit(prices: dict[str, dict[str, Decimal]], simulation: dict[str, object]) -> dict[str, object]:
    """Independently reproduce whole-lot sizing, every fee/cash debit, and closing equity."""
    cash, total_fees, position = Decimal(100000), Decimal(0), 0
    dates = list(prices)
    spread = Decimal(str(simulation["slippage_bps_per_side"])) / 10000
    fills = simulation["ledger"]
    if len(simulation["actions"]) != simulation["days"] or len(fills) != simulation["orders"]:
        raise ValueError("RL journal counts differ from its summary")
    for decision, point in zip(simulation["actions"], simulation["equity"], strict=True):
        day = decision["execution_date"]
        if decision["signal_date"] != dates[dates.index(day) - 1]:
            raise ValueError("Artificial action did not follow its observed signal day")
        buy, sell = prices[day]["open"] * (1 + spread), prices[day]["close"] * (1 - spread)
        action = decision["action"]
        if type(action) is not int or action not in (0, 1, 2):
            raise ValueError("Invalid recorded RL action")
        quantity = int(cash / (buy * 100)) * 100 if action else 0
        if action == 1:
            quantity = min(quantity, 100)
        while quantity and quantity * buy + independent_fee(quantity * buy) > cash:
            quantity -= 100
        if quantity != decision["shares"]:
            raise ValueError("Saved policy size differs from the independent cash/lot calculation")
        if quantity:
            for side, price, sign in (("buy", buy, -1), ("sell", sell, 1)):
                fill = fills[position]
                fee = independent_fee(quantity * price)
                cash += sign * quantity * price - fee
                total_fees += fee
                if (fill["side"] != side or fill["shares"] != quantity or Decimal(fill["price"]) != price
                        or Decimal(fill["fees"]["total"]) != fee or Decimal(fill["cash_after"]) != cash or cash < 0):
                    raise ValueError("Artificial fill does not reconcile independently")
                position += 1
        if abs(float(cash) - point["equity_hkd"]) > 1e-8:
            raise ValueError("Closing equity does not reconcile")
    if (position != len(fills) or float(total_fees) != simulation["fees_hkd"]
            or abs(float(cash) - simulation["ending_equity_hkd"]) > 1e-8):
        raise ValueError("Final artificial ledger does not reconcile")
    return {"passed": True, "fills_checked": position, "ending_equity_hkd": float(cash),
            "fees_hkd": float(total_fees)}


def verify(root: Path) -> dict[str, object]:
    """Verify every walk-forward/control scenario against independently replayed real prices."""
    report = read_report(root / "report.json")
    raw = (root / "provider_response.json").read_bytes()
    if hashlib.sha256(raw).hexdigest() != report["response_sha256"]:
        raise ValueError("RL source snapshot hash changed")
    prices = native_prices(read_report(root / "provider_response.json"))
    files = sorted(root.glob("simulation_*.json"))
    if {path.stem for path in files} != {f"simulation_{name}" for name in report["simulations"]}:
        raise ValueError("Missing or unexpected RL scenario journals")
    checks = {path.stem: audit(prices, read_report(path)) for path in files}
    return {"passed": True, "native_platform": "NautilusTrader", "version": version("nautilus_trader"),
            "native_bars_checked": len(prices), "exact_native_decimals": True,
            "checks": checks, "native_orders": 0, "real_orders_enabled": False,
            "scope": "Native data replay and independent paper accounting; not native order execution"}


def main() -> None:
    """Write independent verification beside a completed RL comparison."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    args = parser.parse_args()
    destination = args.run / "verification.json"
    if destination.exists():
        raise ValueError("Refusing to overwrite verification evidence")
    result = verify(args.run)
    write_report(destination, result)
    print(f"RL native/data/ledger audit passed for {result['native_bars_checked']} bars")


if __name__ == "__main__":
    main()
