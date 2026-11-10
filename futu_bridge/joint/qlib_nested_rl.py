"""Native PPO/DQN learning through Qlib's full nested HK execution simulator.

This is an execution integration experiment, not a profitability claim. The
prescribed parent is a funded 200-share buy. Native interpreters mask future
minute features; native accounts pay fees. No live execution client is used.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
import argparse
import hashlib
import random
import shutil

import numpy as np
import pandas as pd
import torch
from mlflow.tracking import MlflowClient
from qlib.backtest.decision import Order
from qlib.rl.order_execution.interpreter import CategoricalActionInterpreter, FullHistoryStateInterpreter
from qlib.rl.order_execution.network import Recurrent
from qlib.rl.order_execution.policy import DQN, PPO
from qlib.rl.order_execution.reward import PAPenaltyReward
from qlib.rl.order_execution.simulator_qlib import SingleAssetOrderExecution
from qlib.rl.order_execution.state import SAOEState
from qlib.rl.trainer import Trainer
from qlib.rl.utils import EnvWrapper
from tianshou.data import Batch

from .costs import tiered_fee
from .paper_common import SYMBOL, capture
from .paper_qlib import HKExchange, executor_config, prepare_calendar
from .paper_verify import verify
from .qlib_rl_components import FeatureProvider, SynchronousVessel
from .qlib_rl_data import ExecutionData, normalized_features
from .session import write_report
from .tracking import tracking_uri


@dataclass
class NestedFactory:
    """Supply fixed local minutes to the native full simulator and exchange."""

    minutes: pd.DataFrame

    def __call__(self, order: Order) -> SingleAssetOrderExecution:
        """Create the native day/30-minute/1-minute executor hierarchy."""
        exchange = HKExchange(self.minutes.loc[str(order.date.date())])
        return SingleAssetOrderExecution(order, executor_config(), {"exchange": exchange}, cash_limit=100000)


class NestedCostReward(PAPenaltyReward):
    """Native price-advantage reward with the actual single-child commission debit."""

    def __init__(self) -> None:
        """Use whole lots without an extra concentration penalty."""
        super().__init__(penalty=0)

    def reward(self, simulator_state: SAOEState) -> float:
        """Quotes already contain tick-rounded slippage; subtract fees once."""
        reward = super().reward(simulator_state)
        last = simulator_state.history_steps.iloc[-1]
        if last.deal_amount:
            notional = Decimal(str(last.trade_price)) * Decimal(str(last.deal_amount))
            reference = simulator_state.backtest_data.get_deal_price().mean() * simulator_state.order.amount
            reward -= float(tiered_fee(notional).total) / reference * 10000
        return float(reward)


def parent_orders(data: ExecutionData, days: list[str]) -> list[Order]:
    """Create funded tasks ending at the final observed HK minute."""
    return [Order(SYMBOL, 200, Order.BUY, start_time=data.minutes.loc[day].index[0],
                  end_time=data.minutes.loc[day].index[-1]) for day in days]


def interpreters(frames: dict[str, pd.DataFrame]) -> tuple[FullHistoryStateInterpreter, CategoricalActionInterpreter]:
    """Native causal masking covers all 330 real minutes and eleven decisions."""
    return (FullHistoryStateInterpreter(11, 330, 5, FeatureProvider(frames)),
            CategoricalActionInterpreter([0, .5, 1], max_step=11))


def policy(kind: str, frames: dict[str, pd.DataFrame], weights: Path | None = None) -> PPO | DQN:
    """Construct an upstream recurrent algorithm without modifying its learning code."""
    state, action = interpreters(frames)
    network = Recurrent(state.observation_space, hidden_dim=32, output_dim=32)
    common = {"network": network, "obs_space": state.observation_space,
              "action_space": action.action_space, "lr": .0003, "weight_file": weights}
    if kind == "ppo":
        return PPO(**common, reward_normalization=False, value_clip=False, max_grad_norm=1)
    if kind == "dqn":
        result = DQN(**common, target_update_freq=20)
        result.set_eps(.1)
        return result
    raise ValueError("Expected native PPO or DQN")


def evaluate(data: ExecutionData, frames: dict[str, pd.DataFrame], model: object,
             restored: object, days: list[str]) -> list[dict[str, object]]:
    """Audit held-out native fills, full state chronology and exact checkpoint reload."""
    model.eval()
    restored.eval()
    results = []
    for day in days:
        state, action = interpreters(frames)
        env = EnvWrapper(NestedFactory(data.minutes), state, action, parent_orders(data, [day]), NestedCostReward())
        observation, done, commands = env.reset(), False, []
        try:
            while not done:
                batch = Batch([{"obs": observation, "info": {}}])
                with torch.no_grad():
                    original, reloaded = model(batch), restored(batch)
                if not torch.equal(original.logits, reloaded.logits) or not np.array_equal(original.act, reloaded.act):
                    raise ValueError("Nested policy changed after checkpoint reload")
                simulator = env.simulator
                current = simulator.get_state()
                choice = int(np.asarray(original.act)[0])
                commands.append({"time": str(current.cur_time), "side": "buy",
                                 "shares": action.interpret(current, choice)})
                observation, _, done, _ = env.step(choice)
            simulator = env.simulator
            position = simulator._executor.trade_account.current_position
            exchange = simulator._executor.trade_exchange
            fills = [f for f in exchange.journal if f["shares"] > 0]
            result = {"platform": "qlib", "schedule": "policy", "days": [day], "bps": 5,
                      "fills": fills, "ending_cash": str(position.get_cash()),
                      "ending_holding": position.get_stock_amount(SYMBOL), "real_orders_enabled": False}
            result["independent_verification"] = verify(data, result, expected_commands=commands)
            if simulator.get_state().position != 0 or result["ending_holding"] != 200 or len(commands) != 11:
                raise ValueError("Nested policy failed to complete the funded parent across eleven decisions")
            result["commands"] = commands
            results.append(result)
        finally:
            env.close()
    return results


def run(response: Path, output: Path, *, iterations: int = 8, episodes: int = 16,
        data: ExecutionData | None = None) -> dict[str, object]:
    """Train native PPO and DQN, track checkpoints and verify held-out native accounts."""
    if output.exists() or iterations < 1 or episodes < 2:
        raise ValueError("Use a new output and positive iterations with at least two episodes")
    data = capture(response) if data is None else data
    if data.provenance.get("response_sha256") != hashlib.sha256(response.read_bytes()).hexdigest():
        raise ValueError("Execution data does not match the archived provenance capture")
    output.mkdir(parents=True)
    shutil.copyfile(response, output / "provider_response.json")
    prepare_calendar(data, output / "qlib_data")
    minute_blocks = {day: data.minutes.loc[day] for day in data.blocks}
    frames, normalizer = normalized_features(ExecutionData(data.minutes, minute_blocks,
                                                          data.partitions, data.provenance))
    write_report(output / "normalizer.json", normalizer)
    write_report(output / "partitions.json", data.partitions)
    torch.set_num_threads(1)
    client = MlflowClient(tracking_uri=tracking_uri(output))
    experiment = client.create_experiment("native-qlib-nested-hk",
                                         artifact_location=(output.resolve() / "mlartifacts").as_uri())
    reports = {}
    for kind in ("ppo", "dqn"):
        random.seed(11)
        np.random.seed(11)
        torch.manual_seed(11)
        model = policy(kind, frames)
        before = [p.detach().clone() for p in model.parameters()]
        state, action = interpreters(frames)
        vessel = SynchronousVessel(
            seed=11, simulator_fn=NestedFactory(data.minutes), state_interpreter=state,
            action_interpreter=action, policy=model, reward=NestedCostReward(),
            train_initial_states=parent_orders(data, data.partitions["train"]),
            val_initial_states=parent_orders(data, data.partitions["validation"]),
            test_initial_states=parent_orders(data, data.partitions["test"]),
            episode_per_iter=episodes, buffer_size=max(2048, episodes * 12),
            update_kwargs={"repeat": 5, "batch_size": 64} if kind == "ppo" else {})
        trainer = Trainer(max_iters=iterations, val_every_n_iters=iterations, finite_env_type="dummy", concurrency=1)
        run_id = client.create_run(experiment, run_name=f"nested_{kind}_11").info.run_id
        try:
            for key, value in {"algorithm": kind, "seed": 11, "iterations": iterations,
                               "episodes": episodes, "native_nested_execution": True}.items():
                client.log_param(run_id, key, value)
            for key in ("snapshot_id", "snapshot_sha256", "analytical_source", "response_sha256"):
                if key in data.provenance:
                    client.log_param(run_id, key, data.provenance[key])
            trainer.fit(vessel)
            change = max(float((p.detach() - old).abs().max()) for p, old in zip(model.parameters(), before))
            if not change > 0 or not all(torch.isfinite(p).all() for p in model.parameters()):
                raise ValueError("Native nested training failed to update finite weights")
            weights = output / f"{kind}.pth"
            torch.save(model.state_dict(), weights)
            checkpoint = output / f"{kind}_trainer.pth"
            torch.save(trainer.state_dict(), checkpoint)
            restored = policy(kind, frames, weights)
            tests = evaluate(data, frames, model, restored, data.partitions["test"])
            trainer.test(vessel)
            reports[kind] = {"episodes": sum(c["episodes"] for c in vessel.collections),
                             "steps": sum(c["steps"] for c in vessel.collections),
                             "maximum_parameter_change": change, "reload_max_logit_error": 0,
                             "native_test_metrics": {k: float(v) for k, v in trainer.metrics.items()},
                             "held_out_accounts": tests, "mlflow_run_id": run_id}
            if reports[kind]["episodes"] != iterations * episodes:
                raise ValueError("Native trainer did not complete the requested collection budget")
            client.log_metric(run_id, "maximum_parameter_change", change)
            for artifact in (weights, checkpoint):
                client.log_artifact(run_id, str(artifact))
                if Path(client.download_artifacts(run_id, artifact.name)).read_bytes() != artifact.read_bytes():
                    raise ValueError("MLflow nested checkpoint round trip failed")
            client.set_terminated(run_id, "FINISHED")
        except Exception:
            client.set_terminated(run_id, "FAILED")
            raise
    report = {"passed": True, "training": reports, "data": data.provenance,
              "independent_verification": {"passed": True}, "providers_modified": False,
              "real_orders_enabled": False, "scope": "Native funded buy execution tasks, not allocation alpha"}
    write_report(output / "report.json", report)
    return report


def main() -> None:
    """Worker entrypoint for full nested native training in the shared runtime."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--response", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--iterations", type=int, default=8)
    parser.add_argument("--episodes", type=int, default=16)
    args = parser.parse_args()
    run(args.response, args.output, iterations=args.iterations, episodes=args.episodes)


if __name__ == "__main__":
    main()
