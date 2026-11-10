"""Native learner conformance over real daily Parquet snapshots, with honest failure retention."""

from __future__ import annotations

import argparse
from copy import deepcopy
import gc
import hashlib
import inspect
import json
from pathlib import Path
import pickle
from time import perf_counter
import traceback
from typing import Any

import numpy as np
import pandas as pd

from .qlib_model_extensions import configurations, dataset_config
from .qlib_parquet import initialize, view
from .session import write_report
from .storage import SnapshotStore
from .storage.captures import ingest
from .tracking import prepare_tracking


def series(value: pd.Series | pd.DataFrame) -> pd.Series:
    """Retain the native score column when transport models return additional diagnostics."""
    if isinstance(value, pd.Series):
        return value
    if "score" not in value.columns:
        raise ValueError("Native prediction output lacks an identified score column")
    return value["score"]


def fit_one(name: str, spec: dict[str, Any], dataset: Any, output: Path,
            snapshot: str, snapshot_hash: str) -> dict[str, Any]:
    """Fit a native learner, prove finite changed state and exact serialized inference."""
    from qlib.utils import init_instance_by_config
    from qlib.workflow import R
    from .qlib_features_models import neural_weights

    output.mkdir()
    model = init_instance_by_config(spec["model"])
    before = neural_weights(model)
    initial = pickle.dumps(model)
    arguments = inspect.signature(model.fit).parameters
    settings: dict[str, Any] = {}
    if "save_path" in arguments:
        settings["save_path"] = str(output / "native_weights.bin")
    if "num_boost_round" in arguments:
        settings["num_boost_round"] = 20
    if "early_stopping_rounds" in arguments:
        settings["early_stopping_rounds"] = 5
    if "verbose_eval" in arguments:
        settings["verbose_eval"] = False
    if "verbose" in arguments:
        settings["verbose"] = False
    started = perf_counter()
    with R.start(experiment_name="parquet-native-model-extensions", recorder_name=name):
        model.fit(dataset, **settings)
        artifact = pickle.dumps(model)
        after = neural_weights(model)
        if artifact == initial or before is not None and before == after:
            raise ValueError("Native fitting did not change model state")
        prediction = series(model.predict(deepcopy(dataset))).sort_index()
        path = output / "model.pkl"
        path.write_bytes(artifact)
        restored = pickle.loads(path.read_bytes())
        reloaded = series(restored.predict(deepcopy(dataset))).sort_index()
        pd.testing.assert_series_equal(prediction, reloaded, check_exact=True)
        label = dataset.handler.fetch(col_set="label", data_key="learn").iloc[:, 0].reindex(prediction.index)
        if not prediction.index.is_unique or not len(prediction) or not np.isfinite(prediction).all():
            raise ValueError("Native prediction output is empty, duplicate or nonfinite")
        if not label.notna().all():
            raise ValueError("Native predictions are not aligned with observed held-out labels")
        prediction.to_frame("score").to_parquet(output / "predictions.parquet")
        mse = float(np.mean((prediction - label) ** 2))
        R.log_metrics(test_mse=mse, prediction_rows=len(prediction))
        R.get_recorder().set_tags(joint_snapshot_id=snapshot, joint_snapshot_sha256=snapshot_hash,
                                 real_orders_enabled=False, acceptance_configuration=True)
        R.get_recorder().log_artifact(str(path))
        run_id = R.get_recorder().id
    result = {"passed": True, "native_class": spec["model"]["class"], "configuration": spec,
              "fit_arguments": settings, "seconds": perf_counter() - started, "prediction_rows": len(prediction),
              "test_mse_descriptive_only": mse, "snapshot_id": snapshot, "snapshot_sha256": snapshot_hash,
              "reload_predictions_exact": True, "learned_state_changed": True, "mlflow_run_id": run_id,
              "model_sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "real_orders_enabled": False}
    write_report(output / "report.json", result)
    return result


def run(response: Path, output: Path, repo: Path, *, panel: bool = False,
        only: list[str] | None = None) -> dict[str, Any]:
    """Exercise all explicitly selected additional native wrappers using native dataset implementations."""
    import torch
    from qlib.utils import init_instance_by_config

    torch.set_num_threads(1)
    source_hashes = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                     for path in Path(__file__).parent.glob("qlib_model_extensions*.py")}
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    if panel:
        panel_root = repo / "output/joint/plan_completion_20261110/panel"
        manifest = json.loads((panel_root / "report.json").read_text())
        if not manifest.get("passed"):
            raise ValueError("The real manually selected panel capture is not verified")
        store = SnapshotStore(panel_root / "store")
        snapshot = store.snapshot(manifest["snapshot"])
    else:
        store = SnapshotStore(output / "store")
        snapshot = ingest(store, response, "hk00700_daily", "futu-bars")
    source = view(str(store.root), snapshot.identity)
    instruments = sorted(set(source.frame.index.get_level_values("instrument")))
    frame = source.frame.xs(instruments[0], level="instrument")
    frame = frame[["open", "high", "low", "close", "volume"]].astype(float)
    initialize(store.root, snapshot.identity, cache=output / "cache", exp_manager=prepare_tracking(output))
    configs = configurations(output, panel=panel)
    if only:
        unknown = set(only) - set(configs)
        if unknown:
            raise ValueError(f"Unknown requested native model: {sorted(unknown)}")
        configs = {name: configs[name] for name in only}
    results, cached_datasets = {}, {}
    for name, spec in configs.items():
        print("Starting native extension", name, flush=True)
        try:
            kind = spec["dataset_kind"]
            if kind not in cached_datasets:
                cached_datasets[kind] = init_instance_by_config(dataset_config(kind, frame, instruments))
                cached_datasets[kind].config(dump_all=True, recursive=True)
            results[name] = fit_one(name, spec, deepcopy(cached_datasets[kind]), output / name,
                                    snapshot.identity, snapshot.sha256)
            print("Passed native extension", name, flush=True)
        except Exception as error:
            folder = output / name
            folder.mkdir(exist_ok=True)
            (folder / "traceback.txt").write_text(traceback.format_exc(), encoding="utf-8")
            results[name] = {"passed": False, "error_type": type(error).__name__, "error": str(error),
                             "configuration": spec, "status": "compatibility_or_adapter_failure"}
            write_report(folder / "report.json", results[name])
            print("Failed native extension", name, type(error).__name__, str(error), flush=True)
        gc.collect()
    unchanged = source_hashes == {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                                  for path in Path(__file__).parent.glob("qlib_model_extensions*.py")}
    report = {"passed": unchanged and all(result["passed"] for result in results.values()), "models": results,
              "model_count": len(results), "passed_models": sum(result["passed"] for result in results.values()),
              "instruments": instruments, "real_rows": snapshot.rows, "snapshot_id": snapshot.identity,
              "snapshot_sha256": snapshot.sha256, "real_orders_enabled": False, "providers_modified": False,
              "scope": "Native API, training and serialization conformance with explicitly bounded new configurations",
              "source_hashes": source_hashes, "sources_unchanged_during_verification": unchanged,
              "limitations": ["No claim of optimized predictive performance or broad profitability",
                              "Manual current stock basket has selection bias and is not a historical universe",
                              "Unavailable VWAP is explicitly imputed; no invented turnover scale"]}
    write_report(output / "report.json", report)
    return report


def main() -> None:
    """Run bounded native learner conformance and retain every failure for follow-up."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--response", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--panel", action="store_true")
    parser.add_argument("--only", nargs="+")
    args = parser.parse_args()
    result = run(args.response, args.output, args.repo, panel=args.panel, only=args.only)
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
