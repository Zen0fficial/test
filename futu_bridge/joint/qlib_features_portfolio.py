"""Native Qlib portfolio strategies, reports and independently checked HK accounts."""

from __future__ import annotations

from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP
from pathlib import Path

import numpy as np
import pandas as pd
from qlib.backtest.decision import Order
from qlib.backtest.exchange import Exchange
from qlib.contrib.strategy.signal_strategy import WeightStrategyBase

from .paper_common import SYMBOL
from .paper_qlib import HKExchange
from .session import write_report


class HKDailyExchange(HKExchange):
    """Reuse audited HK native fill/fee hooks at daily frequency and retain native performance reports."""

    def __init__(self, frame: pd.DataFrame) -> None:
        """Use only a supported fixed recent daily window and original exact source prices."""
        if not ((frame.open > 200) & (frame.open < 499)).all():
            raise ValueError("Native daily portfolio conformance is scoped to the verified HK price band")
        self.frame, self.bps, self.volume_limit = frame, 5, None
        self.journal: list[dict[str, object]] = []
        Exchange.__init__(self, freq="day", start_time=frame.index[0], end_time=frame.index[-1],
                          codes=[SYMBOL], deal_price=("$buy", "$sell"), trade_unit=100,
                          open_cost=0, close_cost=0, min_cost=0)

    def deal_order(self, order: Order, trade_account: object = None, position: object = None,
                   dealt_order_amount: dict | None = None) -> tuple[float, float, float]:
        """Exclude strategy trial-position calculations from the committed execution journal."""
        if trade_account is None:
            return Exchange.deal_order(self, order, trade_account, position,
                                       {} if dealt_order_amount is None else dealt_order_amount)
        return super().deal_order(order, trade_account, position, dealt_order_amount)


class PositiveReturnWeights(WeightStrategyBase):
    """Native weighting/order generation with a simple prior-close long/cash policy."""

    def generate_target_weight_position(self, score: pd.Series, current: object,
                                        trade_start_time: pd.Timestamp,
                                        trade_end_time: pd.Timestamp) -> dict[str, float]:
        """Use the native previous-session signal; never consult the execution day's close."""
        if isinstance(score, pd.DataFrame):
            score = score.iloc[:, 0]
        return {SYMBOL: 1.0} if float(score.get(SYMBOL, -1)) > 0 else {}


def verify_account(journal: list[dict[str, object]], report: pd.DataFrame,
                   frame: pd.DataFrame) -> dict[str, object]:
    """Reconstruct each fill's fee and every daily marked account independently with Decimal."""
    cash, holding, total_fee = Decimal(100000), 0, Decimal(0)
    rows = {}
    for event in journal:
        rows.setdefault(pd.Timestamp(event["time"]).normalize(), []).append(event)
    count = 0
    for time, native in report.iterrows():
        for event in rows.get(time.normalize(), []):
            shares = int(event["shares"])
            if not shares:
                continue
            if shares % 100:
                raise ValueError("Native portfolio executed a non-lot quantity")
            source = Decimal(str(frame.loc[time.normalize(), "open"]))
            buy = event["side"] == "buy"
            scale = Decimal("1.0005") if buy else Decimal(".9995")
            rounding = ROUND_CEILING if buy else "ROUND_FLOOR"
            price = (source * scale / Decimal(".2")).to_integral_value(rounding=rounding) * Decimal(".2")
            notional = price * shares
            fee = (max(Decimal(18), notional * Decimal(".0005")) + notional * Decimal(".000127")
                   + (notional * Decimal(".001")).to_integral_value(rounding=ROUND_CEILING)).quantize(
                       Decimal(".01"), rounding=ROUND_HALF_UP)
            if abs(Decimal(event["price"]) - price) > Decimal(".000001"):
                raise ValueError("Native portfolio fill uses a noncausal/wrong source quote")
            if abs(Decimal(event["fee"]) - fee) > Decimal(".000001"):
                raise ValueError("Native portfolio fee differs from independent schedule")
            cash -= (notional if buy else -notional) + fee
            holding += shares if buy else -shares
            total_fee += fee
            if cash < 0 or holding < 0:
                raise ValueError("Native portfolio exceeded long-only cash/inventory")
            count += 1
        equity = cash + holding * Decimal(str(frame.loc[time.normalize(), "close"]))
        for field, expected in (("cash", cash), ("account", equity), ("total_cost", total_fee)):
            if abs(Decimal(str(native[field])) - expected) > Decimal(".000001"):
                raise ValueError(f"Native daily portfolio {field} differs from independent account")
    return {"passed": True, "committed_fills": count, "cash": str(cash), "holding": holding,
            "cumulative_fees": str(total_fee), "daily_accounts_checked": len(report)}


def verify_risk(returns: pd.Series, native: pd.DataFrame, mode: str) -> dict[str, float]:
    """Verify Qlib native arithmetic/geometric risk summaries with separately computed formulas."""
    if mode == "sum":
        mean, std = returns.mean(), returns.std(ddof=1)
        annual, drawdown = mean * 252, (returns.cumsum() - returns.cumsum().cummax()).min()
    else:
        wealth = (1 + returns).cumprod()
        mean, std = wealth.iloc[-1] ** (1 / len(returns)) - 1, np.log1p(returns).std(ddof=1)
        annual, drawdown = wealth.iloc[-1] ** (252 / len(returns)) - 1, (wealth / wealth.cummax() - 1).min()
    expected = {"mean": mean, "std": std, "annualized_return": annual,
                "information_ratio": mean / std * np.sqrt(252), "max_drawdown": drawdown}
    if not np.allclose(native["risk"].reindex(expected).to_numpy(), list(expected.values()), atol=1e-10, rtol=1e-10):
        raise ValueError("Native portfolio risk summary differs from independent formulas")
    return {name: float(value) for name, value in expected.items()}


def portfolios(frame: pd.DataFrame, output: Path, split: dict[str, tuple[str, str]]) -> dict[str, object]:
    """Run native TopkDropout and native weighting strategies through full daily accounting."""
    from qlib.backtest import backtest
    from qlib.contrib.evaluate import risk_analysis
    from qlib.contrib.strategy.signal_strategy import TopkDropoutStrategy

    recent = frame.iloc[-41:].copy()
    scores = recent.close.pct_change().fillna(0)
    scores.index = pd.MultiIndex.from_arrays([scores.index, [SYMBOL] * len(scores)],
                                            names=["datetime", "instrument"])
    results = {}
    for name in ("topk_dropout", "positive_return_weights"):
        exchange = HKDailyExchange(recent)
        strategy = (TopkDropoutStrategy(signal=scores, topk=1, n_drop=1, risk_degree=.9)
                    if name == "topk_dropout" else PositiveReturnWeights(signal=scores, risk_degree=.9))
        portfolio, indicators = backtest(
            start_time=recent.index[1], end_time=recent.index[-1], strategy=strategy,
            executor={"class": "SimulatorExecutor", "module_path": "qlib.backtest.executor",
                      "kwargs": {"time_per_step": "day", "generate_portfolio_metrics": True}},
            account=100000, benchmark=SYMBOL, exchange_kwargs={"exchange": exchange})
        daily, positions = portfolio["1day"]
        checked = verify_account(exchange.journal, daily, recent)
        daily.to_csv(output / f"{name}_native_daily.csv")
        write_report(output / f"{name}_fills.json", exchange.journal)
        summaries = {}
        for mode in ("sum", "product"):
            summaries[mode] = verify_risk(daily["return"] - daily["cost"], risk_analysis(
                daily["return"] - daily["cost"], N=252, freq=None, mode=mode), mode)
        render_report(daily, output / f"{name}_report.png")
        if not checked["committed_fills"] or len(positions) != len(daily):
            raise ValueError("Native portfolio execution/report coverage is empty")
        results[name] = {**checked, "risk": summaries, "converter_figures": 1,
                         "signals": "previous-session close return", "native_positions": len(positions)}
    return {"passed": True, "strategies": results,
            "scope": "Single-stock long/cash native order generation; not multi-asset optimizer validation",
            "native_interactive_graphs": "Plotly absent from fixed environment; native numeric reports retained",
            "annualization_assumption": 252, "real_orders_enabled": False}


def render_report(daily: pd.DataFrame, destination: Path) -> None:
    """Plot native account data using installed matplotlib; never mislabel it as the unavailable Plotly catalogue."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    figure, axes = plt.subplots(2, 1, figsize=(9, 5), sharex=True, constrained_layout=True)
    axes[0].plot(daily.index, daily.account, label="Native Qlib account (HKD)")
    axes[0].plot(daily.index, daily.cash, label="Cash", alpha=.7)
    axes[0].legend()
    axes[0].set_title("Captured HK.00700 — simulated long-only portfolio")
    axes[1].plot(daily.index, (daily.account / daily.account.cummax() - 1) * 100)
    axes[1].set_ylabel("Account drawdown (%)")
    for axis in axes:
        axis.grid(alpha=.2)
    figure.savefig(destination, dpi=150)
    plt.close(figure)
