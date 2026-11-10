"""Durable single-host online research scheduling over immutable Parquet snapshots.

The scheduler reads committed heads, creates idempotent Qlib tasks and reconciles
local MLflow registry aliases. It never subscribes to or changes providers and
never constructs a trading client. Historical snapshots certify restart behavior,
not an unattended production service or point-in-time availability of old bars.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
from time import sleep, time_ns
from typing import Any, Iterator

import numpy as np
import pandas as pd

from .qlib_parquet import initialize, view
from .session import write_report
from .storage import SnapshotStore
from .tracking import tracking_uri
from .workflows import TaskManager, run_task

EXPERIMENT = "parquet-online-research"
MODEL_NAME = "hk00700-parquet-online"
POOL = "parquet-online"


def tracking(root: Path) -> dict[str, Any]:
    """Create or reuse only the scheduler's local MLflow experiment."""
    from mlflow.tracking import MlflowClient
    from mlflow.exceptions import MlflowException

    root.mkdir(parents=True, exist_ok=True)
    uri = tracking_uri(root)
    client = MlflowClient(tracking_uri=uri)
    if client.get_experiment_by_name(EXPERIMENT) is None:
        try:
            client.create_experiment(EXPERIMENT, artifact_location=(root.resolve() / "mlartifacts").as_uri())
        except MlflowException:
            if client.get_experiment_by_name(EXPERIMENT) is None:
                raise
    return {"class": "MLflowExpManager", "module_path": "qlib.workflow.expm",
            "kwargs": {"uri": uri, "default_exp_name": EXPERIMENT}}


def task_definition(store: Path, snapshot: str, root: Path, alpha: float = 1.) -> dict[str, Any]:
    """Use native RollingGen with bounded observed dates and explicit one-day label purges."""
    from qlib.workflow.task.gen import RollingGen

    if not np.isfinite(alpha) or alpha < 0:
        raise ValueError("Ridge alpha must be finite and nonnegative")
    source = view(str(store.resolve()), snapshot)
    if source.freq != "day" or len(source.calendar) < 300:
        raise ValueError("Online acceptance requires at least300 real daily observations")
    initialize(store, snapshot, cache=root / "cache", exp_manager=tracking(root))
    dates = source.calendar
    splits = {"train": (str(dates[60]), str(dates[-25])),
              "valid": (str(dates[-23]), str(dates[-4])), "test": (str(dates[-2]), str(dates[-2]))}
    fields = ["$close/Ref($close,1)-1", "$close/Mean($close,5)-1", "$volume/Mean($volume,5)-1"]
    handler = {"class": "DataHandlerLP", "module_path": "qlib.data.dataset.handler", "kwargs": {
        "instruments": ["HK_00700"], "start_time": str(dates[0]), "end_time": str(dates[-1]),
        "data_loader": {"class": "QlibDataLoader", "module_path": "qlib.data.dataset.loader", "kwargs": {
            "config": {"feature": (fields, ["RET1", "MA5", "VOL5"]),
                       "label": (["Ref($close,-1)/$close-1"], ["LABEL0"])}}},
        "infer_processors": [{"class": "Fillna", "kwargs": {"fields_group": "feature", "fill_value": 0}}],
        "learn_processors": [{"class": "DropnaLabel"}]}}
    template = {"model": {"class": "LinearModel", "module_path": "qlib.contrib.model.linear",
                           "kwargs": {"estimator": "ridge", "alpha": alpha, "fit_intercept": True}},
                "dataset": {"class": "DatasetH", "module_path": "qlib.data.dataset",
                            "kwargs": {"handler": handler, "segments": splits}},
                "record": [{"class": "SignalRecord", "module_path": "qlib.workflow.record_temp"}]}
    generator = RollingGen(step=1, rtype=RollingGen.ROLL_EX, trunc_days=1)
    historical = generator.generate(template)
    # Consume exactly the next observed fold. Upstream's unlimited iterator has
    # a None comparison at the final calendar index; no invented sentinel is added.
    task = next(generator.gen_following_tasks(historical[-1], dates[-1]))
    segments = task["dataset"]["kwargs"]["segments"]
    if not segments["train"][1] < segments["valid"][0] <= segments["valid"][1] < segments["test"][0]:
        raise ValueError("Native rolling task violated chronological isolation")
    model_config = {"model": task["model"], "features": fields, "generator": "RollingGen-expanding-one-observed-day",
                    "adapter_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    configuration_hash = hashlib.sha256(json.dumps(model_config, sort_keys=True).encode()).hexdigest()
    task["joint_online"] = {"root": str(root.resolve()), "store": str(store.resolve()),
                            "snapshot_id": snapshot, "snapshot_sha256": source.snapshot.sha256,
                            "configuration_hash": configuration_hash, "decision_date": str(dates[-1]),
                            "real_orders_enabled": False}
    return task


def poll(root: Path, store: Path, dataset: str, *, alpha: float = 1.) -> dict[str, Any]:
    """Create one snapshot/configuration-bound task, or return the same durable task identity."""
    root = root.resolve()
    snapshot = SnapshotStore(store).head(dataset)
    if snapshot is None:
        raise ValueError("The requested observed dataset has no committed snapshot")
    definition = task_definition(store, snapshot, root, alpha)
    manager = TaskManager(POOL, root / "tasks")
    identities = manager.create_task([definition])
    manager.reconcile()
    if (root / "online.sqlite").exists():
        Registry(root).reconcile()
    return {"task_id": identities[0], "snapshot_id": snapshot,
            "configuration_hash": definition["joint_online"]["configuration_hash"],
            "decision_date": definition["joint_online"]["decision_date"], "real_orders_enabled": False}


def train(task: dict[str, Any], experiment_name: str = EXPERIMENT) -> Any:
    """Fit the unchanged native model with a pinned snapshot and record validation-only evidence."""
    from qlib.model.trainer import task_train
    from qlib.utils import init_instance_by_config

    info = task["joint_online"]
    root, store = Path(info["root"]), Path(info["store"])
    if SnapshotStore(store).snapshot(info["snapshot_id"]).sha256 != info["snapshot_sha256"]:
        raise ValueError("Scheduled snapshot identity changed")
    initialize(store, info["snapshot_id"], cache=root / "cache", exp_manager=tracking(root))
    record = task_train(task, experiment_name=experiment_name, recorder_name="online_ridge")
    dataset = init_instance_by_config(task["dataset"])
    model = record.load_object("params.pkl")
    predictions = model.predict(dataset, segment="valid")
    labels = dataset.prepare("valid", col_set="label", data_key="learn").iloc[:, 0]
    if not predictions.index.equals(labels.index) or not np.isfinite(predictions).all():
        raise ValueError("Online model validation is nonfinite or misaligned")
    mse = float(np.mean((predictions - labels) ** 2))
    record.log_metrics(online_validation_mse=mse)
    record.set_tags(joint_snapshot_id=info["snapshot_id"], joint_snapshot_sha256=info["snapshot_sha256"],
                    joint_configuration_hash=info["configuration_hash"], joint_online_research=True)
    return record


def worker(root: Path) -> None:
    """Drain durable work using fenced renewable claims; no provider or broker connection is created."""
    run_task(train, POOL, root=root / "tasks", experiment_name=EXPERIMENT)


class Registry:
    """Validation-gated local model aliases with a replayable SQLite publication outbox."""

    def __init__(self, root: Path) -> None:
        """Keep online alias state separate from task and MLflow-owned schemas."""
        from mlflow.tracking import MlflowClient
        from mlflow.exceptions import MlflowException

        self.root = root.resolve()
        self.client = MlflowClient(tracking_uri=tracking_uri(self.root))
        self.manager = TaskManager(POOL, self.root / "tasks")
        try:
            self.client.create_registered_model(MODEL_NAME)
        except MlflowException:
            self.client.get_registered_model(MODEL_NAME)
        with self.connection() as database:
            database.executescript("""
                CREATE TABLE IF NOT EXISTS versions(task_id TEXT PRIMARY KEY, version TEXT NOT NULL UNIQUE);
                CREATE TABLE IF NOT EXISTS decisions(
                  id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, version TEXT NOT NULL,
                  kind TEXT NOT NULL, evidence TEXT NOT NULL, occurred_ns INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS desired(alias TEXT PRIMARY KEY, decision_id INTEGER NOT NULL,
                  version TEXT NOT NULL, delivered INTEGER NOT NULL DEFAULT 0);
            """)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """Coordinate short local transactions and deterministically close Windows handles."""
        database = sqlite3.connect(self.root / "online.sqlite", timeout=10, isolation_level=None)
        database.row_factory = sqlite3.Row
        try:
            database.execute("PRAGMA journal_mode=WAL")
            database.execute("PRAGMA synchronous=FULL")
            yield database
        finally:
            if database.in_transaction:
                database.rollback()
            database.close()

    def accepted(self, identity: str) -> dict[str, Any]:
        """Reject incomplete/unaccepted workers even if they left a successful MLflow artifact."""
        task = self.manager.re_query(identity)
        if task["status"] != "done" or task["res"].status != "FINISHED":
            raise ValueError("Only a fenced accepted finished result may enter the online registry")
        run = self.client.get_run(task["res"].id)
        info = task["def"]["joint_online"]
        lineage = {"joint_snapshot_id": info["snapshot_id"], "joint_snapshot_sha256": info["snapshot_sha256"],
                   "joint_configuration_hash": info["configuration_hash"]}
        if run.info.status != "FINISHED" or any(run.data.tags.get(key) != value for key, value in lineage.items()):
            raise ValueError("Native recorder lineage is missing or does not match its scheduled data")
        return task

    def register(self, identity: str) -> str:
        """Register accepted native Qlib artifacts once under a local database writer transaction."""
        task = self.accepted(identity)
        with self.connection() as database:
            database.execute("BEGIN IMMEDIATE")
            existing = database.execute("SELECT version FROM versions WHERE task_id=?", (identity,)).fetchone()
            if existing is not None:
                database.commit()
                return existing[0]
            # Reconcile an orphaned MLflow version after a crash before SQLite commit.
            matches = [item for item in self.client.search_model_versions(f"name='{MODEL_NAME}'")
                       if item.tags.get("joint_task_id") == identity]
            if len(matches) > 1:
                raise ValueError("Multiple registry versions claim the same accepted task")
            model = matches[0] if matches else self.client.create_model_version(
                MODEL_NAME, f"runs:/{task['res'].id}/params.pkl", run_id=task["res"].id,
                tags={"joint_task_id": identity, "joint_snapshot_id": task["def"]["joint_online"]["snapshot_id"]})
            version = str(model.version)
            database.execute("INSERT INTO versions VALUES(?,?)", (identity, version))
            database.commit()
        return version

    def evaluate(self, identity: str, reference: str) -> dict[str, Any]:
        """Score accepted models on the same explicit validation window with identical causal features."""
        from qlib.utils import init_instance_by_config

        candidate, evaluation = self.accepted(identity), self.accepted(reference)
        task = deepcopy(evaluation["def"])
        info = task["joint_online"]
        train_end = pd.Timestamp(candidate["def"]["dataset"]["kwargs"]["segments"]["train"][1])
        valid_start = pd.Timestamp(task["dataset"]["kwargs"]["segments"]["valid"][0])
        if train_end >= valid_start:
            raise ValueError("Validation window overlaps candidate training")
        original_loader = candidate["def"]["dataset"]["kwargs"]["handler"]["kwargs"]["data_loader"]
        current_loader = task["dataset"]["kwargs"]["handler"]["kwargs"]["data_loader"]
        if original_loader != current_loader:
            raise ValueError("Model comparison requires the same causal feature definition")
        initialize(Path(info["store"]), info["snapshot_id"], cache=self.root / "cache",
                   exp_manager=tracking(self.root))
        dataset = init_instance_by_config(task["dataset"])
        prediction = candidate["res"].load_object("params.pkl").predict(dataset, segment="valid")
        labels = dataset.prepare("valid", col_set="label", data_key="learn").iloc[:, 0]
        if len(labels) < 10 or not prediction.index.equals(labels.index) or not np.isfinite(prediction).all():
            raise ValueError("Insufficient aligned finite validation observations")
        return {"mse": float(np.mean((prediction - labels) ** 2)), "rows": len(labels),
                "validation_range": [str(value) for value in task["dataset"]["kwargs"]["segments"]["valid"]],
                "evaluation_snapshot": info["snapshot_id"], "evaluation_snapshot_sha256": info["snapshot_sha256"]}

    def decide(self, identity: str, reference: str, *, rollback: bool = False) -> dict[str, Any]:
        """Promote only a strictly better validated candidate; rollback requires a prior accepted version."""
        version = self.register(identity)
        with self.connection() as database:
            previous = database.execute("SELECT decision_id,version FROM desired WHERE alias='champion'").fetchone()
            previous_id = None if previous is None else previous["decision_id"]
            previous_task = None if previous is None else database.execute(
                "SELECT task_id FROM versions WHERE version=?", (previous["version"],)).fetchone()[0]
            known = database.execute("SELECT id FROM decisions WHERE task_id=? LIMIT 1",
                                     (identity,)).fetchone()
        evidence = self.evaluate(identity, reference)
        incumbent = None if previous_task is None else self.evaluate(previous_task, reference)
        if rollback and known is None:
            raise ValueError("Rollback requires an earlier accepted champion decision")
        approved = rollback or incumbent is None or evidence["mse"] < incumbent["mse"]
        result = {"approved": approved, "candidate": identity, "candidate_validation": evidence,
                  "incumbent": previous_task, "incumbent_validation": incumbent,
                  "kind": "rollback" if rollback else "validation_promotion", "real_orders_enabled": False}
        if not approved:
            return result
        with self.connection() as database:
            database.execute("BEGIN IMMEDIATE")
            current = database.execute("SELECT decision_id FROM desired WHERE alias='champion'").fetchone()
            if (None if current is None else current[0]) != previous_id:
                raise RuntimeError("Concurrent promotion changed the incumbent; reevaluate")
            cursor = database.execute("""INSERT INTO decisions(task_id,version,kind,evidence,occurred_ns)
                VALUES(?,?,?,?,?)""", (identity, version, result["kind"],
                                       json.dumps(result, allow_nan=False), time_ns()))
            database.execute("""INSERT INTO desired VALUES('champion',?,?,0)
                ON CONFLICT(alias) DO UPDATE SET decision_id=excluded.decision_id,
                version=excluded.version,delivered=0""", (cursor.lastrowid, version))
            database.commit()
        self.reconcile()
        return result

    def reconcile(self) -> None:
        """Replay the newest desired alias, including repair after interrupted external publication."""
        for _ in range(10):
            with self.connection() as database:
                row = database.execute("SELECT * FROM desired WHERE alias='champion'").fetchone()
            if row is None:
                return
            self.client.set_registered_model_alias(MODEL_NAME, "champion", row["version"])
            with self.connection() as database:
                database.execute("BEGIN IMMEDIATE")
                current = database.execute("SELECT decision_id FROM desired WHERE alias='champion'").fetchone()[0]
                if current == row["decision_id"]:
                    database.execute("UPDATE desired SET delivered=1 WHERE alias='champion'")
                    database.commit()
                    return
                database.rollback()
        raise RuntimeError("Model alias changed repeatedly during reconciliation")


def main() -> None:
    """Run bounded scheduling polls or a durable local model worker, never a trading service."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("poll", "worker"))
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--store", type=Path)
    parser.add_argument("--dataset", default="hk00700_daily")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--polls", type=int, default=1)
    parser.add_argument("--interval", type=float, default=5.)
    args = parser.parse_args()
    if args.mode == "worker":
        worker(args.root)
        return
    if args.store is None or args.output is None or args.polls < 1 or args.interval <= 0:
        raise ValueError("Polls need a store, output and positive poll count/interval")
    for index in range(args.polls):
        write_report(args.output, poll(args.root, args.store, args.dataset))
        if index + 1 < args.polls:
            sleep(args.interval)


if __name__ == "__main__":
    main()
