"""Qlib model comparison and costed artificial trading through the joint layer."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import warnings
from decimal import Decimal
from importlib.metadata import version
from pathlib import Path

import numpy as np
import pandas as pd

from futu_bridge.ml import baseline
from futu_bridge.qlib_export import export_responses

from .costs import SOURCES
from .session import read_report, write_report
from .simulation import SessionSignal, simulate
from .tracking import EXPERIMENT, prepare_tracking, verify_tracking

EXTRA = {
    "return_10": "$close/Ref($close,10)-1",
    "close_to_mean_10": "$close/Mean($close,10)-1",
    "range_position_20": "($close-Min($low,20))/(Max($high,20)-Min($low,20)+1e-12)",
    "volume_trend_5_20": "Mean($volume,5)/Mean($volume,20)-1",
}


def qlib_features(response: dict[str, object], frame: pd.DataFrame, root: Path, source: Path,
                  parquet: tuple[Path, str] | None = None) -> pd.DataFrame:
    """Build real Qlib daily storage/expressions and cross-check a pandas reference."""
    import qlib
    from qlib.config import REG_US
    from qlib.data import D

    if parquet is not None:
        from .qlib_parquet import initialize

        initialize(*parquet, cache=root / "parquet_cache", exp_manager=prepare_tracking(root))
    else:
        export_responses([("HK.00700", response)], root / "qlib_csv", timeframe="1d")
        with (root / "qlib_dump.log").open("w", encoding="utf-8") as log:
            subprocess.run([sys.executable, str(source / "scripts/dump_bin.py"), "dump_all",
                            "--data_path", str(root / "qlib_csv"), "--qlib_dir", str(root / "qlib_bin"),
                            "--freq", "day", "--max_workers", "1", "--include_fields",
                            "open,high,low,close,volume,factor"],
                           stdout=log, stderr=subprocess.STDOUT, check=True, timeout=180)
        qlib.init(provider_uri=str(root / "qlib_bin"), region=REG_US, expression_cache=None, dataset_cache=None,
                  kernels=1, exp_manager=prepare_tracking(root))
    extra = D.features(["HK_00700"], list(EXTRA.values()), freq="day")
    extra.index = extra.index.get_level_values("datetime")
    extra.columns = list(EXTRA)
    extra = extra.reindex(frame.index)
    reference = pd.DataFrame({
        "return_10": frame.close.pct_change(10, fill_method=None),
        "close_to_mean_10": frame.close / frame.close.rolling(10).mean() - 1,
        "range_position_20": (frame.close - frame.low.rolling(20).min()) /
                             (frame.high.rolling(20).max() - frame.low.rolling(20).min() + 1e-12),
        "volume_trend_5_20": frame.volume.rolling(5).mean() / frame.volume.rolling(20).mean() - 1,
    })
    # Qlib's early-window rolling convention differs: enforce complete warm-up.
    extra = extra.where(reference.notna())
    error = float((extra - reference).abs().max().max())
    if not np.allclose(extra, reference, atol=1e-4, rtol=0, equal_nan=True):
        raise ValueError("Qlib daily expressions disagree with source-derived reference")
    write_report(root / "qlib_feature_verification.json", {
        "passed": True, "max_absolute_error": error, "tolerance": 1e-4, "expressions": EXTRA,
    })
    return pd.concat([baseline.features(frame), extra], axis=1)


def signals(frame: pd.DataFrame, probabilities: pd.Series) -> list[SessionSignal]:
    """Map each after-close signal to the following observed session, never the same close."""
    result: list[SessionSignal] = []
    for date, probability in probabilities.items():
        index = frame.index.get_loc(date)
        if index + 1 >= len(frame):
            continue
        execution = frame.iloc[index + 1]
        result.append(SessionSignal(str(date.date()), str(frame.index[index + 1].date()), float(probability),
                                    Decimal(str(execution.open)), Decimal(str(execution.close))))
    return result


def lgb_candidate(data: pd.DataFrame, parts: dict[str, pd.DataFrame], leaves: int, root: Path) -> pd.Series:
    """Fit Qlib's actual LGBModel using native DatasetH train/validation segments."""
    from qlib.contrib.model.gbdt import LGBModel
    from qlib.data.dataset import DatasetH
    from qlib.data.dataset.handler import DataHandlerLP
    from qlib.workflow import R

    columns = [*baseline.FEATURES, *EXTRA]
    native = pd.concat({"feature": data[columns], "label": data[["label"]]}, axis=1)
    native.index = pd.MultiIndex.from_arrays([native.index, ["HK_00700"] * len(native)],
                                           names=["datetime", "instrument"])
    dataset = DatasetH(handler=DataHandlerLP.from_df(native), segments={
        "train": (parts["train"].index.min(), parts["train"].index.max()),
        "valid": (parts["validation"].index.min(), parts["validation"].index.max()),
        "all": (data.index.min(), data.index.max()),
    })
    model = LGBModel(loss="binary", num_boost_round=250, early_stopping_rounds=25, num_leaves=leaves,
                     min_data_in_leaf=80, lambda_l2=10, learning_rate=.03, seed=7, num_threads=2,
                     deterministic=True, force_col_wise=True)
    model_file = root / f"qlib_lgb_{leaves}.txt"
    with R.start(experiment_name=EXPERIMENT, recorder_name=f"qlib_lgb_{leaves}"):
        R.log_params(num_leaves=leaves, target="next_close_direction", real_orders_enabled=False)
        model.fit(dataset, verbose_eval=0)
        model.model.save_model(str(model_file))
        R.get_recorder().log_artifact(str(model_file))
    prediction = model.predict(dataset, segment="all")
    prediction.index = prediction.index.get_level_values("datetime")
    return prediction


def predict_saved(root: Path, features: pd.DataFrame) -> pd.Series:
    """Reload the selected frozen model and prior blend for research inference.

    Call with the causal feature columns saved in features.csv. This does not
    refit, fetch data, produce orders, or change the original evaluation split.
    """
    descriptor = read_report(root / "selected_model.json")
    if descriptor.get("schema_version") != 1 or descriptor.get("real_orders_enabled") is not False:
        raise ValueError("Unsupported research model descriptor")
    values = features[descriptor["features"]].to_numpy(dtype=float)
    if not np.isfinite(values).all():
        raise ValueError("Inference requires finite completed-day features")
    if not 0 <= descriptor["model_weight"] <= 1 or not 0 <= descriptor["training_prior"] <= 1:
        raise ValueError("Invalid model blend")
    family = descriptor["family"]
    if family in ("baseline", "baseline_shrunk"):
        artifact = read_report(root / "baseline_model.json")
        probability = np.array([baseline.predict_portable(artifact, row) for _, row in features.iterrows()])
    elif family == "extended_logistic":
        artifact = read_report(root / "extended_logistic.json")
        values = features[artifact["features"]].to_numpy(dtype=float)
        scores = ((values - artifact["mean"]) / artifact["scale"]) @ np.array(artifact["coefficients"])
        scores += artifact["intercept"]
        probability = np.exp(-np.logaddexp(0, -scores))
    elif family in ("qlib_lgb_7", "qlib_lgb_15"):
        from lightgbm import Booster

        model = Booster(model_file=str(root / f"{family}.txt"))
        probability = model.predict(features[descriptor["features"]])
    else:
        raise ValueError(f"Unknown selected family: {family}")
    weight = descriptor["model_weight"]
    result = weight * probability + (1 - weight) * descriptor["training_prior"]
    if not np.isfinite(result).all() or np.any((result < 0) | (result > 1)):
        raise ValueError("Saved model produced invalid probabilities")
    return pd.Series(result, index=features.index)


def paired_log_loss_interval(labels: pd.Series, selected: pd.Series, reference: pd.Series) -> dict[str, object]:
    """Descriptive paired circular-block bootstrap; never used for model selection.

    Twenty-session blocks retain short-term dependence. This is not a corrected
    significance test after model search or evidence of an untouched holdout.
    """
    y = labels.to_numpy(dtype=float)
    a, b = (np.clip(p.to_numpy(dtype=float), 1e-12, 1 - 1e-12) for p in (selected, reference))
    differences = -(y * np.log(a) + (1 - y) * np.log1p(-a)) + y * np.log(b) + (1 - y) * np.log1p(-b)
    count, block, repetitions = len(y), 20, 2000
    if not count:
        raise ValueError("Bootstrap requires observations")
    rng = np.random.default_rng(7)
    starts = rng.integers(0, count, size=(repetitions, (count + block - 1) // block))
    indices = (starts[:, :, None] + np.arange(block)) % count
    means = differences[indices.reshape(repetitions, -1)[:, :count]].mean(axis=1)
    return {"selected_minus_reference": float(differences.mean()),
            "descriptive_95_percent_interval": np.quantile(means, [.025, .975]).tolist(),
            "block_sessions": block, "repetitions": repetitions, "seed": 7,
            "limitation": "Retrospective, uncorrected for model search; not a significance claim"}


def run(response_path: Path, baseline_dir: Path, output: Path, qlib_source: Path,
        *, parquet: tuple[Path, str] | None = None, storage: str = "parquet") -> dict[str, object]:
    """Lock candidate rules, fit on training data, select on validation, and report all results.

    The original 333-day test is reused for comparison and explicitly labeled
    retrospective. Two newly observed sessions cannot establish future skill.
    """
    from sklearn.linear_model import LogisticRegression
    from sklearn.exceptions import ConvergenceWarning
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    root = output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    raw = response_path.read_bytes()
    (root / "provider_response.json").write_bytes(raw)
    response = json.loads(raw)
    if storage not in ("parquet", "legacy_bin"):
        raise ValueError("Storage must be parquet or legacy_bin")
    if parquet is None and storage == "parquet":
        from .storage import SnapshotStore
        from .storage.captures import ingest

        canonical_store = SnapshotStore(root / "store")
        canonical_snapshot = ingest(canonical_store, response_path, "hk00700_daily", "futu-bars")
        parquet = canonical_store.root, canonical_snapshot.identity
    lineage: dict[str, object] = {"analytical_source": "legacy_bin"}
    if parquet is not None:
        from .storage import SnapshotStore
        from .qlib_parquet_lineage import verify as verify_sources

        store = SnapshotStore(parquet[0])
        snapshot = store.snapshot(parquet[1])
        if snapshot.metadata.get("capture_sha256") != hashlib.sha256(raw).hexdigest():
            raise ValueError("Parquet snapshot and original daily archive anchor identities differ")
        originals = verify_sources(store, snapshot.identity)
        selected_bars = originals.bars()
        response = {**response, "data": {**response["data"], "bars": selected_bars, "count": len(selected_bars)}}
    last = response["data"]["bars"][-1]["date"]
    frame = baseline.validate_response(response, "2020-01-01", last)
    if parquet is not None:
        stored = store.read(snapshot.identity).to_pandas()
        if set(stored.symbol) != {"HK.00700"} or set(stored.timeframe) != {"1d"}:
            raise ValueError("Model comparison requires the daily HK.00700 snapshot")
        derived = stored[["open", "high", "low", "close", "volume"]].astype(float)
        derived.index = pd.DatetimeIndex(pd.to_datetime(stored.trading_date), name=frame.index.name)
        if not derived.equals(frame[derived.columns]):
            raise ValueError("Parquet daily values differ from the immutable source")
        frame.loc[:, derived.columns] = derived
        lineage = {"analytical_source": "parquet", "snapshot": snapshot.identity, "sha256": snapshot.sha256,
                   "snapshot_root": str(store.root), "capture_lineage": originals.evidence()}
    old_raw = (baseline_dir / "provider_response.json").read_bytes()
    old = baseline.validate_response(json.loads(old_raw), "2020-01-01", "2026-11-04")
    if not frame.loc[old.index].equals(old):
        raise ValueError("Provider revised baseline history; compare revisions before training")
    old_parts = baseline.chronological_split(baseline.samples(old))
    plan = {
        "target": "next observed close higher than current close", "selection": "validation log loss only",
        "candidate_families": ["saved baseline", "extended logistic C=0.1", "Qlib LGB leaves=7", "Qlib LGB leaves=15"],
        "lgb_parameters": {"max_rounds": 250, "early_stop_rounds": 25, "min_data_in_leaf": 80,
                           "lambda_l2": 10, "learning_rate": .03, "seed": 7, "num_threads": 2},
        "prior_blend_weights": [.25, .5, 1.0], "thresholds_reported_not_optimized": [.5, .55],
        "primary_threshold": .5,
        "capital_hkd": 100000, "lot_size": 100, "fees": "IBKR Tier I, schedule observed 2026-11-09",
        "slippage_bps_per_side": [0, 5, 10], "primary_slippage_bps": 5,
        "simulation": "prior-day signal -> next open entry -> same-session close exit; flat overnight",
        "splits": {name: {"start": str(p.index.min().date()), "end": str(p.index.max().date()), "n": len(p)}
                   for name, p in old_parts.items()},
        "test_status": "previously examined historical period, not a new untouched test",
        "fee_sources": SOURCES,
    }
    write_report(root / "plan.json", plan)
    x = qlib_features(response, frame, root, qlib_source, parquet)
    data = x.join(baseline.samples(frame)[["label", "label_date"]]).dropna()
    parts = {name: data.loc[part.index] for name, part in old_parts.items()}
    train = parts["train"]
    prior = float(train.label.mean())
    artifact = read_report(baseline_dir / "evaluation_model.json")
    write_report(root / "baseline_model.json", artifact)
    frozen = pd.Series([baseline.predict_portable(artifact, row) for _, row in data.iterrows()], index=data.index)
    candidates = {"baseline": frozen}
    logistic = make_pipeline(StandardScaler(), LogisticRegression(C=.1, max_iter=2000, random_state=7))
    columns = [*baseline.FEATURES, *EXTRA]
    with warnings.catch_warnings():
        warnings.simplefilter("error", ConvergenceWarning)
        logistic.fit(train[columns], train.label.astype(int))
    scaler, classifier = logistic.steps[0][1], logistic.steps[1][1]
    write_report(root / "extended_logistic.json", {
        "features": columns, "mean": scaler.mean_.tolist(), "scale": scaler.scale_.tolist(),
        "coefficients": classifier.coef_[0].tolist(), "intercept": float(classifier.intercept_[0]),
    })
    families = {"extended_logistic": pd.Series(logistic.predict_proba(data[columns])[:, 1], index=data.index)}
    for leaves in (7, 15):
        families[f"qlib_lgb_{leaves}"] = lgb_candidate(data, parts, leaves, root)
    families["baseline_shrunk"] = frozen
    for name, probability in families.items():
        for weight in (.25, .5, 1.0):
            candidates[f"{name}_w{weight}"] = weight * probability + (1 - weight) * prior
    validation = {name: baseline.metrics(parts["validation"].label, p.loc[parts["validation"].index])
                  for name, p in candidates.items()}
    selected = min(validation, key=lambda name: (validation[name]["log_loss"], name))
    # Persist the decision before reading test scores or simulation outcomes.
    write_report(root / "selection.json", {"selected": selected, "validation": validation})
    family, weight = selected.rsplit("_w", 1) if selected != "baseline" else ("baseline", "1")
    write_report(root / "selected_model.json", {
        "schema_version": 1, "selected": selected, "family": family, "model_weight": float(weight),
        "training_prior": prior, "features": columns, "target": plan["target"],
        "training": plan["splits"]["train"], "selection": plan["splits"]["validation"],
        "refitted_on_test": False, "real_orders_enabled": False,
    })
    reloaded = predict_saved(root, data[columns])
    if not np.allclose(reloaded, candidates[selected], atol=1e-12, rtol=0):
        raise ValueError("Saved selected model does not reproduce predictions")
    x.to_csv(root / "features.csv", index_label="feature_date")
    predictions = pd.DataFrame(candidates)
    predictions["label"] = data.label
    predictions["label_date"] = data.label_date
    predictions["partition"] = "purged_boundary"
    predictions.loc[predictions.index > parts["test"].index.max(), "partition"] = "new_sessions"
    for name, part in parts.items():
        predictions.loc[part.index, "partition"] = name
    predictions.to_csv(root / "predictions.csv", index_label="feature_date")
    evaluations = {}
    for partition, part in parts.items():
        evaluations[partition] = {name: baseline.metrics(part.label, p.loc[part.index])
                                  for name, p in candidates.items()}
    fresh = data.loc[data.index > parts["test"].index.max()]
    evaluations["new_sessions"] = {name: baseline.metrics(fresh.label, p.loc[fresh.index])
                                    for name, p in candidates.items()} if len(fresh) else {}
    constant = pd.Series(prior, index=data.index)
    references = {partition: baseline.metrics(part.label, constant.loc[part.index])
                  for partition, part in {**parts, "new_sessions": fresh}.items() if len(part)}
    test = parts["test"]
    uncertainty = {name: paired_log_loss_interval(test.label, candidates[selected].loc[test.index], p.loc[test.index])
                   for name, p in {"baseline": frozen, "constant_training_prior": constant}.items()}
    simulations: dict[str, object] = {}
    primary = {"baseline": frozen, "selected": candidates[selected],
               "always_long_intraday": pd.Series(1.0, index=data.index),
               "cash": pd.Series(0.0, index=data.index)}
    for name, p in primary.items():
        stream = signals(frame, p.loc[test.index])
        for threshold in (.5, .55):
            for bps in (0, 5, 10):
                result = simulate(stream, threshold=threshold, slippage_bps=Decimal(bps))
                key = f"{name}_threshold{threshold}_slippage{bps}"
                write_report(root / f"simulation_{key}.json", result)
                simulations[key] = {k: v for k, v in result.items() if k not in ("ledger", "equity")}
        frictionless = simulate(stream, threshold=.5, slippage_bps=Decimal(0), charge_fees=False)
        write_report(root / f"simulation_{name}_frictionless.json", frictionless)
        simulations[f"{name}_frictionless"] = {k: v for k, v in frictionless.items() if k not in ("ledger", "equity")}
    report = {
        "completed": True, "selected": selected, "evaluations": evaluations, "simulations": simulations,
        "raw_bars": len(frame), "new_labeled_sessions": len(fresh), "plan": plan,
        "response_sha256": hashlib.sha256(raw).hexdigest(),
        "baseline_response_sha256": hashlib.sha256(old_raw).hexdigest(), "baseline_history_unchanged": True,
        "providers_modified": False, "real_orders_enabled": False,
        "model_reload_verified": True,
        "dataset_lineage": lineage,
        "mlflow_verification": verify_tracking(root),
        "constant_training_prior_metrics": references, "test_log_loss_uncertainty": uncertainty,
        "versions": {"python": sys.version, **{name: version(name) for name in
                     ("pyqlib", "lightgbm", "scikit-learn", "numpy", "pandas", "mlflow")}},
        "code_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in
                        (Path(__file__), Path(__file__).with_name("costs.py"),
                         Path(__file__).with_name("simulation.py"), Path(baseline.__file__))},
        "limitations": ["Reused test period: retrospective comparison only",
                        "Current fees applied to every date; historical tariffs not reconstructed",
                        "100-share lot assumption; no fractional or odd-lot execution",
                        "Close-to-close forecast traded next open-to-close; overnight moves are not captured",
                        "Assumed fills; slippage is a scenario, not measured historical execution",
                        "Flat overnight; no dividends, financing, FX conversions, or cash interest",
                        "Assumes broker permits reuse of unsettled proceeds; settlement dates not modeled",
                        "Daily closing-equity drawdown does not measure intraday drawdown"],
    }
    if parquet is not None:
        from mlflow.tracking import MlflowClient
        from .tracking import tracking_uri

        client = MlflowClient(tracking_uri=tracking_uri(root))
        experiment = client.get_experiment_by_name(EXPERIMENT)
        for run_info in client.search_runs([experiment.experiment_id]):
            for key, value in lineage.items():
                client.set_tag(run_info.info.run_id, "joint.dataset." + key, str(value))
            lock_path = Path(__file__).resolve().parents[2] / "requirements/joint-py312-windows.lock"
            client.set_tag(run_info.info.run_id, "joint.runtime_lock_sha256",
                           hashlib.sha256(lock_path.read_bytes()).hexdigest())
            client.set_tag(run_info.info.run_id, "joint.comparison_code_sha256",
                           hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
        predictions.to_parquet(root / "predictions.parquet")
        x.to_parquet(root / "features.parquet")
    from .comparison_verify import verify as independent_verify

    report["independent_verification"] = independent_verify(root, report)
    write_report(root / "report.json", report)
    return report


def main() -> None:
    """Run only in the isolated Qlib/ML environment, with explicit input snapshots."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--response", type=Path, required=True)
    parser.add_argument("--baseline-dir", type=Path, required=True)
    parser.add_argument("--storage", choices=("parquet", "legacy_bin"), default="parquet")
    parser.add_argument("--store", type=Path)
    parser.add_argument("--snapshot")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--qlib-source", type=Path, required=True)
    args = parser.parse_args()
    if (args.store is None) != (args.snapshot is None) or args.store is not None and args.storage != "parquet":
        parser.error("An existing canonical input requires both --store and --snapshot with parquet storage")
    selected = None if args.store is None else (args.store, args.snapshot)
    report = run(args.response, args.baseline_dir, args.output, args.qlib_source,
                 storage=args.storage, parquet=selected)
    print(json.dumps({"completed": report["completed"], "selected": report["selected"]}))


if __name__ == "__main__":
    main()
