"""Gymnasium allocation environment using the existing Decimal artificial ledger.

Each action is chosen after a completed daily bar, filled next open, and closed
that session. This first environment is a sequential daily allocation task,
largely contextual-bandit-like; it does not model overnight inventory or claim
that PPO discovers signals unavailable to simpler methods.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal

import gymnasium as gym
import numpy as np
import pandas as pd
from gymnasium import spaces

from .comparison import signals
from .simulation import SessionSignal, simulate


@dataclass(frozen=True, slots=True)
class ResearchTape:
    """Causal observations paired with unseen next-session execution prices."""

    observations: np.ndarray
    sessions: tuple[SessionSignal, ...]
    feature_dates: tuple[str, ...]

    def __post_init__(self) -> None:
        """Reject malformed arrays and require increasing, genuinely prior signal dates."""
        values = self.observations
        if (values.ndim != 2 or not len(values) or not np.isfinite(values).all()
                or len(values) != len(self.sessions) or len(values) != len(self.feature_dates)):
            raise ValueError("Invalid RL research tape")
        for index, session in enumerate(self.sessions):
            if session.signal_date != self.feature_dates[index]:
                raise ValueError("Feature date does not match completed signal date")
            if index and session.execution_date <= self.sessions[index - 1].execution_date:
                raise ValueError("RL execution sessions must be strictly increasing")
        values.setflags(write=False)


def make_tape(frame: pd.DataFrame, normalized: pd.DataFrame) -> ResearchTape:
    """Align observations with the next observed trading day, including holiday gaps."""
    probability = pd.Series(1.0, index=normalized.index)
    stream = tuple(signals(frame, probability))
    if len(stream) != len(normalized):
        raise ValueError("RL evaluation requires an observed next-session outcome")
    return ResearchTape(normalized.to_numpy(dtype=np.float32, copy=True), stream,
                        tuple(str(day.date()) for day in normalized.index))


class AllocationEnv(gym.Env):
    """Cash/one-lot/maximum-affordable actions; costs are charged by the shared ledger.

    Only training resets may choose random contiguous windows. Evaluation uses
    the entire chronological tape without resetting cash at intermediate dates.
    Market observations are normalized with training-only statistics. Portfolio
    state contains log equity relative to starting cash and closing drawdown.
    """

    metadata = {"render_modes": []}

    def __init__(self, tape: ResearchTape, *, episode_length: int | None = None,
                 slippage_bps: Decimal = Decimal(5), record: bool = False) -> None:
        """Bind a validated partition; neither training nor evaluation can read another tape."""
        super().__init__()
        if episode_length is not None and not 1 <= episode_length <= len(tape.sessions):
            raise ValueError("Invalid training episode length")
        if not slippage_bps.is_finite() or not 0 <= slippage_bps < 10000:
            raise ValueError("Invalid slippage")
        self.tape = tape
        self.episode_length = episode_length
        self.slippage_bps = slippage_bps
        self.record = record
        self.action_space = spaces.Discrete(3)
        self.observation_space = spaces.Box(-np.inf, np.inf, shape=(tape.observations.shape[1] + 2,), dtype=np.float32)
        self.cash = Decimal(100000)
        self.peak = self.cash
        self.index = 0
        self.stop = 0
        self.done = True
        self.fees = Decimal(0)
        self.slippage = Decimal(0)
        self.turnover: dict[str, Decimal] = {}
        self.ledger: list[dict[str, object]] = []
        self.equity: list[dict[str, object]] = []
        self.actions: list[dict[str, object]] = []

    def _observation(self) -> np.ndarray:
        """Expose only the current completed-day features and already realized cash state."""
        if self.done:
            return np.zeros(self.observation_space.shape, dtype=np.float32)
        state = [math.log(float(self.cash / 100000)), float(self.cash / self.peak - 1)]
        return np.concatenate([self.tape.observations[self.index], np.asarray(state, dtype=np.float32)])

    def reset(self, *, seed: int | None = None, options: dict[str, object] | None = None
              ) -> tuple[np.ndarray, dict[str, object]]:
        """Reset capital and optionally select one contiguous training-only episode."""
        super().reset(seed=seed)
        length = self.episode_length or len(self.tape.sessions)
        upper = len(self.tape.sessions) - length + 1
        self.index = int(self.np_random.integers(0, upper)) if self.episode_length else 0
        self.stop = self.index + length
        self.done = False
        self.cash = self.peak = Decimal(100000)
        self.fees = self.slippage = Decimal(0)
        self.turnover = {}
        self.ledger, self.equity, self.actions = [], [], []
        return self._observation(), {"feature_date": self.tape.feature_dates[self.index]}

    def step(self, action: int) -> tuple[np.ndarray, float, bool, bool, dict[str, object]]:
        """Settle the chosen next-session allocation and return its net log-equity reward."""
        if self.done:
            raise RuntimeError("Reset the environment before stepping a completed episode")
        if not self.action_space.contains(action):
            raise ValueError("RL action must be 0=cash, 1=one lot, or 2=maximum affordable")
        action = int(action)
        session = self.tape.sessions[self.index]
        before = self.cash
        result = simulate([session], capital=self.cash, slippage_bps=self.slippage_bps,
                          max_lots=None if action == 2 else action)
        fills = result["ledger"]
        if fills:
            self.cash = Decimal(fills[-1]["cash_after"])
            self.fees += sum((Decimal(fill["fees"]["total"]) for fill in fills), Decimal(0))
            month = session.execution_date[:7]
            traded = sum((Decimal(fill["notional"]) for fill in fills), Decimal(0))
            self.turnover[month] = self.turnover.get(month, Decimal(0)) + traded
            if self.turnover[month] > 15_000_000:
                raise ValueError("RL monthly turnover exceeds implemented Tier I")
            self.slippage += fills[0]["shares"] * (session.open + session.close) * self.slippage_bps / 10000
        self.peak = max(self.peak, self.cash)
        if self.record:
            self.ledger.extend(fills)
            self.equity.append({"date": session.execution_date, "equity_hkd": float(self.cash)})
            self.actions.append({"signal_date": session.signal_date, "execution_date": session.execution_date,
                                 "action": action, "shares": fills[0]["shares"] if fills else 0})
        reward = 100 * math.log(float(self.cash / before))
        self.index += 1
        self.done = self.index >= self.stop
        return self._observation(), reward, self.done, False, {"equity_hkd": float(self.cash), "action": action}

    def report(self) -> dict[str, object]:
        """Return auditable final cash, fills and daily equity for a recorded full episode."""
        if not self.record or not self.done:
            raise ValueError("Finish a recorded episode before requesting its report")
        peak, drawdown = 100000.0, 0.0
        for point in self.equity:
            peak = max(peak, point["equity_hkd"])
            drawdown = min(drawdown, point["equity_hkd"] / peak - 1)
        return {"ending_equity_hkd": float(self.cash), "net_return": float(self.cash / 100000 - 1),
                "max_drawdown": drawdown, "fees_hkd": float(self.fees), "slippage_hkd": float(self.slippage),
                "round_trips": len(self.ledger) // 2, "orders": len(self.ledger), "days": len(self.equity),
                "max_monthly_turnover_hkd": float(max(self.turnover.values(), default=Decimal(0))),
                "ledger": self.ledger, "equity": self.equity, "actions": self.actions,
                "slippage_bps_per_side": float(self.slippage_bps), "real_orders_enabled": False}
