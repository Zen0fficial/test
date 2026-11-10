"""Local artificial portfolio accounting; no brokerage or order-routing code."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from .costs import tiered_fee


@dataclass(frozen=True, slots=True)
class SessionSignal:
    """Prior-completed-day signal with next observed session's execution prices."""

    signal_date: str
    execution_date: str
    probability: float
    open: Decimal
    close: Decimal

    def __post_init__(self) -> None:
        """Validate chronological signals and finite positive reference prices."""
        if (any(date.fromisoformat(d).isoformat() != d for d in (self.signal_date, self.execution_date))
                or date.fromisoformat(self.signal_date) >= date.fromisoformat(self.execution_date)
                or not 0 <= self.probability <= 1):
            raise ValueError("Signal must precede execution and have a valid probability")
        if any(not isinstance(p, Decimal) or not p.is_finite() or p <= 0 for p in (self.open, self.close)):
            raise ValueError("Invalid execution reference price")


def simulate(
    signals: list[SessionSignal], *, threshold: float = .5, capital: Decimal = Decimal(100000),
    lot: int = 100, slippage_bps: Decimal = Decimal(5), charge_fees: bool = True,
    max_lots: int | None = None,
) -> dict[str, object]:
    """Simulate fully funded long-or-cash, entering next open and exiting its close.

    Flat overnight avoids inventing dividend/split cash flows from raw OHLCV.
    Entry and exit are predetermined from the prior-day signal. They are assumed
    fills, not proof of achievable auction prices. No current-day closing price
    is used to decide entry or size. Prices include adverse slippage on both legs.
    Cash never goes negative; reuse of unsettled proceeds is assumed permitted.
    This is not a settled-cash account simulation or a historical tariff audit.
    """
    if (not isinstance(capital, Decimal) or not capital.is_finite() or capital <= 0
            or type(lot) is not int or lot <= 0 or not isinstance(slippage_bps, Decimal)
            or not slippage_bps.is_finite() or not 0 <= slippage_bps < 10000
            or not 0 <= threshold <= 1
            or (max_lots is not None and (type(max_lots) is not int or max_lots < 0))):
        raise ValueError("Invalid portfolio configuration")
    cash = capital
    fees = Decimal(0)
    slippage_cost = Decimal(0)
    turnover: dict[str, Decimal] = {}
    ledger: list[dict[str, object]] = []
    equity: list[dict[str, object]] = []
    previous_date = ""
    peak = capital
    drawdown = Decimal(0)
    daily_returns: list[float] = []
    spread = slippage_bps / 10000
    for signal in signals:
        if signal.execution_date <= previous_date:
            raise ValueError("Duplicate or nonmonotonic execution date")
        previous_date = signal.execution_date
        before = cash
        if signal.probability >= threshold:
            buy = signal.open * (1 + spread)
            sell = signal.close * (1 - spread)
            shares = int(cash / (buy * lot)) * lot
            if max_lots is not None:
                shares = min(shares, max_lots * lot)
            while shares > 0:
                buy_fee = tiered_fee(buy * shares).total if charge_fees else Decimal(0)
                if buy * shares + buy_fee <= cash:
                    break
                shares -= lot
            if shares:
                month = signal.execution_date[:7]
                turnover[month] = turnover.get(month, Decimal(0)) + (buy + sell) * shares
                if turnover[month] > 15_000_000:
                    raise ValueError("Monthly turnover exceeds implemented Tier-I schedule")
                for side, price in (("buy", buy), ("sell", sell)):
                    breakdown = tiered_fee(price * shares)
                    fee = breakdown.total if charge_fees else Decimal(0)
                    cash += (-price * shares if side == "buy" else price * shares) - fee
                    fees += fee
                    if cash < 0:
                        raise ValueError("Simulation violated the no-borrowing constraint")
                    ledger.append({
                        "signal_date": signal.signal_date, "date": signal.execution_date, "side": side,
                        "shares": shares, "price": str(price), "notional": str(price * shares),
                        "fees": breakdown.json() if charge_fees else {"total": "0"}, "cash_after": str(cash),
                    })
                slippage_cost += shares * (signal.open + signal.close) * spread
        daily_returns.append(float(cash / before - 1))
        peak = max(peak, cash)
        drawdown = min(drawdown, cash / peak - 1)
        equity.append({"date": signal.execution_date, "equity_hkd": float(cash), "shares_at_close": 0})
    mean = sum(daily_returns) / len(daily_returns) if daily_returns else 0
    variance = sum((r - mean) ** 2 for r in daily_returns) / max(1, len(daily_returns) - 1)
    return {
        "ending_equity_hkd": float(cash), "net_return": float(cash / capital - 1),
        "max_drawdown": float(drawdown), "fees_hkd": float(fees), "slippage_hkd": float(slippage_cost),
        "round_trips": len(ledger) // 2, "orders": len(ledger), "days": len(signals),
        "annualized_sharpe_zero_cash_rate": mean / math.sqrt(variance) * math.sqrt(252) if variance else None,
        "threshold": threshold, "capital_hkd": float(capital), "slippage_bps_per_side": float(slippage_bps),
        "max_monthly_turnover_hkd": float(max(turnover.values(), default=Decimal(0))),
        "ledger": ledger, "equity": equity, "real_orders_enabled": False,
    }
