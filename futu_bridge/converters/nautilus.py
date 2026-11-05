"""NautilusTrader 1.231 converter and data-only historical replay probe."""

import argparse
from decimal import Decimal
from pathlib import Path

from .capture import load_capture, sha256, write_json
from .validation import FIELDS, bar_end, bars, compare_rows, expected_rows, nanoseconds, number, probabilities, ticker


def run(capture_dir, output):
    # Deliberately lazy: installing/importing the bridge never imports Nautilus.
    import nautilus_trader
    from nautilus_trader.backtest.engine import BacktestEngine
    from nautilus_trader.config import BacktestEngineConfig, LoggingConfig, StrategyConfig
    from nautilus_trader.core.data import Data
    from nautilus_trader.model.currencies import USD
    from nautilus_trader.model.data import Bar, BarType, CustomData, DataType
    from nautilus_trader.model.enums import AccountType, OmsType
    from nautilus_trader.model.identifiers import ClientId, InstrumentId, Symbol, TraderId, Venue
    from nautilus_trader.model.instruments import Equity
    from nautilus_trader.model.objects import Money, Price, Quantity
    from nautilus_trader.trading.strategy import Strategy

    if not nautilus_trader.__version__.startswith("1.231."):
        raise RuntimeError("This converter targets NautilusTrader 1.231.x; 2.x requires a separate converter")
    manifest, responses = load_capture(capture_dir)
    equity_inputs, probability_inputs = list(bars(manifest, responses)), list(probabilities(manifest, responses))
    root = Path(output).resolve()
    root.mkdir(parents=True, exist_ok=False)
    venue = Venue("FUTU")
    instruments, types, data, symbol_map = [], [], [], {}
    for symbol, response in equity_inputs:
        instrument_id = InstrumentId(Symbol(ticker(symbol)), venue)
        symbol_map[str(instrument_id)] = symbol
        # Nine decimal places preserve bridge prices. This is replay precision,
        # not a claim that the exchange accepts a 1e-9 order tick size.
        instrument = Equity(instrument_id=instrument_id, raw_symbol=Symbol(ticker(symbol)), currency=USD,
                            price_precision=9, price_increment=Price.from_str("0.000000001"),
                            lot_size=Quantity.from_int(1), ts_event=0, ts_init=0)
        instruments.append(instrument)
        bar_type = BarType.from_str(f"{instrument_id}-1-MINUTE-LAST-EXTERNAL")
        types.append(bar_type)
        for row in response["data"]["bars"]:
            stamp = nanoseconds(bar_end(row, manifest["bar_timestamp"]))
            prices = {}
            for field in FIELDS[:4]:
                value = number(row[field])
                if value != value.quantize(Decimal("0.000000001")):
                    raise ValueError("Nautilus replay precision would lose source price digits")
                prices[field] = Price.from_str(format(value, ".9f"))
            volume = number(row["volume"])
            if volume != volume.to_integral_value():
                raise ValueError("The Nautilus Equity instrument requires integer share volume")
            data.append(Bar(bar_type=bar_type, **prices, volume=Quantity.from_int(int(volume)),
                            ts_event=stamp, ts_init=stamp))

    class Probability(Data):
        def __init__(self, token_id, price, event_ns, available_ns):
            self.token_id, self.price = token_id, price
            self._event_ns, self._available_ns = event_ns, available_ns

        @property
        def ts_event(self):
            return self._event_ns

        @property
        def ts_init(self):
            return self._available_ns

    data_type = DataType(Probability)
    custom = []
    for outcome, rows in probability_inputs:
        for row, available in rows:
            point = Probability(outcome["token_id"], number(row["price"]),
                                int(number(row["timestamp"])) * 1_000_000_000, nanoseconds(available))
            custom.append(CustomData(data_type, point))

    class Probe(Strategy):
        def __init__(self):
            super().__init__(StrategyConfig(strategy_id="LOCAL-PROBE"))
            self.rows = []

        def on_start(self):
            for bar_type in types:
                self.subscribe_bars(bar_type)
            self.subscribe_data(data_type, client_id=ClientId("POLYMARKET"))

        def on_bar(self, bar):
            if self.clock.timestamp_ns() < bar.ts_event:
                raise ValueError("Nautilus delivered a bar before its close")
            identity = str(bar.bar_type.instrument_id)
            self.rows.append(["bar", symbol_map[identity], str(bar.ts_event),
                              *[str(getattr(bar, field).as_decimal()) for field in FIELDS]])

        def on_data(self, point):
            if self.clock.timestamp_ns() < point.ts_init:
                raise ValueError("Nautilus delivered a probability before the bucket end")
            self.rows.append(["probability", point.token_id, str(point.ts_init), str(point.price)])

    engine = BacktestEngine(BacktestEngineConfig(trader_id=TraderId("LOCAL-001"), logging=LoggingConfig(bypass_logging=True)))
    try:
        engine.add_venue(venue=venue, oms_type=OmsType.NETTING, account_type=AccountType.CASH,
                         base_currency=USD, starting_balances=[Money(1000000, USD)])
        for instrument in instruments:
            engine.add_instrument(instrument)
        engine.add_data(data)
        engine.add_data(custom, client_id=ClientId("POLYMARKET"))
        strategy = Probe()
        engine.add_strategy(strategy)
        engine.run()
        submitted_orders = len(engine.cache.orders())
        write_json(root / "engine_rows.json", {"rows": strategy.rows, "submitted_orders": submitted_orders})
        report = compare_rows(strategy.rows, expected_rows(manifest, responses))
        if submitted_orders != 0:
            raise ValueError("The data-only observer submitted an order")
        report.update(platform="NautilusTrader", version=nautilus_trader.__version__, submitted_orders=submitted_orders,
                      live_orders=0, capture_sha256=sha256(Path(capture_dir) / "manifest.json"),
                      purpose="Data compatibility only; trading is manual",
                      replay_price_precision=9, real_exchange_tick_size_verified=False)
        write_json(root / "verification.json", report)
        return report
    finally:
        engine.dispose()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--capture", required=True)
    p.add_argument("--output", required=True)
    args = p.parse_args()
    print(run(args.capture, args.output))


if __name__ == "__main__":
    main()
