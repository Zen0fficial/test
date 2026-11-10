"""Real-data differential acceptance of Qlib's legacy and Parquet storage paths."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
from pathlib import Path
import pickle
from typing import Any

import numpy as np
import pandas as pd

from .qlib_parquet import ParquetFeatureStorage, export_bin, initialize
from .qlib_parquet_cache import ParquetExpressionProvider
from .qlib_parquet_execution import execution_from_snapshot
from .session import write_report
from .storage import SnapshotStore
from .storage.captures import ingest


def evaluate_handlers(frame: pd.DataFrame, output: Path) -> dict[str, Any]:
    """Evaluate unchanged native Alpha158/360, DatasetH and train-only native Ridge."""
    from qlib.data import D
    from qlib.data.dataset import DatasetH
    from qlib.utils import init_instance_by_config
    from .qlib_features import SYMBOL, handler_config, segments, verify_features
    from .qlib_features_models import model_configs

    output.mkdir()
    split = segments(frame)
    results: dict[str, Any] = {"calendar": D.calendar(), "instruments": D.list_instruments(D.instruments("all"))}
    fields = ["$close", "Ref($close,1)", "$close/Mean($close,5)-1", "Ref($close,-1)/$close-1", "$vwap"]
    results["expressions"] = D.features([SYMBOL], fields, disk_cache=1)
    for name in ("Alpha158", "Alpha360"):
        handler = init_instance_by_config(handler_config(name, frame, split))
        results[name + "_audit"] = verify_features(name, handler, frame, split)
        for kind in ("raw", "infer", "learn"):
            values = handler.fetch(data_key=kind)
            results[name + "_" + kind] = values
            values.to_parquet(output / f"{name}_{kind}.parquet")
        dataset = DatasetH(handler=handler, segments=split)
        model = init_instance_by_config(model_configs()["linear_ridge"])
        model.fit(dataset)
        prediction = model.predict(dataset)
        restored = pickle.loads(pickle.dumps(model)).predict(dataset)
        pd.testing.assert_series_equal(prediction, restored, check_exact=True)
        results[name + "_prediction"] = prediction
        prediction.to_frame("score").to_parquet(output / f"{name}_predictions.parquet")
        results[name + "_split"] = split
    return results


def verify_execution(minute_response: Path, output: Path, *, train_rl: bool = False) -> dict[str, Any]:
    """Compare all RL inputs and optionally run full unchanged simple/nested training budgets."""
    from .qlib_rl_data import FIELDS, load_data, normalized_features

    store = SnapshotStore(output / "minute_store")
    snapshot = ingest(store, minute_response, "hk00700_minutes", "futu-bars")
    parquet = execution_from_snapshot(store.root, snapshot.identity)
    original = load_data(minute_response)
    pd.testing.assert_frame_equal(original.minutes[list(FIELDS)], parquet.minutes[list(FIELDS)], check_exact=True)
    if original.partitions != parquet.partitions:
        raise ValueError("Parquet execution changed chronological partitions")
    for day in original.blocks:
        pd.testing.assert_frame_equal(original.blocks[day], parquet.blocks[day], check_exact=True)
    old_frames, old_normalizer = normalized_features(original)
    new_frames, new_normalizer = normalized_features(parquet)
    if old_normalizer != new_normalizer:
        raise ValueError("Parquet changed train-only RL normalization")
    for day in old_frames:
        pd.testing.assert_frame_equal(old_frames[day], new_frames[day], check_exact=True)
    report = {"passed": True, "snapshot": asdict(snapshot), "continuous_minutes": len(parquet.minutes),
              "sessions": len(parquet.blocks), "partitions": parquet.partitions, "normalization_exact": True,
              "simple_nested_training": "not_requested", "real_orders_enabled": False}
    if train_rl:
        from mlflow.tracking import MlflowClient
        from .qlib_rl import run as simple
        from .qlib_nested_rl import run as nested
        from .tracking import tracking_uri

        simple_report = simple(minute_response, output / "simple_rl", data=parquet)
        nested_report = nested(minute_response, output / "nested_rl", data=parquet)
        if not simple_report.get("completed") or not nested_report.get("passed"):
            raise ValueError("Native RL acceptance failed")
        for name, expected_runs in (("simple_rl", 6), ("nested_rl", 2)):
            client = MlflowClient(tracking_uri=tracking_uri(output / name))
            experiments = client.search_experiments()
            runs = client.search_runs([experiment.experiment_id for experiment in experiments])
            if len(runs) != expected_runs or any(run.info.status != "FINISHED" for run in runs):
                raise ValueError("Native Parquet RL did not finish all tracked policies")
            if any(run.data.params.get("snapshot_id") != snapshot.identity
                   or run.data.params.get("snapshot_sha256") != snapshot.sha256 for run in runs):
                raise ValueError("MLflow model lineage is not bound to the input snapshot")
        report["simple_nested_training"] = {"passed": True, "simple_policies": 6, "nested_policies": 2,
            "simple_iterations": 24, "simple_episodes_per_iteration": 32,
            "nested_iterations": 8, "nested_episodes_per_iteration": 16,
            "simple_report": str(output / "simple_rl" / "report.json"),
            "nested_report": str(output / "nested_rl" / "report.json"), "mlflow_snapshot_lineage_runs": 8}
    return report


def run(response: Path, output: Path, repo: Path, *, train_rl: bool = False) -> dict[str, Any]:
    """Verify real daily model parity, snapshot-keyed caches and minute execution conversion."""
    import qlib
    from qlib.config import REG_US
    from qlib.data import D
    from qlib.data.data import DatasetD
    from qlib.data.storage.file_storage import FileFeatureStorage
    from .qlib_features import SYMBOL, load_daily, storage
    from .tracking import prepare_tracking

    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    paths = list(Path(__file__).parent.glob("qlib_parquet*.py"))
    paths += [Path(__file__).with_name(name) for name in ("qlib_rl.py", "qlib_nested_rl.py")]
    source_hashes = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
    frame = load_daily(response)
    store = SnapshotStore(output / "store")
    snapshot = ingest(store, response, "hk00700_daily", "futu-bars")
    storage(frame, output / "legacy_bin")
    export_bin(store.root, snapshot.identity, output / "canonical_bin")
    for original in (output / "legacy_bin" / "features").rglob("*.bin"):
        converted = output / "canonical_bin" / original.relative_to(output / "legacy_bin")
        if original.read_bytes() != converted.read_bytes():
            raise ValueError("Parquet-derived native bin cache differs from the original exporter")
    tracking = prepare_tracking(output)
    qlib.init(provider_uri=str(output / "legacy_bin"), region=REG_US, kernels=1, logging_level=40,
              expression_cache=None, dataset_cache=None, exp_manager=tracking)
    legacy = evaluate_handlers(frame, output / "legacy")
    for field in ("open", "high", "low", "close", "volume", "factor", "vwap"):
        old = FileFeatureStorage(SYMBOL, field, "day")
        new = ParquetFeatureStorage(str(store.root), snapshot.identity, SYMBOL, field, "day")
        pd.testing.assert_series_equal(old[:], new[:], check_exact=True)
        for bounds in (slice(0, 10), slice(10, 20), slice(1680, 2000), slice(5, 5), slice(-10, 5)):
            pd.testing.assert_series_equal(old[bounds], new[bounds], check_exact=True)
    initialize(store.root, snapshot.identity, cache=output / "cache",
               context={"processor_fit_interval": legacy["Alpha158_split"]["train"]},
               exp_manager=tracking)
    parquet = evaluate_handlers(frame, output / "parquet")
    for key in legacy:
        if isinstance(legacy[key], pd.DataFrame):
            pd.testing.assert_frame_equal(legacy[key], parquet[key], check_exact=True)
        elif isinstance(legacy[key], pd.Series):
            pd.testing.assert_series_equal(legacy[key], parquet[key], check_exact=True)
        elif isinstance(legacy[key], np.ndarray):
            np.testing.assert_array_equal(legacy[key], parquet[key])
        elif legacy[key] != parquet[key]:
            raise ValueError(f"Parquet differential mismatch: {key}")
    fields = list(legacy["expressions"].columns)
    one = D.features([SYMBOL], fields, disk_cache=1)
    two = D.features([SYMBOL], fields, disk_cache=1)
    pd.testing.assert_frame_equal(one, two, check_exact=True)
    if DatasetD.cache.hits < 2:
        raise ValueError("Repeated native requests did not hit the Parquet dataset cache")
    expression = ParquetExpressionProvider(str(store.root), snapshot.identity, str(output / "expression_cache"))
    first = expression.expression(SYMBOL, "$close/Mean($close,5)-1", str(frame.index[0]), str(frame.index[-1]))
    second = expression.expression(SYMBOL, "$close/Mean($close,5)-1", str(frame.index[0]), str(frame.index[-1]))
    pd.testing.assert_series_equal(first, second, check_exact=True)
    cache_hits = DatasetD.cache.hits
    minute_response = repo / "output/joint/qlib_native_rl_data_20261109/provider_response.json"
    execution = verify_execution(minute_response, output, train_rl=train_rl)
    if source_hashes != {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}:
        raise ValueError("Qlib Parquet adapter source changed during acceptance")
    report = {"passed": True, "snapshot": asdict(snapshot), "real_daily_rows": len(frame),
              "primitive_fields_exact": 7, "calendar_instrument_expression_exact": True,
              "alpha158_alpha360_raw_processed_exact": True, "native_ridge_predictions_reload_exact": True,
              "dataset_cache_hits": cache_hits, "expression_cache_hits": expression.cache.hits,
              "execution": execution, "providers_modified": False, "real_orders_enabled": False,
              "canonical_bin_export_exact": True, "source_hashes": source_hashes,
              "limitations": ["Observed spans are not historical constituent memberships",
                              "No future exchange calendar is fabricated",
                              "Unknown historical availability and unscaled VWAP remain unavailable",
                              "Qlib storage mutations must publish through the snapshot coordinator"]}
    write_report(output / "report.json", report)
    return report


def main() -> None:
    """Run isolated differential acceptance without changing providers or the shared environment."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--response", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--train-rl", action="store_true")
    args = parser.parse_args()
    run(args.response, args.output, args.repo, train_rl=args.train_rl)


if __name__ == "__main__":
    main()
