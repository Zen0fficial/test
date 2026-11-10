"""Offline native Nautilus execution over simulated quotes from real minute opens.

Only BacktestEngine is constructed. Quote size is an explicit simulation
assumption (200 shares), not observed depth. Native matching, cash accounts,
positions and order events are retained; no live node or broker is imported.
"""

from __future__ import annotations

from decimal import Decimal

import pandas as pd
import nautilus_trader
from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.backtest.models import FeeModel
from nautilus_trader.config import BacktestEngineConfig, LoggingConfig, StrategyConfig
from nautilus_trader.model.currencies import HKD
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.enums import AccountType, OmsType, OrderSide
from nautilus_trader.model.identifiers import InstrumentId, Symbol, TraderId, Venue
from nautilus_trader.model.instruments import Equity
from nautilus_trader.model.objects import Money, Price, Quantity
from nautilus_trader.trading.strategy import Strategy

from .costs import tiered_fee
from .paper_common import CAPITAL, execution_price, schedule, utc_ns
from .qlib_rl_data import ExecutionData


class HKFeeModel(FeeModel):
    """Debit the existing HK IBKR tiered scenario inside native account updates."""

    def get_commission(self, order: object, fill_qty: Quantity, fill_px: Price, instrument: Equity) -> Money:
        """Compute all included charges on the actual native fill's notional."""
        return Money(tiered_fee(fill_qty.as_decimal() * fill_px.as_decimal()).total, HKD)


class PaperStrategy(Strategy):
    """Prescribed, fully funded orders in a local BacktestEngine only."""

    def __init__(self, instrument_id: InstrumentId, commands: list[dict[str, object]]) -> None:
        """Bind an immutable schedule before any quote is processed."""
        super().__init__(StrategyConfig(strategy_id="HK-PAPER"))
        self.instrument_id = instrument_id
        self.commands = {utc_ns(c["time"]): c for c in commands}
        self.fills: list[dict[str, object]] = []
        self.failures: list[str] = []

    def on_start(self) -> None:
        """Subscribe to the backtest's local quote stream."""
        self.subscribe_quote_ticks(self.instrument_id)

    def on_quote_tick(self, tick: QuoteTick) -> None:
        """Submit at a predetermined observed instant using the native order factory."""
        command = self.commands.pop(tick.ts_event, None)
        if command is not None:
            if command["shares"] <= 0 or command["shares"] % 100:
                raise ValueError("Offline HK orders require positive 100-share lots")
            if command["side"] == "buy":
                notional = tick.ask_price.as_decimal() * command["shares"]
                cash = self.cache.account_for_venue(self.instrument_id.venue).balance_free(HKD).as_decimal()
                if notional <= cash < notional + tiered_fee(notional).total:
                    self.failures.append("Fee reservation: cash pays notional but cannot cover actual HK charges")
                    return
            order = self.order_factory.market(
                self.instrument_id, OrderSide.BUY if command["side"] == "buy" else OrderSide.SELL,
                Quantity.from_int(command["shares"]))
            self.submit_order(order)

    def on_order_filled(self, event: object) -> None:
        """Record authoritative native matching-engine fills and commissions."""
        time = pd.Timestamp(event.ts_event, unit="ns", tz="UTC").tz_convert("Asia/Hong_Kong").tz_localize(None)
        self.fills.append({"time": str(time), "side": "buy" if event.order_side == OrderSide.BUY else "sell",
                           "shares": str(event.last_qty.as_decimal()), "price": str(event.last_px.as_decimal()),
                           "notional": str(event.last_qty.as_decimal() * event.last_px.as_decimal()),
                           "fee": str(event.commission.as_decimal()), "status": "filled"})

    def on_order_denied(self, event: object) -> None:
        """Retain native risk denials for verification instead of hiding them."""
        self.failures.append(str(event))

    def on_order_rejected(self, event: object) -> None:
        """Retain native exchange rejections for verification."""
        self.failures.append(str(event))


def run(data: ExecutionData, days: list[str], *, variant: str = "split", bps: int = 5,
        capital: Decimal = CAPITAL) -> dict[str, object]:
    """Run native simulated orders, matching, fees and HKD cash accounting."""
    if not nautilus_trader.__version__.startswith("1.231."):
        raise RuntimeError("Native paper adapter requires the pinned Nautilus 1.231.x runtime")
    venue = Venue("HKPAPER")
    instrument_id = InstrumentId(Symbol("00700"), venue)
    instrument = Equity(instrument_id, Symbol("00700"), HKD, 1, Price.from_str("0.2"),
                        Quantity.from_int(100), 0, 0, min_quantity=Quantity.from_int(100))
    quotes = []
    for day in days:
        for time, row in data.minutes.loc[day].iterrows():
            reference, stamp = Decimal(str(row.open)), utc_ns(time)
            quotes.append(QuoteTick(instrument_id,
                                    Price.from_str(str(execution_price(reference, "sell", bps))),
                                    Price.from_str(str(execution_price(reference, "buy", bps))),
                                    Quantity.from_int(200), Quantity.from_int(200), stamp, stamp))
    engine = BacktestEngine(BacktestEngineConfig(trader_id=TraderId("PAPER-001"),
                                               logging=LoggingConfig(bypass_logging=True)))
    try:
        engine.add_venue(venue=venue, oms_type=OmsType.NETTING, account_type=AccountType.CASH,
                         base_currency=HKD, starting_balances=[Money(capital, HKD)], fee_model=HKFeeModel())
        engine.add_instrument(instrument)
        engine.add_data(quotes)
        strategy = PaperStrategy(instrument_id, schedule(days, variant))
        engine.add_strategy(strategy)
        engine.run()
        account = engine.cache.account_for_venue(venue)
        return {"platform": "nautilus", "engine": "BacktestEngine/SimulatedExchange/CashAccount",
                "schedule": variant, "bps": bps, "days": days, "fills": strategy.fills,
                "ending_cash": str(account.balance_total(HKD).as_decimal()),
                "ending_holding": sum(p.signed_qty for p in engine.cache.positions_open()),
                "order_count": len(engine.cache.orders()), "failures": strategy.failures,
                "unconsumed_commands": len(strategy.commands), "real_orders_enabled": False}
    finally:
        engine.dispose()
