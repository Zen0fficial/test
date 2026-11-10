"""Native Qlib cross-sectional signal analysis with independent source labels and metric checks.

Saved scores from the retrospective twenty-stock basket are evaluated without
refitting, promoting models or generating orders. Quantile return diagnostics
are algebraic label summaries, not a funded portfolio or achievable HK trades.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any
import warnings

import numpy as np
import pandas as pd

from .qlib_model_extensions import dataset_config
from .qlib_model_inventory import verified_extensions
from .qlib_parquet_lineage import verify as verify_sources
from .session import write_report
from .storage import SnapshotStore


def source_labels(store: SnapshotStore, snapshot: str) -> pd.DataFrame:
    """Derive next-observed-session returns from exact original closes without using training labels."""
    lineage = verify_sources(store, snapshot)
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in lineage.rows:
        if row["timeframe"] != "1d":
            raise ValueError("Panel signal analysis requires original daily source bars")
        groups.setdefault(row["symbol"].replace(".", "_"), []).append(row)
    records = []
    for symbol, rows in sorted(groups.items()):
        for ordinal, row in enumerate(rows):
            following = rows[ordinal + 1] if ordinal + 1 < len(rows) else None
            records.append({"datetime": pd.Timestamp(row["trading_date"]), "instrument": symbol,
                            "close": float(row["close"]),
                            "label": float(following["close"] / row["close"] - 1) if following else np.nan,
                            "label_end": pd.Timestamp(following["trading_date"]) if following else pd.NaT})
    result = pd.DataFrame(records).set_index(["datetime", "instrument"]).sort_index()
    if not result.index.is_unique:
        raise ValueError("Original source dates are not unique within each instrument")
    return result


def average_ranks(values: np.ndarray) -> np.ndarray:
    """Calculate one-based average ranks directly, retaining exact ties without pandas or scipy ranking."""
    order = sorted(range(len(values)), key=lambda index: (values[index], index))
    result, position = np.empty(len(values), dtype=float), 0
    while position < len(order):
        end = position + 1
        while end < len(order) and values[order[end]] == values[order[position]]:
            end += 1
        result[order[position:end]] = (position + 1 + end) / 2
        position = end
    return result


def correlation(left: np.ndarray, right: np.ndarray) -> float:
    """Use centered dot products; constant inputs are explicitly undefined, never zero or perfect IC."""
    if len(left) < 2 or np.ptp(left) == 0 or np.ptp(right) == 0:
        return np.nan
    first, second = left - left.mean(), right - right.mean()
    return float(np.dot(first, second) / np.sqrt(np.dot(first, first) * np.dot(second, second)))


def aligned(prediction: pd.Series, labels: pd.DataFrame, split: dict[str, tuple[str, str]],
            instruments: list[str]) -> pd.DataFrame:
    """Require exact panel coverage, finite scores/labels, future target dates and purged fit boundaries."""
    if (not isinstance(prediction.index, pd.MultiIndex)
            or prediction.index.names != ["datetime", "instrument"] or not prediction.index.is_unique
            or not prediction.index.is_monotonic_increasing):
        raise ValueError("Predictions require unique ordered datetime/instrument identities")
    dates = labels.index.get_level_values("datetime").unique().sort_values()
    segments = {key: tuple(pd.Timestamp(value) for value in bounds) for key, bounds in split.items()}
    train, validation, test = segments["train"], segments["valid"], segments["test"]
    if not train[0] <= train[1] < validation[0] <= validation[1] < test[0] <= test[1]:
        raise ValueError("Training, validation and prediction windows overlap or are unordered")
    for end, following in ((train[1], validation[0]), (validation[1], test[0])):
        boundary = labels.xs(end, level="datetime")["label_end"]
        if boundary.isna().any() or (boundary >= following).any():
            raise ValueError("A fitted/selection label reaches the following evaluation window")
    evaluation_dates = dates[(dates >= test[0]) & (dates <= test[1])]
    expected = pd.MultiIndex.from_product([evaluation_dates, sorted(instruments)],
                                          names=["datetime", "instrument"])
    if not len(expected) or not prediction.index.equals(expected):
        raise ValueError("Prediction coverage differs from the declared complete held-out stock panel")
    frame = labels.reindex(expected).copy()
    frame["score"] = prediction
    feature_dates = frame.index.get_level_values("datetime")
    if (not np.isfinite(frame[["score", "label"]].to_numpy()).all() or frame.label_end.isna().any()
            or not (frame.label_end.to_numpy() > feature_dates.to_numpy()).all()):
        raise ValueError("Scores/labels are nonfinite, missing or temporally misaligned")
    return frame


def analyze(prediction: pd.Series, labels: pd.DataFrame, split: dict[str, tuple[str, str]],
            instruments: list[str], quantile: float = .2) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    """Cross-check native Pearson/Spearman IC and quantile diagnostics against independent arithmetic."""
    from qlib.contrib.eva.alpha import calc_ic, calc_long_short_prec, calc_long_short_return

    if (not 0 < quantile < .5 or int(len(instruments) * quantile) < 1
            or int(1 / quantile) >= len(instruments)):
        raise ValueError("Choose a nonempty quantile smaller than one half of the fixed panel")
    frame = aligned(prediction, labels, split, instruments)
    records, ranks = [], []
    for date, group in frame.groupby(level="datetime", sort=True):
        score, target = group.score.to_numpy(dtype=np.float64), group.label.to_numpy(dtype=np.float64)
        count, selected = len(group), int(len(group) * quantile)
        score_rank, target_rank = average_ranks(score), average_ranks(target)
        constant_score, constant_label = np.ptp(score) == 0, np.ptp(target) == 0
        upper = sorted(range(count), key=lambda index: (-score[index], index))[:selected]
        lower = sorted(range(count), key=lambda index: (score[index], index))[:selected]
        top, bottom = target[upper], target[lower]
        metrics = {"pearson_ic": correlation(score, target), "spearman_ic": correlation(score_rank, target_rank),
                   "long_precision": float(np.mean(top > 0)), "short_precision": float(np.mean(bottom < 0)),
                   "half_long_short_return": float((top.mean() - bottom.mean()) / 2),
                   "basket_mean_return": float(target.mean()), "top_mean_return": float(top.mean()),
                   "bottom_mean_return": float(bottom.mean()), "top_excess_return": float(top.mean() - target.mean())}
        if constant_score:
            for key in metrics:
                if key not in ("pearson_ic", "spearman_ic", "basket_mean_return"):
                    metrics[key] = np.nan
        records.append({"datetime": date, **metrics, "stock_count": count, "quantile_count": selected,
                        "score_ties": count - len(set(score)), "constant_score": constant_score,
                        "constant_label": constant_label,
                        "selection_overlap": len(set(upper) & set(lower)) if not constant_score else 0})
        ranks.append(pd.DataFrame({"score_rank": score_rank, "label_rank": target_rank,
            "top_selected": [not constant_score and index in upper for index in range(count)],
            "bottom_selected": [not constant_score and index in lower for index in range(count)]}, index=group.index))
    independent = pd.DataFrame(records).set_index("datetime")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        ic, rank_ic = calc_ic(frame.score, frame.label, dropna=False)
    eligible = frame.loc[~frame.index.get_level_values("datetime").isin(
        independent.index[independent.constant_score])]
    native = pd.DataFrame({"pearson_ic": ic, "spearman_ic": rank_ic})
    if len(eligible):
        long_precision, short_precision = calc_long_short_prec(eligible.score, eligible.label, quantile=quantile)
        spread, average = calc_long_short_return(eligible.score, eligible.label, quantile=quantile)
        native = native.join(pd.DataFrame({"long_precision": long_precision, "short_precision": short_precision,
                                           "half_long_short_return": spread, "basket_mean_return": average}))
    else:
        for name in ("long_precision", "short_precision", "half_long_short_return", "basket_mean_return"):
            native[name] = np.nan
    errors = {}
    for name in native.columns:
        actual = native[name].reindex(independent.index)
        expected = independent[name].copy()
        if name == "basket_mean_return":
            expected[independent.constant_score] = np.nan
        if not np.allclose(actual, expected, equal_nan=True, rtol=0, atol=1e-12):
            raise ValueError(f"Native Qlib {name} differs from independent per-date arithmetic")
        difference = (actual - expected).abs().dropna()
        errors[name] = float(difference.max()) if len(difference) else 0.
    summary = {"passed": True, "dates": len(independent), "rows": len(frame), "stocks": len(instruments),
               "quantile": quantile, "native_max_absolute_errors": errors,
               "undefined_ic_dates": int(independent.pearson_ic.isna().sum()),
               "constant_score_dates": int(independent.constant_score.sum()),
               "constant_label_dates": int(independent.constant_label.sum()),
               "selection_overlap_dates": int((independent.selection_overlap > 0).sum()),
               "means": {name: float(independent[name].mean()) if independent[name].notna().any() else None
                         for name in (*native.columns, "top_mean_return", "bottom_mean_return", "top_excess_return")}}
    return frame.join(pd.concat(ranks)), independent, summary


def run(output: Path, repo: Path) -> dict[str, Any]:
    """Analyze six saved verified native panel learners without refitting or changing model configurations."""
    import qlib.contrib.eva.alpha as native_alpha

    output, repo = output.resolve(), repo.resolve()
    output.mkdir(parents=True, exist_ok=False)
    model_root = repo / "output/joint/qlib_models_panel_final_20261110"
    model_report = model_root / "report.json"
    evidence = verified_extensions([model_report])
    report = json.loads(model_report.read_text())
    panel = repo / "output/joint/plan_completion_20261110/panel"
    store = SnapshotStore(panel / "store")
    snapshot = store.snapshot(report["snapshot_id"])
    if snapshot.sha256 != report["snapshot_sha256"]:
        raise ValueError("Saved model predictions reference a different canonical panel")
    labels = source_labels(store, snapshot.identity)
    instruments = sorted(labels.index.get_level_values("instrument").unique())
    paths = [Path(__file__), Path(native_alpha.__file__), model_report,
             repo / "requirements/joint-py312-windows.lock",
             *[Path(__file__).with_name(name) for name in
               ("qlib_parquet_lineage.py", "qlib_model_extensions.py", "qlib_features.py",
                "qlib_model_inventory.py")]]
    for name, model in report["models"].items():
        artifact = model_root / name / "model.pkl"
        if hashlib.sha256(artifact.read_bytes()).hexdigest() != model["model_sha256"]:
            raise ValueError("Saved native model artifact changed after its training acceptance")
        paths.extend([artifact, model_root / name / "predictions.parquet"])
    hashes = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
    results = {}
    sample = labels.xs(instruments[0], level="instrument")
    for name in sorted(evidence):
        prediction = pd.read_parquet(model_root / name / "predictions.parquet")["score"]
        model = report["models"][name]
        split = dataset_config(model["configuration"]["dataset_kind"], sample, instruments)["kwargs"]["segments"]
        rows, daily, checked = analyze(prediction, labels, split, instruments)
        target = output / name
        target.mkdir()
        rows.to_parquet(target / "aligned_scores_labels.parquet")
        daily.to_parquet(target / "daily_metrics.parquet")
        checked.update(mlflow_run_id=model["mlflow_run_id"], segments=split,
                       target="Raw next-observed-session close return from original exact-decimal source prices")
        write_report(target / "report.json", checked)
        results[name] = checked
    if hashes != {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}:
        raise ValueError("Source, accepted models or saved predictions changed during analysis")
    result = {"passed": True, "models": results, "model_count": len(results), "training_performed": False,
              "source_hashes": hashes, "sources_unchanged_during_verification": True,
              "snapshot_id": snapshot.identity, "snapshot_sha256": snapshot.sha256,
              "instruments": instruments, "source_rows": len(labels), "real_orders_enabled": False,
              "providers_modified": False,
              "scope": "Retrospective 20-stock signal ranking and cross-sectional label diagnostics only",
              "limitations": ["Manually selected current basket; historical constituents and availability unknown",
                              "No model promotion or hyperparameter selection from these held-out diagnostics",
                              "Close-to-close labels do not establish executable next-open returns",
                              "Quantile means/spreads are not a funded portfolio; no fees, lots or fills are claimed",
                              "Equal-score ties follow canonical instrument order; quantile overlap is reported",
                              "Constant-score dates have undefined selection diagnostics, never arbitrary trades"]}
    write_report(output / "report.json", result)
    return result


def main() -> None:
    """Generate current-source-bound native and independently verified panel analytics in Parquet."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    args = parser.parse_args()
    run(args.output, args.repo)


if __name__ == "__main__":
    main()
