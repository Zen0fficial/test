"""Costed, fully funded artificial accounts for native Qlib execution schedules."""

from __future__ import annotations

from decimal import Decimal

import numpy as np
import pandas as pd
import torch
from tianshou.data import Batch

from .costs import tiered_fee
from .qlib_rl_components import make_environment
from .qlib_rl_data import ExecutionData


def policy_action(policy: object, observation: dict[str, object]) -> int:
    """Use the native policy's deterministic evaluation behavior without updates."""
    policy.eval()
    with torch.no_grad():
        result = policy(Batch([{"obs": observation, "info": {}}]))
    return int(np.asarray(result.act)[0])


def trajectory(data: ExecutionData, frames: dict[str, pd.DataFrame], day: str,
               policy: object | None, control: str | None = None) -> dict[str, object]:
    """Collect an actual native simulator trajectory, including forced completion."""
    environment = make_environment(data, frames, day)
    observation = environment.reset()
    records, rewards, done = [], [], False
    while not done:
        before = environment.simulator.get_state()
        if control == "immediate":
            action = 2
        elif control == "two_lot_schedule":
            action = 1 if before.cur_step in (0, 5) else 0
        elif policy is not None:
            action = policy_action(policy, observation)
        else:
            raise ValueError("A native policy or explicit execution control is required")
        observation, reward, done, _ = environment.step(action)
        records.append({"time": str(before.cur_time), "action": action})
        rewards.append(float(reward))
    state = environment.simulator.get_state()
    fills = state.history_exec[state.history_exec.deal_amount > 0]
    result = {"actions": records, "reward": sum(rewards), "native_pa_bps": float(state.metrics["pa"]),
              "filled": float(fills.deal_amount.sum()),
              "fills": [{"time": str(t), "shares": float(row.deal_amount), "reference": float(row.trade_price)}
                        for t, row in fills.iterrows()]}
    if result["filled"] != 200 or state.position != 0:
        raise ValueError("Native execution did not complete its prescribed parent order")
    environment.close()
    return result


def evaluate(data: ExecutionData, frames: dict[str, pd.DataFrame], days: list[str],
             policy: object | None = None, *, control: str | None = None,
             slippage_bps: int = 5) -> dict[str, object]:
    """Carry HKD 100,000 across daily native buy schedules and artificial closing sells.

Each nonzero native step is one child order with its own commission minimum.
The 16:00 closing sell is prescribed, not an RL action. These are assumed fills,
not a live brokerage connection or an exchange order-book simulation.
"""
    if slippage_bps not in (0, 5, 10):
        raise ValueError("Use an audited 0/5/10-bps cost scenario")
    cash, peak = Decimal(100000), Decimal(100000)
    fees, worst_drawdown = Decimal(0), Decimal(0)
    spread = Decimal(slippage_bps) / 10000
    ledger: list[dict[str, object]] = []
    sessions: list[dict[str, object]] = []
    turnover: dict[str, Decimal] = {}
    for day in days:
        native = trajectory(data, frames, day, policy, control)
        holding, entry_debit = 0, Decimal(0)
        entries = [*native["fills"], {"time": day + " 16:00:00", "shares": 200, "exit": True}]
        for fill in entries:
            sell = bool(fill.get("exit"))
            shares = int(fill["shares"])
            if shares != fill["shares"] or shares <= 0 or shares % 100:
                raise ValueError("Native child fill violated the 100-share lot contract")
            reference = (data.blocks[day].close.iloc[-1] if sell
                         else data.blocks[day].loc[pd.Timestamp(fill["time"]), "open"])
            if not sell and abs(float(reference) - fill["reference"]) > 1e-8:
                raise ValueError("Native fill price does not match the original captured bar")
            price = Decimal(str(reference)) * (1 - spread if sell else 1 + spread)
            notional = price * shares
            breakdown = tiered_fee(notional)
            fee = breakdown.total
            cash += (notional if sell else -notional) - fee
            holding += -shares if sell else shares
            if cash < 0 or holding < 0 or holding > 200:
                raise ValueError("Artificial account violated cash/inventory constraints")
            month = day[:7]
            turnover[month] = turnover.get(month, Decimal(0)) + notional
            if turnover[month] > 15_000_000:
                raise ValueError("Turnover exceeds the implemented IBKR tier")
            fees += fee
            if not sell:
                entry_debit += notional + fee
            ledger.append({"time": fill["time"], "side": "sell" if sell else "buy", "shares": shares,
                           "reference": str(reference), "price": str(price), "notional": str(notional),
                           "fees": breakdown.json(), "cash_after": str(cash), "holding_after": holding,
                           "engine": "prescribed closing sell" if sell else "native Qlib simple simulator"})
        benchmark = Decimal(str(data.blocks[day].open.mean())) * 200
        peak = max(peak, cash)
        worst_drawdown = min(worst_drawdown, cash / peak - 1)
        sessions.append({"date": day, "ending_cash_hkd": float(cash), "native": native,
                         "net_execution_advantage_bps": float((benchmark - entry_debit) / benchmark * 10000)})
    return {"ending_equity_hkd": float(cash), "net_return": float(cash / 100000 - 1),
            "max_drawdown": float(worst_drawdown), "fees_hkd": float(fees), "round_trips": len(days),
            "fills": len(ledger), "slippage_bps": slippage_bps, "sessions": sessions, "ledger": ledger,
            "mean_net_execution_advantage_bps": float(np.mean(
                [s["net_execution_advantage_bps"] for s in sessions])), "real_orders_enabled": False}
