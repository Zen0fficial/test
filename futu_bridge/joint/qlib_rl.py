"""Native Qlib PPO/DQN training on real local HK.00700 execution tasks.

This command invokes upstream Trainer, TrainingVessel learning, Recurrent,
PPO/DQN, FullHistoryStateInterpreter, CategoricalActionInterpreter, EnvWrapper
and SingleAssetOrderExecutionSimple. It is independent of the SB3 allocation
workflow. All executions are local simulations; no routing client is present.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import random
import shutil
import time
import warnings

import numpy as np
import torch
from mlflow.tracking import MlflowClient
from qlib.rl.trainer import Trainer
from tianshou.data import Batch

from .qlib_rl_components import (CostReward, SimulatorFactory, SynchronousVessel, interpreters,
                                 make_environment, make_policy, orders)
from .qlib_rl_data import ExecutionData, load_data, normalized_features
from .qlib_rl_ledger import evaluate
from .session import write_report
from .tracking import tracking_uri

SEEDS = (11, 29, 47)


def scalars(result: dict[str, object]) -> dict[str, object]:
    """Separate scalar evidence from full native trajectories and artificial ledgers."""
    return {key: value for key, value in result.items() if key not in ("sessions", "ledger")}


def verify_reload(data: ExecutionData, frames: dict, original: object, restored: object) -> float:
    """Compare native logits and actions at every validation-trajectory decision."""
    error = 0.0
    original.eval()
    restored.eval()
    for day in data.partitions["validation"]:
        env = make_environment(data, frames, day)
        observation, done = env.reset(), False
        while not done:
            batch = Batch([{"obs": observation, "info": {}}])
            with torch.no_grad():
                a, b = original(batch), restored(batch)
            error = max(error, float(torch.max(torch.abs(a.logits - b.logits))))
            if not np.array_equal(a.act, b.act):
                raise ValueError("Reloaded native policy changed its decision")
            observation, _, done, _ = env.step(int(np.asarray(a.act)[0]))
        env.close()
    if error > 1e-7:
        raise ValueError("Native policy logits failed serialization verification")
    return error


def fit(data: ExecutionData, frames: dict, root: Path, kind: str, seed: int, iterations: int,
        episodes: int, client: MlflowClient, experiment: str) -> tuple[object, dict[str, object], Trainer]:
    """Train one upstream policy, audit updates, save/reload and log artifacts."""
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    policy = make_policy(kind, frames)
    before = {key: value.detach().clone() for key, value in policy.named_parameters()}
    state, action = interpreters(frames)
    vessel = SynchronousVessel(
        seed=seed, simulator_fn=SimulatorFactory(data.blocks), state_interpreter=state,
        action_interpreter=action, policy=policy, reward=CostReward(),
        train_initial_states=orders(data, data.partitions["train"]),
        val_initial_states=orders(data, data.partitions["validation"]),
        test_initial_states=orders(data, data.partitions["test"]),
        episode_per_iter=episodes, buffer_size=max(2048, episodes * 12),
        update_kwargs={"repeat": 5, "batch_size": 64} if kind == "ppo" else {})
    trainer = Trainer(max_iters=iterations, val_every_n_iters=iterations,
                      finite_env_type="dummy", concurrency=1)
    run_id = client.create_run(experiment, run_name=f"qlib_{kind}_{seed}").info.run_id
    started = time.monotonic()
    try:
        for key, value in {"algorithm": kind, "seed": seed, "iterations": iterations,
                           "episodes_per_iteration": episodes, "native_trainer": True,
                           "native_simple_simulator": True, "real_orders_enabled": False}.items():
            client.log_param(run_id, key, value)
        for key in ("snapshot_id", "snapshot_sha256", "analytical_source", "response_sha256"):
            if key in data.provenance:
                client.log_param(run_id, key, data.provenance[key])
        trainer.fit(vessel)
        changes = [float(torch.max(torch.abs(value.detach() - before[key])))
                   for key, value in policy.named_parameters()]
        if (not max(changes) > 0 or not all(torch.isfinite(p).all() for p in policy.parameters())
                or sum(c["episodes"] for c in vessel.collections) != iterations * episodes):
            raise ValueError("Native policy did not complete finite, effective learning")
        weights = root / f"{kind}_seed{seed}.pth"
        torch.save(policy.state_dict(), weights)
        checkpoint = root / f"{kind}_seed{seed}_trainer.pth"
        continuation = trainer.state_dict()
        continuation["should_stop"] = False
        torch.save(continuation, checkpoint)
        restored = make_policy(kind, frames, weights)
        restored.eval()
        reload_error = verify_reload(data, frames, policy, restored)
        validation = evaluate(data, frames, data.partitions["validation"], restored)
        write_report(root / f"{kind}_seed{seed}_validation.json", validation)
        report = {"algorithm": kind, "seed": seed, "native_iterations": trainer.current_iter,
                  "episodes": sum(c["episodes"] for c in vessel.collections),
                  "steps": sum(c["steps"] for c in vessel.collections),
                  "maximum_parameter_change": max(changes), "reload_max_logit_error": reload_error,
                  "validation": scalars(validation), "mlflow_run_id": run_id,
                  "training_seconds": time.monotonic() - started}
        for key in ("episodes", "steps", "maximum_parameter_change", "reload_max_logit_error"):
            client.log_metric(run_id, key, report[key])
        client.log_metric(run_id, "validation_execution_advantage_bps",
                          validation["mean_net_execution_advantage_bps"])
        for artifact in (weights, checkpoint):
            client.log_artifact(run_id, str(artifact))
            downloaded = Path(client.download_artifacts(run_id, artifact.name))
            if downloaded.read_bytes() != artifact.read_bytes():
                raise ValueError("MLflow native Qlib artifact did not round trip")
        client.set_terminated(run_id, "FINISHED")
        return restored, report, trainer
    except Exception:
        client.set_terminated(run_id, "FAILED")
        raise


def run(response: Path, output: Path, *, iterations: int = 24, episodes: int = 32,
        data: ExecutionData | None = None) -> dict[str, object]:
    """Freeze data/splits, train six native policies, select on validation, then test."""
    if output.exists() or iterations < 1 or episodes < 2:
        raise ValueError("Use a new output directory and positive training budget")
    torch.set_num_threads(1)
    # The native pandas concatenation behavior is tested with our pinned pandas.
    warnings.filterwarnings("ignore", category=FutureWarning,
                            module=r"qlib\.rl\.order_execution\.simulator_simple")
    data = load_data(response) if data is None else data
    if data.provenance.get("response_sha256") != hashlib.sha256(response.read_bytes()).hexdigest():
        raise ValueError("Execution data does not match the archived provenance capture")
    frames, normalizer = normalized_features(data)
    output.mkdir(parents=True)
    shutil.copyfile(response, output / "provider_response.json")
    write_report(output / "data.json", data.provenance)
    write_report(output / "partitions.json", data.partitions)
    write_report(output / "normalizer.json", normalizer)
    client = MlflowClient(tracking_uri=tracking_uri(output))
    experiment = client.create_experiment("native-qlib-execution", artifact_location=(
        output.resolve() / "mlartifacts").as_uri())
    models, reports, trainers = {}, {}, {}
    for kind in ("ppo", "dqn"):
        for seed in SEEDS:
            name = f"{kind}_seed{seed}"
            models[name], reports[name], trainers[name] = fit(
                data, frames, output, kind, seed, iterations, episodes, client, experiment)
    scores = {name: report["validation"]["mean_net_execution_advantage_bps"]
              for name, report in reports.items()}
    for control in ("immediate", "two_lot_schedule"):
        result = evaluate(data, frames, data.partitions["validation"], control=control)
        write_report(output / f"{control}_validation.json", result)
        scores[control] = result["mean_net_execution_advantage_bps"]
    selected = max(sorted(scores), key=scores.get)
    write_report(output / "selection.json", {"selected": selected, "validation_scores": scores,
                                            "metric": "mean net buy execution advantage vs grid TWAP"})
    tests = {}
    for name in scores:
        result = evaluate(data, frames, data.partitions["test"], models.get(name),
                          control=name if name not in models else None)
        write_report(output / f"{name}_test.json", result)
        tests[name] = scalars(result)
        if name in trainers:
            trainer = trainers[name]
            trainer.test(trainer.vessel)
            reports[name]["native_test_metrics"] = {k: float(v) for k, v in trainer.metrics.items()}
    scenarios = {}
    for bps in (0, 5, 10):
        result = evaluate(data, frames, data.partitions["test"], models.get(selected),
                          control=selected if selected not in models else None, slippage_bps=bps)
        write_report(output / f"selected_slippage{bps}.json", result)
        scenarios[str(bps)] = scalars(result)
    finished = client.search_runs([experiment])
    if len(finished) != 6 or any(r.info.status != "FINISHED" for r in finished):
        raise ValueError("Incomplete native Qlib experiment tracking")
    report = {"completed": True, "backend": "native-qlib", "data": data.provenance,
              "partitions": data.partitions, "selected": selected, "training": reports,
              "tests": tests, "selected_slippage_scenarios": scenarios, "mlflow_finished_runs": 6,
              "model_reload_verified": True, "providers_modified": False, "real_orders_enabled": False,
              "code_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                               for p in Path(__file__).parent.glob("qlib_rl*.py")},
              "limitations": ["Small retrospective dataset: integration evidence, not evidence of alpha",
                              "Prescribed 200-share buy task; does not learn when to enter the market",
                              "Native simple simulator; full nested Qlib exchange simulator is separate",
                              "Thirty-minute open fills and close liquidation are assumptions",
                              "No partial fills, queue matching, margin, or real orders",
                              "Current IBKR Tier-I schedule applied to all dates"]}
    write_report(output / "report.json", report)
    return report


def main() -> None:
    """Run explicitly selected native RL training with immutable local input."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--response", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--iterations", type=int, default=24)
    parser.add_argument("--episodes", type=int, default=32)
    args = parser.parse_args()
    report = run(args.response, args.output, iterations=args.iterations, episodes=args.episodes)
    print(json.dumps({key: report[key] for key in ("completed", "selected", "mlflow_finished_runs")}, indent=2))


if __name__ == "__main__":
    main()
