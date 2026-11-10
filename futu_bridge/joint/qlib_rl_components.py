"""Native QlibRL components with local-data and transaction-cost adapters.

Trainer, TrainingVessel, PPO/DQN, recurrent network, interpreters and simulator
transition logic are upstream implementations. FrameSimulator overrides only
data loading. This uses Qlib's simple execution simulator, not its CN-specific
nested exchange simulator, and makes no exchange queue or partial-fill claim.
"""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
import random

import pandas as pd
import numpy as np
import torch
from qlib.backtest.decision import Order
from qlib.rl.data.base import BaseIntradayProcessedData, ProcessedDataProvider
from qlib.rl.data.native import DataframeIntradayBacktestData
from qlib.rl.order_execution.interpreter import CategoricalActionInterpreter, FullHistoryStateInterpreter
from qlib.rl.order_execution.network import Recurrent
from qlib.rl.order_execution.policy import DQN, PPO
from qlib.rl.order_execution.reward import PAPenaltyReward
from qlib.rl.order_execution.simulator_simple import SingleAssetOrderExecutionSimple
from qlib.rl.order_execution.state import SAOEState
from qlib.rl.utils import EnvWrapper
from qlib.rl.trainer import TrainingVessel

from .costs import tiered_fee
from .qlib_rl_data import ExecutionData


class FrameSimulator(SingleAssetOrderExecutionSimple):
    """Replace filesystem loading with a captured dataframe; preserve native steps."""

    def __init__(self, order: Order, frame: pd.DataFrame) -> None:
        """Use one block per native step, without inventing unobserved minute fills."""
        self.frame = frame
        super().__init__(order, Path("."), data_granularity=30, ticks_per_step=30, vol_threshold=None)

    def get_backtest_data(self) -> DataframeIntradayBacktestData:
        """Expose real block-open execution references and observed block volume."""
        return DataframeIntradayBacktestData(self.frame, price_column="open", volume_column="volume")


@dataclass
class ProcessedFrames(BaseIntradayProcessedData):
    """Native processed-data contract; Qlib masks current/future blocks itself."""

    today: pd.DataFrame
    yesterday: pd.DataFrame


class FeatureProvider(ProcessedDataProvider):
    """Read only captured features transformed with training-partition statistics."""

    def __init__(self, frames: dict[str, pd.DataFrame]) -> None:
        """Bind chronological frames without changing any local market-data provider."""
        self.frames = frames
        self.dates = sorted(frames)

    def get_data(self, stock_id: str, date: pd.Timestamp, feature_dim: int,
                 time_index: pd.Index) -> ProcessedFrames:
        """Return this and the previous observed session with validated dimensions."""
        day = str(date.date())
        index = self.dates.index(day)
        if stock_id != "HK_00700" or not index or feature_dim != 5:
            raise ValueError("Invalid native RL feature request")
        today = self.frames[day]
        if not today.index.equals(time_index):
            raise ValueError("Feature and simulator grids differ")
        return ProcessedFrames(today, self.frames[self.dates[index - 1]])


class CostReward(PAPenaltyReward):
    """Native price-advantage reward less actual child-order fees and slippage.

The liquidation at session close is reported in the separate artificial cash
ledger. Its price/cost is not known at decision time and is not an observation.
This task optimizes a prescribed buy order, not the decision whether to buy.
"""

    def __init__(self) -> None:
        """Disable concentration penalty so whole-lot schedules remain comparable."""
        super().__init__(penalty=0)

    def reward(self, simulator_state: SAOEState) -> float:
        """Keep upstream price-advantage arithmetic, adding explicit cost debits."""
        value = super().reward(simulator_state)
        last = simulator_state.history_steps.iloc[-1]
        shares = Decimal(str(last.deal_amount))
        if shares:
            raw = Decimal(str(last.trade_price)) * shares
            debit = tiered_fee(raw * Decimal("1.0005")).total + raw * Decimal(".0005")
            reference = simulator_state.backtest_data.get_deal_price().mean() * simulator_state.order.amount
            value -= float(debit) / reference * 10000
        return float(value)


@dataclass
class SimulatorFactory:
    """Picklable factory for the native trainer's finite environments."""

    blocks: dict[str, pd.DataFrame]

    def __call__(self, order: Order) -> FrameSimulator:
        """Construct an unmodified native transition engine for one captured day."""
        return FrameSimulator(order, self.blocks[str(order.date.date())])


def orders(data: ExecutionData, days: list[str]) -> list[Order]:
    """Define fully funded 200-share buy tasks on observed session grids."""
    return [Order(stock_id="HK_00700", amount=200, direction=Order.BUY,
                  start_time=data.blocks[day].index[0], end_time=pd.Timestamp(day) + pd.Timedelta(hours=16))
            for day in days]


def interpreters(frames: dict[str, pd.DataFrame]) -> tuple[FullHistoryStateInterpreter, CategoricalActionInterpreter]:
    """Use native history masking and 0/100/200-share categorical actions."""
    return (FullHistoryStateInterpreter(11, 11, 5, FeatureProvider(frames)),
            CategoricalActionInterpreter([0, .5, 1], max_step=11))


def make_policy(kind: str, frames: dict[str, pd.DataFrame], weights: Path | None = None) -> PPO | DQN:
    """Build upstream recurrent PPO or DQN; no algorithm rewrite or SB3 dependency."""
    state, action = interpreters(frames)
    network = Recurrent(state.observation_space, hidden_dim=32, output_dim=32)
    common = {"network": network, "obs_space": state.observation_space,
              "action_space": action.action_space, "lr": .0003, "weight_file": weights}
    if kind == "ppo":
        return PPO(**common, reward_normalization=False, value_clip=False, max_grad_norm=1)
    if kind == "dqn":
        policy = DQN(**common, target_update_freq=20)
        policy.set_eps(.1)
        return policy
    raise ValueError("Native Qlib policy must be ppo or dqn")


def make_environment(data: ExecutionData, frames: dict[str, pd.DataFrame], day: str) -> EnvWrapper:
    """Build native Qlib's environment wrapper over one immutable real-data task."""
    state, action = interpreters(frames)
    return EnvWrapper(SimulatorFactory(data.blocks), state, action, orders(data, [day]), CostReward())


class SynchronousVessel(TrainingVessel):
    """Native collect/update with synchronous seeds for a single dummy worker.

Upstream DataQueue.__del__ attempts to poll closed multiprocessing handles on
Windows. Only the seed iterator is replaced: shuffled complete training epochs
repeat forever; validation/test visit each seed exactly once. Native collection,
losses, policy updates, environment wrappers and Trainer loops remain intact.
"""

    def __init__(self, *, seed: int, **kwargs: object) -> None:
        """Bind an explicit sampler seed and audit native collection counts."""
        super().__init__(**kwargs)
        self.rng = np.random.default_rng(seed)
        self.collections: list[dict[str, int]] = []

    def train_seed_iterator(self) -> Iterator[Order]:
        """Yield shuffled complete epochs without asynchronous prefetch or handles."""
        if not self.train_initial_states:
            raise ValueError("Native training requires initial states")
        while True:
            for index in self.rng.permutation(len(self.train_initial_states)):
                yield self.train_initial_states[index]

    def val_seed_iterator(self) -> Iterator[Order]:
        """Visit validation orders once without training-time random resampling."""
        return iter(self.val_initial_states or [])

    def test_seed_iterator(self) -> Iterator[Order]:
        """Visit test orders once; no updates are performed by native test()."""
        return iter(self.test_initial_states or [])

    def train(self, vector_env: object) -> dict[str, object]:
        """Delegate the full learning operation and retain its real sample counts."""
        result = super().train(vector_env)
        self.collections.append({"episodes": int(result["n/ep"]), "steps": int(result["n/st"])})
        return result

    def state_dict(self) -> dict[str, object]:
        """Preserve native weights plus optimizer, sampler and RNG continuation state."""
        return {**super().state_dict(), "optimizer": self.policy.optim.state_dict(),
                "sampler_rng": self.rng.bit_generator.state, "torch_rng": torch.get_rng_state(),
                "numpy_rng": np.random.get_state(), "python_rng": random.getstate(),
                "collections": self.collections.copy(), "update_counter": getattr(self.policy, "_iter", None)}

    def load_state_dict(self, state_dict: dict[str, object]) -> None:
        """Restore exact continuation for this CPU, single-worker PPO/DQN configuration."""
        super().load_state_dict(state_dict)
        self.policy.optim.load_state_dict(state_dict["optimizer"])
        self.rng.bit_generator.state = state_dict["sampler_rng"]
        torch.set_rng_state(state_dict["torch_rng"])
        np.random.set_state(state_dict["numpy_rng"])
        random.setstate(state_dict["python_rng"])
        self.collections = state_dict["collections"].copy()
        if state_dict["update_counter"] is not None:
            self.policy._iter = state_dict["update_counter"]
