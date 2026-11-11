"""Compatibility entrypoint for Qlib tasks backed by local Parquet histories.

The former MongoDB acceptance implementation is retired. Native Qlib training
runs through the joint task coordinator, using SQLite for transactional ownership
and Parquet for immutable history. No MongoDB service or client is constructed.
Legacy pure data helpers remain available for existing local contract tests.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from .qlib_features import load_daily, segments


def validate_uri(uri: str) -> None:
    """Reject every legacy MongoDB URI before data access or task creation."""
    raise ValueError("MongoDB task backends are retired; use the local Parquet task workflow")



def dataset(frame: pd.DataFrame, root: Path) -> dict[str, object]:
    """Build causal real daily features for small native local regression tasks."""
    features = pd.DataFrame({"return_1": frame.close.pct_change(),
                             "close_mean_5": frame.close / frame.close.rolling(5).mean() - 1,
                             "volume_mean_5": frame.volume / frame.volume.rolling(5).mean() - 1})
    labels = (frame.close.shift(-1) / frame.close - 1).rename("LABEL0").to_frame()
    values = pd.concat({"feature": features, "label": labels}, axis=1).dropna()
    values.index = pd.MultiIndex.from_arrays([values.index, ["HK_00700"] * len(values)],
                                            names=["datetime", "instrument"])
    path = root / "task_dataset.parquet"
    values.to_parquet(path)
    return {"class": "DatasetH", "module_path": "qlib.data.dataset", "kwargs": {
        "handler": {"class": "DataHandlerLP", "module_path": "qlib.data.dataset.handler", "kwargs": {
            "data_loader": {"class": "StaticDataLoader", "module_path": "qlib.data.dataset.loader",
                            "kwargs": {"config": str(path)}}}}, "segments": segments(frame)}}


def definitions(data_config: dict[str, object], root: Path, source_hash: str) -> list[dict[str, object]]:
    """Produce six distinct serializable tasks with a common immutable real-data source."""
    return [{"model": {"class": "LinearModel", "module_path": "qlib.contrib.model.linear",
                        "kwargs": {"estimator": "ridge", "alpha": alpha, "fit_intercept": True}},
             "dataset": deepcopy(data_config),
             "record": [{"class": "SignalRecord", "module_path": "qlib.workflow.record_temp"}],
             "joint_task": {"key": f"ridge_{index}", "root": str(root), "source_sha256": source_hash}}
            for index, alpha in enumerate((.01, .1, 1., 10., 100., 1000.))]


def first_stage(task: dict[str, object]) -> dict[str, object]:
    """Return typed real-market data to test native partial-result serialization."""
    raw = Path(task["source"]).read_bytes()
    if hashlib.sha256(raw).hexdigest() != task["sha256"]:
        raise ValueError("Staged source bytes changed")
    frame = load_daily(Path(task["source"]))
    return {"prices": frame.close.tail(5).to_numpy(), "span": (frame.index[-5], frame.index[-1]),
            "source_sha256": task["sha256"]}


def second_stage(value: dict[str, object]) -> dict[str, object]:
    """Require native pickle roundtrip to preserve arrays/tuples, then finalize actual source statistics."""
    if not isinstance(value["prices"], np.ndarray) or not isinstance(value["span"], tuple):
        raise ValueError("Native partial-result serialization lost its Python types")
    return {"mean_close": float(value["prices"].mean()), "count": len(value["prices"]),
            "source_sha256": value["source_sha256"], "array_type": type(value["prices"]).__name__,
            "span_type": type(value["span"]).__name__}

def run(response: Path, output: Path, repo: Path, *, mongo_uri: str | None = None) -> dict[str, object]:
    """Forward the legacy module entrypoint to the verified local replacement."""
    if mongo_uri is not None:
        validate_uri(mongo_uri)
    from .workflows.acceptance import run as acceptance

    return acceptance(response, output, repo)


def main() -> None:
    """Run local native Qlib task acceptance without a database-service option."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--response", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    args = parser.parse_args()
    print(json.dumps(run(args.response, args.output, args.repo), indent=2))


if __name__ == "__main__":
    main()
