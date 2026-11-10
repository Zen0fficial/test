"""Train and evaluate actual PPO policies on real HK.00700 data in the shared runtime."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import time
from decimal import Decimal
from datetime import date
from importlib.metadata import version
from pathlib import Path

import numpy as np
import pandas as pd
import torch
from mlflow.tracking import MlflowClient
from stable_baselines3 import PPO
from stable_baselines3.common.env_checker import check_env

from futu_bridge.ml import baseline

from .comparison import qlib_features
from .rl_env import AllocationEnv, ResearchTape, make_tape
from .session import read_report, write_report
from .tracking import tracking_uri

SEEDS = (11, 29, 47)
PARAMETERS = {"learning_rate": .0003, "n_steps": 512, "batch_size": 128, "n_epochs": 5,
              "gamma": .99, "gae_lambda": .95, "clip_range": .2, "ent_coef": .01,
              "vf_coef": .5, "max_grad_norm": .5, "device": "cpu"}


def summarize(result: dict[str, object]) -> dict[str, object]:
    """Keep scalar statistics separate from full action, equity and fill journals."""
    return {key: value for key, value in result.items() if key not in ("ledger", "equity", "actions")}


def action_probabilities(models: list[PPO], observation: np.ndarray) -> np.ndarray:
    """Average categorical policy probabilities; deterministic ties favor cash."""
    if not models:
        raise ValueError("Need at least one trained PPO policy")
    probabilities = []
    with torch.no_grad():
        for model in models:
            tensor, _ = model.policy.obs_to_tensor(observation)
            probability = model.policy.get_distribution(tensor).distribution.probs.cpu().numpy()[0]
            probabilities.append(probability)
    result = np.mean(probabilities, axis=0)
    if not np.isfinite(result).all() or abs(float(result.sum()) - 1) > 1e-5:
        raise ValueError("Invalid PPO action probabilities")
    return result


def evaluate(tape: ResearchTape, models: list[PPO], *, bps: int = 5,
             fixed_actions: list[int] | None = None) -> dict[str, object]:
    """Replay one frozen policy or explicit control without learning from evaluation data."""
    if fixed_actions is not None and len(fixed_actions) != len(tape.sessions):
        raise ValueError("Control actions do not cover the evaluation partition")
    environment = AllocationEnv(tape, record=True, slippage_bps=Decimal(bps))
    observation, _ = environment.reset(seed=0)
    done, index = False, 0
    while not done:
        action = fixed_actions[index] if fixed_actions is not None else int(
            action_probabilities(models, observation).argmax())
        observation, _, done, _, _ = environment.step(action)
        index += 1
    return environment.report()


def predict_saved(root: Path, features: pd.Series, *, feature_date: str,
                  cash: Decimal = Decimal(100000), peak: Decimal = Decimal(100000)) -> dict[str, object]:
    """Reload an eligible frozen fold for one completed-day manual-review decision.

    The caller supplies causal features and a flat portfolio's current cash.
    Saved local policy files are trusted research artifacts. No live prices are
    fetched, no learning occurs, and an action is not an order or a promised fill.
    """
    if (date.fromisoformat(feature_date).isoformat() != feature_date or not cash.is_finite()
            or not peak.is_finite() or cash <= 0 or peak < cash):
        raise ValueError("Invalid inference date or paper cash state")
    partitions = json.loads((root / "partitions.json").read_text(encoding="utf-8"))
    eligible = [index for index, part in enumerate(partitions) if part["test"]["first"] <= feature_date]
    if not eligible:
        raise ValueError("No policy was selected before this feature date")
    index = max(eligible)
    folder = root / f"fold{index + 1}"
    normalizer = read_report(folder / "normalizer.json")
    values = features[normalizer["features"]].to_numpy(dtype=float)
    if not np.isfinite(values).all():
        raise ValueError("RL inference requires finite completed-day features")
    values = np.clip((values - normalizer["mean"]) / normalizer["scale"], -10, 10)
    observation = np.asarray([*values, math.log(float(cash / 100000)), float(cash / peak - 1)], dtype=np.float32)
    models = [PPO.load(folder / f"ppo_seed{seed}.zip", device="cpu") for seed in SEEDS]
    probability = action_probabilities(models, observation)
    selected = read_report(folder / "selection.json")["selected"]
    raw_action = int(probability.argmax())
    return {"feature_date": feature_date, "fold": index + 1, "selected": selected,
            "probabilities": probability.tolist(), "raw_action": raw_action,
            "action": raw_action if selected == "ensemble" else 0,
            "timing": "next observed open entry, same-session close exit",
            "real_orders_enabled": False}


def fit_policy(tape: ResearchTape, validation: ResearchTape, seed: int, steps: int,
               root: Path, client: MlflowClient, experiment_id: str) -> tuple[PPO, dict[str, object]]:
    """Train on a contiguous-window training environment and verify serialization and MLflow."""
    environment = AllocationEnv(tape, episode_length=min(126, len(tape.sessions)))
    model = PPO("MlpPolicy", environment, seed=seed, policy_kwargs={"net_arch": [32, 32]}, **PARAMETERS)
    initial = torch.cat([parameter.detach().flatten().clone() for parameter in model.policy.parameters()])
    run = client.create_run(experiment_id, run_name=f"{root.name}-seed{seed}")
    run_id = run.info.run_id
    started = time.monotonic()
    try:
        for name, value in {**PARAMETERS, "seed": seed, "steps": steps, "network": "32,32",
                            "train_sessions": len(tape.sessions), "real_orders_enabled": False}.items():
            client.log_param(run_id, name, value)
        model.learn(total_timesteps=steps)
        updated = torch.cat([parameter.detach().flatten() for parameter in model.policy.parameters()])
        change = float(torch.max(torch.abs(updated - initial)))
        if not change > 0 or not torch.isfinite(updated).all():
            raise ValueError("PPO did not learn finite updated parameters")
        model_file = root / f"ppo_seed{seed}.zip"
        model.save(model_file)
        loaded = PPO.load(model_file, device="cpu")
        observation, _ = AllocationEnv(validation).reset()
        error = float(np.max(np.abs(action_probabilities([model], observation)
                                    - action_probabilities([loaded], observation))))
        if error > 1e-7:
            raise ValueError("Saved PPO policy does not reproduce action probabilities")
        result = evaluate(validation, [loaded])
        evidence = {"seed": seed, "timesteps": model.num_timesteps, "maximum_parameter_change": change,
                    "reload_probability_error": error, "training_seconds": time.monotonic() - started,
                    "validation": summarize(result), "mlflow_run_id": run_id}
        write_report(root / f"validation_seed{seed}.json", result)
        for name in ("net_return", "ending_equity_hkd", "fees_hkd", "round_trips"):
            client.log_metric(run_id, f"validation_{name}", result[name])
        client.log_metric(run_id, "actual_timesteps", model.num_timesteps)
        client.log_metric(run_id, "maximum_parameter_change", change)
        client.log_artifact(run_id, str(model_file))
        restored = Path(client.download_artifacts(run_id, model_file.name))
        if restored.read_bytes() != model_file.read_bytes():
            raise ValueError("MLflow PPO artifact differs from the saved policy")
        client.set_terminated(run_id, "FINISHED")
        return loaded, evidence
    except Exception:
        client.set_terminated(run_id, "FAILED")
        raise
    finally:
        environment.close()


def rolling_partitions(data: pd.DataFrame, test_index: pd.DatetimeIndex) -> list[dict[str, pd.DataFrame]]:
    """Define three expanding training windows with 331 validation days and one-label purges."""
    result = []
    for dates in np.array_split(test_index, 3):
        start = data.index.get_loc(dates[0])
        validation = data.iloc[start - 332:start - 1]
        training = data.iloc[:start - 333]
        test = data.loc[dates]
        if (len(training) < 500 or len(validation) != 331
                or training.label_date.max() >= validation.index.min()
                or validation.label_date.max() >= test.index.min()):
            raise ValueError("RL partitions failed chronological label purging")
        result.append({"train": training, "validation": validation, "test": test})
    return result


def walk_forward(tape: ResearchTape, folds: list[dict[str, object]], *, gated: bool,
                 bps: int = 5) -> dict[str, object]:
    """Switch frozen fold policies on schedule while carrying cash and monthly fees continuously."""
    environment = AllocationEnv(tape, record=True, slippage_bps=Decimal(bps))
    observation, _ = environment.reset()
    index, fold_index, done = 0, 0, False
    boundary = folds[0]["test_count"]
    while not done:
        if index >= boundary:
            fold_index += 1
            boundary += folds[fold_index]["test_count"]
        fold = folds[fold_index]
        action = 0 if gated and fold["selected"] == "cash" else int(
            action_probabilities(fold["models"], observation).argmax())
        observation, _, done, _, _ = environment.step(action)
        index += 1
    return environment.report()


def run(response_path: Path, baseline_dir: Path, output: Path, qlib_source: Path,
        *, steps: int = 32768) -> dict[str, object]:
    """Freeze the research plan, train nine real PPO policies, and evaluate without test tuning."""
    from sklearn.preprocessing import StandardScaler

    if steps < 512 or steps % 512:
        raise ValueError("Training steps must be a positive multiple of the 512-step rollout")
    root = output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    torch.set_num_threads(2)
    torch.use_deterministic_algorithms(True)
    raw = response_path.read_bytes()
    response = json.loads(raw)
    (root / "provider_response.json").write_bytes(raw)
    frame = baseline.validate_response(response, "2020-01-01", response["data"]["bars"][-1]["date"])
    old_response = read_report(baseline_dir / "provider_response.json")
    old = baseline.validate_response(old_response, "2020-01-01", old_response["data"]["bars"][-1]["date"])
    if not frame.loc[old.index].equals(old):
        raise ValueError("Historical prices differ from the original baseline")
    original_parts = baseline.chronological_split(baseline.samples(old))
    plan = {"algorithm": "Stable-Baselines3 PPO", "seeds": SEEDS, "steps_per_seed_per_fold": steps,
            "folds": 3, "policy_network": [32, 32], "parameters": PARAMETERS,
            "train_episode_sessions": 126, "normalization": "training-only StandardScaler, then clip to [-10,10]",
            "reward": "100 * log(equity_after/equity_before), after all fees and slippage",
            "actions": {"0": "cash", "1": "one 100-share lot", "2": "maximum affordable whole lots"},
            "execution": "previous completed bar -> next open entry -> same-session close exit",
            "capital_hkd": 100000, "slippage_bps_per_side": 5,
            "ensemble": "equal mean of all three policies' action probabilities; deterministic argmax",
            "selection": "ensemble only if validation ending equity exceeds cash; otherwise cash",
            "test_status": "Previously examined period; all walk-forward results are retrospective",
            "primary": "validation-gated walk-forward; cash carries across all three test folds",
            "real_orders_enabled": False}
    write_report(root / "plan.json", plan)
    features = qlib_features(response, frame, root, qlib_source)
    features.to_csv(root / "features.csv", index_label="feature_date")
    columns = features.columns.tolist()
    data = features.join(baseline.samples(frame)[["label", "label_date"]]).dropna()
    parts = rolling_partitions(data, original_parts["test"].index)
    write_report(root / "partitions.json", [{name: {"first": str(p.index.min().date()),
                   "last": str(p.index.max().date()), "last_label": str(p.label_date.max().date()), "n": len(p)}
                   for name, p in fold.items()} for fold in parts])
    client = MlflowClient(tracking_uri=tracking_uri(root))
    experiment_id = client.create_experiment("hk00700-rl", artifact_location=(root / "rl_artifacts").as_uri())
    folds, fold_reports, normalized_tests = [], [], []
    for index, partition in enumerate(parts):
        folder = root / f"fold{index + 1}"
        folder.mkdir()
        scaler = StandardScaler().fit(partition["train"][columns])
        normalized = {name: pd.DataFrame(np.clip(scaler.transform(p[columns]), -10, 10),
                                         index=p.index, columns=columns) for name, p in partition.items()}
        write_report(folder / "normalizer.json", {"features": columns, "mean": scaler.mean_.tolist(),
                                                  "scale": scaler.scale_.tolist(), "clip": [-10, 10]})
        tapes = {name: make_tape(frame, values) for name, values in normalized.items()}
        check_env(AllocationEnv(tapes["train"], episode_length=126), warn=True)
        models, training = [], []
        for seed in SEEDS:
            write_report(root / "progress.json", {"stage": "training", "fold": index + 1, "seed": seed})
            model, evidence = fit_policy(tapes["train"], tapes["validation"], seed, steps,
                                         folder, client, experiment_id)
            models.append(model)
            training.append(evidence)
        validation = evaluate(tapes["validation"], models)
        selected = "ensemble" if validation["ending_equity_hkd"] > 100000 else "cash"
        write_report(folder / "validation_ensemble.json", validation)
        write_report(folder / "selection.json", {"selected": selected, "validation": summarize(validation)})
        test_runs = {}
        choices = [(f"seed{seed}", [model]) for seed, model in zip(SEEDS, models)] + [("ensemble", models)]
        for name, policies in choices:
            result = evaluate(tapes["test"], policies)
            write_report(folder / f"test_{name}.json", result)
            test_runs[name] = summarize(result)
        fold_reports.append({"fold": index + 1, "selected": selected, "training": training,
                             "validation_ensemble": summarize(validation), "test_reset_capital": test_runs})
        folds.append({"models": models, "selected": selected, "test_count": len(tapes["test"].sessions)})
        normalized_tests.append(normalized["test"])
    test_features = pd.concat(normalized_tests)
    tape = make_tape(frame, test_features)
    results = {}
    for bps in (0, 5, 10):
        for gated in (True, False):
            name = f"{'selected' if gated else 'ensemble'}_slippage{bps}"
            result = walk_forward(tape, folds, gated=gated, bps=bps)
            write_report(root / f"simulation_{name}.json", result)
            results[name] = summarize(result)
    original_model = read_report(baseline_dir / "evaluation_model.json")
    predictions = [baseline.predict_portable(original_model, row)
                   for _, row in data.loc[test_features.index].iterrows()]
    controls = {"baseline": [2 if p >= .5 else 0 for p in predictions], "cash": [0] * len(tape.sessions),
                "always_long_intraday": [2] * len(tape.sessions)}
    for name, actions in controls.items():
        result = evaluate(tape, [], fixed_actions=actions)
        write_report(root / f"simulation_{name}.json", result)
        results[name] = summarize(result)
    runs = client.search_runs([experiment_id])
    if len(runs) != 9 or any(run.info.status != "FINISHED" for run in runs):
        raise ValueError("Not all nine PPO runs finished in MLflow")
    report = {"completed": True, "plan": plan, "folds": fold_reports, "simulations": results,
              "raw_bars": len(frame), "test_sessions": len(tape.sessions),
              "response_sha256": hashlib.sha256(raw).hexdigest(),
              "code_sha256": {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in
                              (Path(__file__), Path(__file__).with_name("rl_env.py"),
                               Path(__file__).with_name("simulation.py"), Path(__file__).with_name("costs.py"))},
              "versions": {name: version(name) for name in ("torch", "gymnasium", "stable-baselines3", "pyqlib",
                           "nautilus_trader", "mlflow", "numpy", "pandas", "pyarrow")},
              "mlflow_finished_runs": len(runs), "model_reload_verified": True, "providers_modified": False,
              "real_orders_enabled": False,
              "limitations": ["Retrospective walk-forward on a previously examined period",
                              "Repeated training windows are not new independent market observations",
                              "Daily flat-overnight allocation; no multi-day inventory or dividend modeling",
                              "Assumed fills and current fees; not certified historical execution",
                              "No cash interest, FX, financing, or settled-cash restriction modeling"]}
    write_report(root / "report.json", report)
    write_report(root / "progress.json", {"stage": "complete"})
    return report


def main() -> None:
    """Run explicit local RL research with saved market-data inputs and bounded training."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--response", type=Path, required=True)
    parser.add_argument("--baseline-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--qlib-source", type=Path, required=True)
    parser.add_argument("--steps", type=int, default=32768)
    args = parser.parse_args()
    report = run(args.response, args.baseline_dir, args.output, args.qlib_source, steps=args.steps)
    print(json.dumps({"completed": report["completed"], "simulation": report["simulations"]["selected_slippage5"]}))


if __name__ == "__main__":
    main()
