"""Live NautilusTrader data client for the fixed Futu loopback SSE API."""

import argparse
import asyncio
from collections import Counter
from pathlib import Path
import queue
import time

from .capture import write_json
from .live_source import Journal, LocalSSE, TickSelector, hk_symbol
from .validation import number


async def run(base_url, symbol, output, seconds=0, disconnect_after=0):
    import nautilus_trader
    from nautilus_trader.common.providers import InstrumentProvider
    from nautilus_trader.config import LiveDataClientConfig, LiveExecEngineConfig, LoggingConfig, StrategyConfig, TradingNodeConfig
    from nautilus_trader.live.data_client import LiveMarketDataClient
    from nautilus_trader.live.factories import LiveDataClientFactory
    from nautilus_trader.live.node import TradingNode
    from nautilus_trader.model.currencies import HKD
    from nautilus_trader.model.data import QuoteTick, TradeTick
    from nautilus_trader.model.enums import AggressorSide
    from nautilus_trader.model.identifiers import ClientId, InstrumentId, Symbol, TraderId, TradeId, Venue
    from nautilus_trader.model.instruments import Equity
    from nautilus_trader.model.objects import Price, Quantity
    from nautilus_trader.trading.strategy import Strategy

    if not nautilus_trader.__version__.startswith("1.231."):
        raise ValueError("Use the separately pinned NautilusTrader 1.231.x environment")
    symbol = hk_symbol(symbol)
    root = Path(output).resolve()
    root.mkdir(parents=True, exist_ok=False)
    stream = LocalSSE(base_url, symbol, root, disconnect_after=disconnect_after)
    selector = TickSelector(symbol)
    admitted, callbacks = Journal(root / "admitted.jsonl"), Journal(root / "callbacks.jsonl")
    counts, pending = Counter(), {}
    instrument_id = InstrumentId(Symbol(symbol[3:]), Venue("SEHK"))
    instrument = Equity(instrument_id=instrument_id, raw_symbol=Symbol(symbol[3:]), currency=HKD,
                        price_precision=9, price_increment=Price.from_str("0.000000001"),
                        lot_size=Quantity.from_int(1), ts_event=0, ts_init=0)

    def price(value):
        decimal = number(value)
        result = Price.from_str(format(decimal, ".9f"))
        if result.as_decimal() != decimal: raise ValueError("Price precision loss")
        return result

    def size(value):
        decimal = number(value)
        if decimal != decimal.to_integral_value(): raise ValueError("Fractional HK equity size is unsupported")
        return Quantity.from_int(int(decimal))

    class Client(LiveMarketDataClient):
        def __init__(self, loop, msgbus, cache, clock, config):
            provider = InstrumentProvider()
            provider.add(instrument)
            super().__init__(loop=loop, client_id=ClientId("FUTU"), venue=Venue("SEHK"), msgbus=msgbus,
                             cache=cache, clock=clock, instrument_provider=provider, config=config)
            self.pump_task = None
            self.active = True

        async def _connect(self):
            self._handle_data_py(instrument)
            stream.start()
            self.pump_task = asyncio.create_task(self.pump())

        async def _subscribe_trade_ticks(self, command): pass
        async def _subscribe_quote_ticks(self, command): pass
        async def _unsubscribe_trade_ticks(self, command): pass
        async def _unsubscribe_quote_ticks(self, command): pass

        async def pump(self):
            while self.active or not stream.events.empty():
                try:
                    # Wake on data arrival instead of adding up to 10 ms of
                    # polling latency. A bounded wait also permits shutdown
                    # when the market is quiet; the event loop never blocks.
                    envelope = await asyncio.to_thread(stream.events.get, True, 0.1)
                except queue.Empty:
                    continue
                try:
                    for row in selector.select(envelope):
                        if row["kind"] == "trade":
                            tick = TradeTick(instrument_id=instrument_id, price=price(row["price"]), size=size(row["size"]),
                                             aggressor_side=AggressorSide.NO_AGGRESSOR, trade_id=TradeId(row["trade_id"]),
                                             ts_event=row["event_ns"], ts_init=row["received_ns"])
                        else:
                            tick = QuoteTick(instrument_id=instrument_id, bid_price=price(row["bid"]), ask_price=price(row["ask"]),
                                             bid_size=size(row["bid_size"]), ask_size=size(row["ask_size"]),
                                             ts_event=row["event_ns"], ts_init=row["received_ns"])
                        key = (row["kind"], row["received_ns"], row.get("trade_id", ""))
                        if key in pending: raise ValueError("Duplicate live delivery key")
                        pending[key] = row
                        admitted.write(row)
                        counts["admitted_" + row["kind"]] += 1
                        self._handle_data_py(tick)
                except Exception as exc:
                    counts["conversion_errors"] += 1
                    stream._status("conversion_error", reason=str(exc), unknown_gap=True)

        async def quiesce(self):
            await asyncio.to_thread(stream.close)
            self.active = False
            if self.pump_task:
                await self.pump_task
            await asyncio.sleep(0.25)

        async def _disconnect(self):
            await self.quiesce()

    clients = []

    class Factory(LiveDataClientFactory):
        @staticmethod
        def create(loop, name, config, msgbus, cache, clock):
            client = Client(loop, msgbus, cache, clock, config)
            clients.append(client)
            return client

    class Observer(Strategy):
        def __init__(self):
            super().__init__(StrategyConfig(strategy_id="HK-LIVE-OBSERVER"))

        def on_start(self):
            self.subscribe_quote_ticks(instrument_id)
            self.subscribe_trade_ticks(instrument_id)

        def record(self, kind, tick, values):
            key = (kind, tick.ts_init, str(tick.trade_id) if kind == "trade" else "")
            row = pending.pop(key)
            result = {**row, **values, "event_ns": tick.ts_event, "received_ns": tick.ts_init,
                      "callback_ns": time.time_ns(), "instrument_id": str(tick.instrument_id)}
            callbacks.write(result)
            counts["callbacks_" + kind] += 1
            if row["connection"] > 1: counts["callbacks_after_reconnect"] += 1

        def on_trade_tick(self, tick):
            self.record("trade", tick, {"price": str(tick.price.as_decimal()), "size": str(tick.size.as_decimal())})

        def on_quote_tick(self, tick):
            self.record("quote", tick, {"bid": str(tick.bid_price.as_decimal()), "ask": str(tick.ask_price.as_decimal()),
                                        "bid_size": str(tick.bid_size.as_decimal()), "ask_size": str(tick.ask_size.as_decimal())})

    node = TradingNode(TradingNodeConfig(trader_id=TraderId("HK-LIVE-001"), logging=LoggingConfig(log_level="ERROR"),
                       exec_engine=LiveExecEngineConfig(reconciliation=False),
                       data_clients={"FUTU": LiveDataClientConfig()}, exec_clients={}, timeout_post_stop=0.5),
                       loop=asyncio.get_running_loop())
    node.add_data_client_factory("FUTU", Factory)
    node.trader.add_strategy(Observer())
    node.build()
    task = asyncio.create_task(node.run_async())
    try:
        started = time.monotonic()
        while not seconds or time.monotonic() - started < seconds:
            await asyncio.sleep(0.25)
            if (root / "STOP").exists(): break
            if task.done():
                await task
                raise RuntimeError("Nautilus live node stopped unexpectedly")
    finally:
        for client in clients: await client.quiesce()
        await node.stop_async()
        await task
        report = {"platform": "NautilusTrader", "version": nautilus_trader.__version__, "mode": "live-data-only",
                  "symbol": symbol, "transport": stream.status(), "selection": dict(selector.counts), "counts": dict(counts),
                  "pending_callbacks": len(pending), "live_orders": len(node.kernel.cache.orders()), "execution_clients": 0,
                  "limitations": ["No exchange replay across disconnects", "Book timestamps use provider observation time",
                                  "Instrument increment/lot size are data precision settings, not verified trading rules"]}
        write_json(root / "run.json", report)
        # asyncio.run owns this loop. TradingNode.dispose also stops/closes it,
        # so release the stopped kernel here and let asyncio own loop cleanup.
        node.kernel.dispose()
    return report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base-url", default="http://127.0.0.1:8765")
    p.add_argument("--symbol", default="00700")
    p.add_argument("--output", required=True)
    p.add_argument("--seconds", type=float, default=0, help="0 runs continuously until interrupted")
    p.add_argument("--disconnect-after", type=float, default=0, help="One converter-only disconnect for testing")
    args = p.parse_args()
    if args.seconds < 0 or args.disconnect_after < 0: p.error("Durations must be nonnegative")
    try:
        report = asyncio.run(run(**vars(args)))
        print(report)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
