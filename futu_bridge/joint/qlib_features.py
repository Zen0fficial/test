"""Native Qlib feature, model, workflow and portfolio coverage using fixed daily data.

Missing VWAP is retained as missing in the native feature schema and explicitly
imputed for model experiments. Unscaled raw turnover is never guessed into VWAP.
This is an offline research/conformance workflow, not evidence of predictive skill.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import importlib.util
from pathlib import Path
import shutil
import socket
import struct
from time import perf_counter

import numpy as np
import pandas as pd

from futu_bridge.ml.baseline import validate_response
from .session import write_report
from .tracking import prepare_tracking

SYMBOL = "HK_00700"


def load_daily(response: Path) -> pd.DataFrame:
    """Validate the unchanged raw daily provider contract before any conversion."""
    value = json.loads(response.read_text(encoding="utf-8"))
    if value.get("data", {}).get("timeframe") != "1d":
        raise ValueError("Native feature coverage requires a raw DAILY HK.00700 capture")
    frame = validate_response(value, value["data"]["bars"][0]["date"], value["data"]["bars"][-1]["date"])
    if len(frame) < 600:
        raise ValueError("At least 600 real daily observations are required")
    return frame


def storage(frame: pd.DataFrame, root: Path) -> None:
    """Write converter-owned native float32 storage without changing the original exporter."""
    calendars, instruments = root / "calendars", root / "instruments"
    folder = root / "features" / SYMBOL.lower()
    for path in (calendars, instruments, folder):
        path.mkdir(parents=True)
    calendar = "\n".join(str(t.date()) for t in frame.index) + "\n"
    (calendars / "day.txt").write_text(calendar, encoding="utf-8")
    # RollingGen only selects observed dates. The final sentinel permits calendar indexing.
    (calendars / "day_future.txt").write_text(
        calendar + str((frame.index[-1] + pd.Timedelta(days=1)).date()) + "\n", encoding="utf-8")
    (instruments / "all.txt").write_text(
        f"{SYMBOL}\t{frame.index[0].date()}\t{frame.index[-1].date()}\n", encoding="utf-8")
    for field in ("open", "high", "low", "close", "volume", "factor", "vwap"):
        values = (np.ones(len(frame)) if field == "factor" else np.full(len(frame), np.nan)
                  if field == "vwap" else frame[field].to_numpy())
        (folder / f"{field}.day.bin").write_bytes(struct.pack("<f", 0) + np.asarray(values, dtype="<f4").tobytes())


def segments(frame: pd.DataFrame) -> dict[str, tuple[str, str]]:
    """Reserve warm-up and purge one forward-return label at each chronological boundary."""
    dates = frame.index
    a, b = int(len(dates) * .6), int(len(dates) * .8)
    return {"train": (str(dates[60]), str(dates[a - 2])), "valid": (str(dates[a]), str(dates[b - 2])),
            "test": (str(dates[b]), str(dates[-2]))}


def handler_config(name: str, frame: pd.DataFrame, split: dict[str, tuple[str, str]]) -> dict[str, object]:
    """Use native schema and processors, with train-only fitting and an explicit causal label."""
    if name not in ("Alpha158", "Alpha360"):
        raise ValueError("Unsupported native alpha handler")
    return {"class": name, "module_path": "qlib.contrib.data.handler", "kwargs": {
        "instruments": [SYMBOL], "start_time": str(frame.index[0]), "end_time": str(frame.index[-1]),
        "fit_start_time": split["train"][0], "fit_end_time": split["train"][1],
        "label": (["Ref($close,-1)/$close-1"], ["LABEL0"]),
        "infer_processors": [{"class": "FiniteByDate", "module_path": "futu_bridge.joint.qlib_features_models"},
                             {"class": "RobustZScoreNorm", "kwargs": {"fields_group": "feature"}},
                             {"class": "Fillna", "kwargs": {"fields_group": "feature", "fill_value": 0}}],
        "learn_processors": [{"class": "DropnaLabel"}]}}


def verify_features(name: str, handler: object, frame: pd.DataFrame,
                    split: dict[str, tuple[str, str]]) -> dict[str, object]:
    """Compare independent feature formulas and train-only normalization against native outputs."""
    raw = handler.fetch(data_key="raw", col_set=["feature", "label"])
    processed = handler.fetch(data_key="infer", col_set=["feature", "label"])
    values = raw["feature"].droplevel("instrument").reindex(frame.index)
    close = frame.close
    expected = {"OPEN0": frame.open / close, "HIGH0": frame.high / close, "LOW0": frame.low / close}
    if name == "Alpha360":
        expected = {f"{field.upper()}{lag}": frame[field].shift(lag) / (
            frame.volume + 1e-12 if field == "volume" else close)
                    for field in ("open", "high", "low", "close", "volume") for lag in range(60)}
    else:
        expected.update(KMID=(close - frame.open) / frame.open,
                        KLEN=(frame.high - frame.low) / frame.open,
                        ROC5=close.shift(5) / close, MA5=close.rolling(5, min_periods=1).mean() / close)
    error = 0.
    for field, series in expected.items():
        actual = values[field]
        if not np.allclose(actual, series, atol=2e-5, rtol=2e-6, equal_nan=True):
            raise ValueError(f"Native {name} formula differs from independent source: {field}")
        error = max(error, float((actual - series).abs().max()))
    expected_label = close.shift(-1) / close - 1
    label = raw["label"].iloc[:, 0].droplevel("instrument").reindex(frame.index)
    if not np.allclose(label, expected_label, atol=2e-6, rtol=0, equal_nan=True):
        raise ValueError("Native forward-return labels differ from source")
    missing = [c for c in values if values[c].isna().all()]
    wanted_missing = [f"VWAP{i}" for i in range(60)] if name == "Alpha360" else ["VWAP0"]
    if set(missing) != set(wanted_missing):
        raise ValueError("Unexpected unavailable feature fields")
    train = values.loc[split["train"][0]:split["train"][1]].replace([np.inf, -np.inf], np.nan)
    center = train.median()
    scale = (train.sub(center).abs().median() + 1e-12) * 1.4826
    normalized = values.replace([np.inf, -np.inf], np.nan).sub(center).div(scale).clip(-3, 3).fillna(0)
    actual = processed["feature"].droplevel("instrument").reindex(frame.index)
    if not np.isfinite(actual.to_numpy()).all() or not np.allclose(actual, normalized, atol=1e-4, rtol=1e-4):
        raise ValueError("Native processor differs from train-only independent robust normalization")
    return {"passed": True, "feature_count": values.shape[1], "independently_checked_formulas": len(expected),
            "formula_max_absolute_error": error, "missing_provider_fields": ["vwap"],
            "explicitly_imputed_columns": missing, "normalization_fit_range": list(split["train"]),
            "labels_purged_at_boundaries": True}


def benchmark_operations(frame: pd.DataFrame, repeats: int = 7) -> dict[str, object]:
    """Measure equivalent warm expression calls; selection belongs to the serialized joint benchmark."""
    from qlib.data import D

    native_times, reference_times = [], []
    for _ in range(repeats):
        started = perf_counter()
        native = D.features([SYMBOL], ["$close/Mean($close,5)-1"], freq="day", disk_cache=0)
        native_times.append(perf_counter() - started)
        started = perf_counter()
        reference = frame.close / frame.close.rolling(5, min_periods=1).mean() - 1
        reference_times.append(perf_counter() - started)
        if not np.allclose(native.iloc[:, 0].to_numpy(), reference, atol=2e-6, rtol=0):
            raise ValueError("Benchmark implementations differ numerically")
    return {"operation": "five_day_close_to_mean", "rows": len(frame), "passed": True,
            "qlib_warm_seconds": native_times, "pandas_seconds": reference_times,
            "scope": "Warm in-process repeated expressions; includes native memory caches; no winner selected"}


def remaining_constraints() -> dict[str, object]:
    """Inspect concrete local prerequisites rather than reporting unattempted families as covered."""
    services = {}
    for service, executable, port in (("redis", "redis-server", 6379),):
        with socket.socket() as probe:
            probe.settimeout(.2)
            listening = probe.connect_ex(("127.0.0.1", port)) == 0
        services[service] = {"executable_on_path": shutil.which(executable), "localhost_listening": listening}
    return {"local_task_workflows": {"status": "separate_acceptance_suite", "requires_mongodb": False,
                                     "coordinator": "SQLite", "immutable_history": "Parquet",
                                     "entrypoint": "futu_bridge.joint.workflows.acceptance",
                                     "separate_host_operation_verified": False},
            "Redis_expression_service": {"status": "service_unconfigured", **services["redis"]},
            "native_MetaModelDS": {"status": "insufficient_cross_section",
                "reason": "InternalData._calc_perf uses per-date Spearman IC; one stock yields all NaN",
                "training_MSE_mode_exists": True,
                "why_MSE_is_insufficient": "Changing the loss cannot supply the missing IC performance matrix"},
            "enhanced_indexing": {"status": "missing_inputs",
                "requires": ["multi-stock universe", "point-in-time factor exposures", "factor covariance",
                             "specific risk", "benchmark constituent weights"]},
            "native_interactive_graph_catalogue": {"status": "dependency_missing",
                "plotly_installed": importlib.util.find_spec("plotly") is not None,
                "numeric_native_reports_verified": True},
            "continuous_online_service": {"status": "not_started",
                "native_online_recorder_promotion_rollback_and_PredUpdater_verified": True}}


def run(response: Path, output: Path, repo: Path) -> dict[str, object]:
    """Run native feature/model/workflow/portfolio checks without changing providers or packages."""
    import qlib
    from qlib.config import REG_US
    from qlib.data import D, LocalDatasetProvider, SimpleDatasetCache
    from qlib.data.data import DatasetD
    from qlib.data.dataset import DatasetH
    from qlib.utils import init_instance_by_config, register_wrapper
    from .qlib_features_models import train_models, rolling_workflow
    from .qlib_features_portfolio import portfolios

    response, output = response.resolve(), output.resolve()
    frame = load_daily(response)
    output.mkdir(parents=True, exist_ok=False)
    raw = response.read_bytes()
    (output / "provider_response.json").write_bytes(raw)
    write_report(output / "status.json", {"state": "running"})
    try:
        storage(frame, output / "qlib_data")
        qlib.init(provider_uri=str(output / "qlib_data"), region=REG_US, kernels=1, logging_level=40,
                  expression_cache=None, dataset_cache=None, local_cache_path=str(output / "cache"),
                  exp_manager=prepare_tracking(output), trade_unit=100)
        # Avoid native Config.set probing Redis even for this Redis-free cache.
        # Register its unchanged native implementation through Qlib's wrapper hook.
        register_wrapper(DatasetD, SimpleDatasetCache(LocalDatasetProvider()), "qlib.data")
        split = segments(frame)
        handlers, datasets, features = {}, {}, {}
        for name in ("Alpha158", "Alpha360"):
            handler = init_instance_by_config(handler_config(name, frame, split))
            handlers[name], datasets[name] = handler, DatasetH(handler=handler, segments=split)
            features[name] = verify_features(name, handler, frame, split)
            handler.fetch(data_key="raw", col_set=["feature", "label"]).to_parquet(output / f"{name}_raw.parquet")
            handler.fetch(data_key="infer", col_set=["feature", "label"]).to_parquet(
                output / f"{name}_processed.parquet")
        cache_a = D.features([SYMBOL], ["$close", "$close/Ref($close,1)-1"], disk_cache=1)
        cached_files = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (output / "cache").iterdir()}
        cache_b = D.features([SYMBOL], ["$close", "$close/Ref($close,1)-1"], disk_cache=1)
        if not cached_files or not cache_a.equals(cache_b) or any(
                hashlib.sha256((output / "cache" / name).read_bytes()).hexdigest() != digest
                for name, digest in cached_files.items()):
            raise ValueError("Native local dataset-cache roundtrip failed")
        models = train_models(datasets, output)
        workflow = rolling_workflow(frame, output)
        portfolio = portfolios(frame, output, split)
        report = {"passed": True, "framework": "qlib", "version": qlib.__version__,
                  "source_sha256": hashlib.sha256(raw).hexdigest(), "real_sessions": len(frame),
                  "segments": split, "features": features, "models": models, "rolling_workflow": workflow,
                  "portfolio": portfolio, "cache": {"passed": True, "kind": "SimpleDatasetCache",
                                                       "files": len(cached_files), "requires_redis": False},
                  "benchmark": benchmark_operations(frame), "providers_modified": False,
                  "remaining_constraints": remaining_constraints(),
                  "real_orders_enabled": False, "full_feature_coverage": False,
                  "limitations": ["VWAP unavailable: raw turnover scale unverified; missing fields explicitly imputed",
                                  "Single-stock data cannot validate ranking, IC or cross-sectional risk models",
                                  "Smoke training does not establish financial skill or full catalogue coverage",
                                  "Local task recovery and meta-learning use separate acceptance suites",
                                  "Separate-host task operation and a Redis expression service are not verified",
                                  "Rolling retraining is historical; no continuous online service is launched"]}
        write_report(output / "report.json", report)
        write_report(output / "status.json", {"state": "completed"})
        return report
    except Exception:
        write_report(output / "status.json", {"state": "failed"})
        raise


def main() -> None:
    """Expose the native daily feature conformance worker."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--response", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    args = parser.parse_args()
    print(json.dumps(run(args.response, args.output, args.repo), indent=2))


if __name__ == "__main__":
    main()
