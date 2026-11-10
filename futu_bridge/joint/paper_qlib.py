"""Full native Qlib nested HK execution with converter-owned calendar and costs.

HKStateAdapter.update adapts Microsoft Qlib 0.9.7's SAOEStateAdapter.update
(Copyright Microsoft Corporation, MIT License). It retains upstream metric
calculations while replacing CN minute indices and generated timestamps with
the observed HK calendar. Native NestedExecutor, SimulatorExecutor, Exchange,
Order, Position and Account perform execution and account updates.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import struct

import numpy as np
import pandas as pd
import qlib
from qlib.backtest.decision import Order, TradeDecisionWO
from qlib.backtest.exchange import Exchange
from qlib.config import REG_US
from qlib.rl.data.native import IntradayBacktestData
from qlib.rl.order_execution.simulator_qlib import SingleAssetOrderExecution
from qlib.rl.order_execution.strategy import ProxySAOEStrategy, SAOEStateAdapter
from qlib.rl.order_execution.utils import dataframe_append
from qlib.strategy.base import BaseStrategy

from .costs import tiered_fee
from .paper_common import CAPITAL, SYMBOL, execution_price, schedule
from .qlib_rl_data import ExecutionData


def prepare_calendar(data: ExecutionData, root: Path) -> None:
    """Publish native calendars and a benchmark series without altering exporters."""
    folder = root / "calendars"
    folder.mkdir(parents=True)
    days = pd.DatetimeIndex(sorted(data.blocks))
    calendars = {"day": days, "1min": data.minutes.index,
                 "30min": pd.DatetimeIndex([t for frame in data.blocks.values() for t in frame.index])}
    for frequency, index in calendars.items():
        sentinel = days[-1] + pd.Timedelta(days=1) if frequency == "day" else days[-1] + pd.Timedelta(hours=16)
        text = "\n".join(str(t) for t in index) + "\n"
        (folder / f"{frequency}.txt").write_text(text, encoding="utf-8")
        (folder / f"{frequency}_future.txt").write_text(text + str(sentinel) + "\n", encoding="utf-8")
    instruments = root / "instruments"
    instruments.mkdir()
    (instruments / "all.txt").write_text(f"{SYMBOL}\t{days[0]}\t{days[-1]}\n", encoding="utf-8")
    features = root / "features" / SYMBOL.lower()
    features.mkdir(parents=True)
    closes = [float(data.blocks[str(day.date())].close.iloc[-1]) for day in days]
    (features / "close.day.bin").write_bytes(struct.pack("<" + "f" * (len(closes) + 1), 0, *closes))
    qlib.init(provider_uri=str(root), region=REG_US, expression_cache=None, dataset_cache=None,
              logging_level=40, trade_unit=100)


class HKExchange(Exchange):
    """Native exchange clipping/accounting, with exact source quotes and IBKR fees."""

    def __init__(self, frame: pd.DataFrame, *, bps: int = 5, volume_limit: float | None = None) -> None:
        """Bind one real session; an optional cap exercises native liquidity clipping."""
        if volume_limit is not None and not 0 < volume_limit <= 1:
            raise ValueError("Volume participation must be in (0, 1]")
        self.frame, self.bps = frame, bps
        self.volume_limit = volume_limit
        self.journal: list[dict[str, object]] = []
        super().__init__(freq="1min", start_time=frame.index[0], end_time=frame.index[-1],
                         codes=[SYMBOL], deal_price=("$buy", "$sell"), trade_unit=100,
                         open_cost=0, close_cost=0, min_cost=0,
                         volume_threshold=("current", "$capacity") if volume_limit else None)

    def get_quote_from_qlib(self) -> None:
        """Supply original capture precision through the native exchange data hook."""
        source = self.frame
        quotes = pd.DataFrame(index=pd.MultiIndex.from_product([[SYMBOL], source.index],
                                                               names=["instrument", "datetime"]))
        for field in ("close", "volume"):
            quotes["$" + field] = source[field].to_numpy(dtype=float)
        quotes["$factor"], quotes["$change"] = 1.0, source.close.pct_change().fillna(0).to_numpy()
        if self.volume_limit is not None:
            quotes["$capacity"] = source.volume.to_numpy(dtype=float) * self.volume_limit
        for side in ("buy", "sell"):
            quotes["$" + side] = [float(execution_price(Decimal(str(p)), side, self.bps)) for p in source.open]
        self.quote_df = quotes
        self.trade_w_adj_price = False
        self._update_limit(self.limit_threshold)

    def _calc_trade_info_by_order(self, order: Order, position: object,
                                  dealt_order_amount: dict) -> tuple[float, float, float]:
        """Retain native clipping then reserve actual fees before committing a fill."""
        price, _, _ = super()._calc_trade_info_by_order(order, position, dealt_order_amount)
        quantity = int(order.deal_amount)
        while quantity > 0:
            notional = Decimal(str(price)) * quantity
            fee = tiered_fee(notional).total
            if (order.direction == Order.SELL or position is None
                    or notional + fee <= Decimal(str(position.get_cash()))):
                order.deal_amount = quantity
                return price, float(notional), float(fee)
            quantity -= 100
        order.deal_amount = 0
        return price, 0.0, 0.0

    def deal_order(self, order: Order, trade_account: object = None, position: object = None,
                   dealt_order_amount: dict | None = None) -> tuple[float, float, float]:
        """Record actual native order results after native Account/Position updates."""
        amounts = {} if dealt_order_amount is None else dealt_order_amount
        result = super().deal_order(order, trade_account, position, amounts)
        value, fee, price = result
        account_position = trade_account.current_position if trade_account else position
        self.journal.append({"time": str(order.start_time), "side": "buy" if order.direction == Order.BUY else "sell",
                             "requested": float(order.amount), "shares": float(order.deal_amount),
                             "price": str(price) if value else None, "notional": str(value), "fee": str(fee),
                             "cash_after": str(account_position.get_cash()),
                             "holding_after": float(account_position.get_stock_amount(SYMBOL)),
                             "status": "filled" if value else "unfilled"})
        return result


class HKStateAdapter(SAOEStateAdapter):
    """Use observed HK indices and lunch gaps while retaining native SAOE metrics."""

    def update(self, execute_result: list, last_step_range: tuple[int, int]) -> None:
        """Accumulate actual fills at their calendar indices, never CN clock offsets."""
        start, end = last_step_range
        ticks = self.backtest_data.ticks_index[start:end + 1]
        if not len(ticks):
            raise ValueError("Empty native execution interval")
        volume = np.zeros(len(ticks))
        for order, _, _, _ in execute_result:
            index = ticks.get_indexer([order.start_time])[0]
            if index < 0:
                raise ValueError("Native fill fell outside its observed HK interval")
            volume[index] += order.deal_amount
        if volume.sum() > self.position + 1e-6:
            raise ValueError("Native fills exceeded the parent order")
        market_volume = np.asarray(self.exchange.get_volume(
            self.order.stock_id, ticks[0], ticks[-1], method=None), dtype=float).reshape(-1)
        market_price = np.asarray(self.exchange.get_deal_price(
            self.order.stock_id, ticks[0], ticks[-1], method=None,
            direction=self.order.direction), dtype=float).reshape(-1)
        if (market_price.shape != volume.shape or market_volume.shape != volume.shape
                or not np.isfinite(market_price).all() or not np.isfinite(market_volume).all()):
            raise ValueError("Native exchange quotes disagree with the observed HK calendar")
        indicators = self.executor.trade_account.get_trade_indicator().generate_trade_indicators_dataframe()
        self.history_exec = dataframe_append(self.history_exec, self._collect_multi_order_metric(
            self.order, ticks, market_volume, market_price, volume, indicators.iloc[-1]["pa"]))
        self.history_steps = dataframe_append(self.history_steps, [self._collect_single_order_metric(
            self.order, self.cur_time, market_volume, market_price, volume.sum(), volume)])
        self.position -= volume.sum()
        self.cur_time = self._next_time()


class HKProxyStrategy(ProxySAOEStrategy):
    """Native RL proxy using a fresh, uncached HK execution-state adapter."""

    def _create_qlib_backtest_adapter(self, order: Order, trade_decision: object,
                                     trade_range: object) -> HKStateAdapter:
        """Avoid upstream's date-only cache key mixing exchange/cost configurations."""
        ticks = pd.DatetimeIndex(self.trade_exchange.quote_df.index.get_level_values("datetime"))
        ticks = ticks[(ticks >= order.date) & (ticks < order.date + pd.Timedelta(days=1))]
        selected = ticks[(ticks >= order.start_time) & (ticks <= order.end_time)]
        backtest = IntradayBacktestData(order, self.trade_exchange, ticks, selected)
        return HKStateAdapter(order, trade_decision, self.executor, self.trade_exchange, 30, backtest)

    def post_exe_step(self, execute_result: list | None) -> None:
        """Include one-minute ranges; upstream's end-start check drops them."""
        grouped = {key: [] for key in self.adapter_dict}
        for result in execute_result or []:
            grouped[result[0].key_by_day].append(result)
        for key, adapter in self.adapter_dict.items():
            adapter.update(grouped[key], self._last_step_range)


class FirstMinuteStrategy(BaseStrategy):
    """Submit each prescribed child once to the native one-minute executor."""

    def generate_trade_decision(self, execute_result: list | None = None) -> TradeDecisionWO:
        """Execute at the first available minute of the outer decision's interval."""
        current = self.trade_calendar.get_trade_step()
        orders = []
        if current == 0:
            for parent in self.outer_trade_decision.get_decision():
                orders.append(self.trade_exchange.get_order_helper().create(
                    parent.stock_id, parent.amount, parent.direction))
        return TradeDecisionWO(orders, self)


def executor_config() -> dict[str, object]:
    """Compose native day -> 30-minute -> 1-minute execution levels."""
    return {"class": "NestedExecutor", "module_path": "qlib.backtest.executor", "kwargs": {
        "time_per_step": "day", "generate_portfolio_metrics": False,
        "inner_strategy": {"class": HKProxyStrategy},
        "inner_executor": {"class": "NestedExecutor", "module_path": "qlib.backtest.executor", "kwargs": {
            "time_per_step": "30min", "generate_portfolio_metrics": False,
            "inner_strategy": {"class": FirstMinuteStrategy},
            "inner_executor": {"class": "SimulatorExecutor", "module_path": "qlib.backtest.executor",
                               "kwargs": {"time_per_step": "1min", "generate_portfolio_metrics": False}}}}}}


def episode(frame: pd.DataFrame, account: dict, side: str, commands: list[dict[str, object]],
            *, bps: int = 5, amount: int = 200, volume_limit: float | None = None) -> tuple[dict, list, list]:
    """Run the full native simulator and return native cash, positions and states."""
    grid = {str(time) for time in frame.index[::30]}
    if (side not in ("buy", "sell") or amount <= 0 or amount % 100
            or any(c["side"] != side or c["time"] not in grid or c["shares"] < 0
                   or c["shares"] % 100 for c in commands)
            or sum(c["shares"] for c in commands) > amount):
        raise ValueError("Prescribed HK commands must match the side, parent lots and observed grid")
    day = frame.index[0].normalize()
    exchange = HKExchange(frame, bps=bps, volume_limit=volume_limit)
    order = Order(SYMBOL, amount, Order.BUY if side == "buy" else Order.SELL,
                  start_time=day + pd.Timedelta(hours=9, minutes=30),
                  end_time=day + pd.Timedelta(hours=15, minutes=59))
    simulator = SingleAssetOrderExecution(order, executor_config(), {"exchange": exchange}, cash_limit=account.copy())
    states = []
    while not simulator.done():
        state = simulator.get_state()
        requested = sum(c["shares"] for c in commands if c["time"] == str(state.cur_time))
        simulator.step(requested)
        states.append({"time": str(state.cur_time), "step": state.cur_step, "requested": requested,
                       "remaining_after": float(simulator.get_state().position)})
    position = simulator._executor.trade_account.current_position
    final = {"cash": position.get_cash()}
    holding = position.get_stock_amount(SYMBOL)
    if holding:
        final[SYMBOL] = {"amount": holding, "price": float(frame.close.iloc[-1])}
    return final, exchange.journal, states


def run(data: ExecutionData, output: Path, days: list[str], *,
        variant: str = "split", bps: int = 5) -> dict[str, object]:
    """Execute buys and sells through native Qlib accounts, carrying cash across days."""
    output.mkdir(parents=True, exist_ok=False)
    prepare_calendar(data, output / "qlib_data")
    account, journal, states = {"cash": float(CAPITAL)}, [], []
    commands = schedule(days, variant)
    for day in days:
        frame = data.minutes.loc[day]
        for side in ("buy", "sell"):
            selected = [c for c in commands if c["time"].startswith(day) and c["side"] == side]
            account, fills, trace = episode(frame, account, side, selected, bps=bps)
            journal.extend(fill for fill in fills if fill["shares"] > 0)
            states.extend(trace)
        if SYMBOL in account:
            raise ValueError("Native Qlib left overnight inventory")
    return {"platform": "qlib", "engine": "SingleAssetOrderExecution/NestedExecutor/SimulatorExecutor",
            "schedule": variant, "bps": bps, "days": days, "fills": journal, "states": states,
            "ending_cash": str(account["cash"]), "ending_holding": 0, "real_orders_enabled": False}
