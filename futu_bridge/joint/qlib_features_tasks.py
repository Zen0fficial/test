"""Real MongoDB-backed native Qlib TaskManager/TrainerRM acceptance and recovery.

Only a uniquely owned loopback database is used. Native atomic claiming,
serialization, status transitions and exception rollback stay unchanged. The
coordinator owns worker processes, but never starts/stops the MongoDB server.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import pickle
import subprocess
import sys
from time import monotonic, sleep, time_ns
from urllib.parse import urlparse
from uuid import uuid4

import numpy as np
import pandas as pd

from .qlib_features import load_daily, segments, storage
from .session import write_report
from .tracking import EXPERIMENT, prepare_tracking, tracking_uri


class InjectedTaskFailure(RuntimeError):
    """Identify the deliberate first-attempt failure without swallowing real training errors."""


def validate_uri(uri: str) -> None:
    """Refuse external servers, credentials and multi-host URIs for disposable acceptance data."""
    parsed = urlparse(uri)
    if (parsed.scheme != "mongodb" or parsed.hostname not in ("127.0.0.1", "localhost", "::1")
            or parsed.username or parsed.password or "," in parsed.netloc):
        raise ValueError("Task acceptance requires a credential-free single loopback MongoDB endpoint")


def initialize(root: Path, uri: str, database: str) -> None:
    """Configure native Qlib against the isolated task store and precreated local experiment."""
    import qlib
    from qlib.config import REG_US

    validate_uri(uri)
    if not database.startswith("codex_qlib_") or len(database) != len("codex_qlib_") + 32:
        raise ValueError("Task database must be a newly generated codex_qlib UUID namespace")
    qlib.init(provider_uri=str(root / "qlib_data"), region=REG_US, kernels=1, logging_level=40,
              expression_cache=None, dataset_cache=None,
              mongo={"task_url": uri, "task_db_name": database},
              exp_manager={"class": "MLflowExpManager", "module_path": "qlib.workflow.expm",
                           "kwargs": {"uri": tracking_uri(root), "default_exp_name": EXPERIMENT}})


def dataset(frame: pd.DataFrame, root: Path) -> dict[str, object]:
    """Build causal real daily features for small native distributed regression tasks."""
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


def train_logged(task: dict[str, object], experiment_name: str, recorder_name: str | None = None) -> object:
    """Train via native task_train while recording the authoritative atomic claim and worker overlap."""
    from qlib.model.trainer import task_train
    from qlib.workflow.task.manage import TaskManager
    from qlib.workflow.task.utils import get_mongodb

    database = get_mongodb()
    key, pid = task["joint_task"]["key"], os.getpid()
    native = database.training.find_one({"filter.joint_task.key": key})
    if native is None or native["status"] != TaskManager.STATUS_RUNNING:
        raise ValueError("Training callback did not receive an atomically claimed native task")
    database.audit.insert_one({"kind": "start", "task_key": key, "pid": pid,
                               "native_status": native["status"], "time_ns": time_ns()})
    deadline = monotonic() + 45
    while len(database.audit.distinct("pid", {"kind": "start"})) < 2:
        if monotonic() > deadline:
            raise TimeoutError("Both native workers did not hold claims concurrently")
        sleep(.05)
    recorder = task_train(task, experiment_name=experiment_name, recorder_name=recorder_name)
    recorder.set_tags(joint_worker_pid=pid, joint_task_key=key)
    database.audit.insert_one({"kind": "complete", "task_key": key, "pid": pid, "time_ns": time_ns(),
                               "recorder_id": recorder.id})
    return recorder


def retry_train(task: dict[str, object], experiment_name: str) -> object:
    """Fail exactly once in the native safe-fetch context, then perform real model training."""
    from qlib.model.trainer import task_train
    from qlib.workflow.task.utils import get_mongodb

    events = get_mongodb().injections
    key = task["joint_task"]["key"]
    previous = events.find_one_and_update({"_id": key}, {"$inc": {"attempts": 1}}, upsert=True)
    if previous is None:
        raise InjectedTaskFailure("Deliberate first-attempt failure before native training")
    return task_train(task, experiment_name=experiment_name, recorder_name="retry_native_ridge")


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


def worker(root: Path, uri: str, database: str, identity: str, crash: bool = False) -> None:
    """Run a real native TrainerRM worker, or intentionally exit after claiming an orphan task."""
    initialize(root, uri, database)
    from qlib.model.trainer import TrainerRM
    from qlib.workflow.task.manage import TaskManager

    if crash:
        task = TaskManager("orphan").fetch_task()
        if task is None:
            raise ValueError("Missing orphan recovery task")
        write_report(root / "orphan_claim.json", {"id": str(task["_id"]), "status": task["status"],
                                                 "pid": os.getpid()})
        os._exit(23)
    write_report(root / f"worker_{identity}_ready.json", {"pid": os.getpid()})
    deadline = monotonic() + 45
    while not (root / "go.json").exists():
        if monotonic() > deadline:
            raise TimeoutError("Coordinator did not release native workers")
        sleep(.05)
    TrainerRM(experiment_name=EXPERIMENT, task_pool="training", train_func=train_logged).worker()
    write_report(root / f"worker_{identity}_done.json", {"pid": os.getpid(), "passed": True})


def verify_training(tasks: list[dict[str, object]], manager: object, root: Path) -> dict[str, object]:
    """Check native Mongo results, recorder artifacts and predictions against independent sklearn fits."""
    from bson.binary import Binary
    from qlib.model.trainer import TrainerRM
    from qlib.utils import init_instance_by_config
    from sklearn.linear_model import Ridge

    trainer = TrainerRM(experiment_name=EXPERIMENT, task_pool="training", skip_run_task=True)
    recorders = trainer.train(tasks)
    trainer.end_train(recorders)
    results = []
    for task, record in zip(tasks, recorders):
        native = next(manager.query({"filter.joint_task.key": task["joint_task"]["key"]}))
        raw = manager.task_pool.find_one({"_id": native["_id"]})
        if native["def"] != task or native["status"] != "done" or native["res"].id != record.id:
            raise ValueError("Native task/result roundtrip disagrees with submitted definitions")
        if not isinstance(raw["def"], Binary) or not isinstance(raw["res"], Binary):
            raise ValueError("Native BSON binary task/result storage was bypassed")
        data = init_instance_by_config(task["dataset"])
        train = data.prepare("train", col_set=["feature", "label"], data_key="learn")
        test = data.prepare("test", col_set=["feature", "label"], data_key="learn")
        independent = Ridge(alpha=task["model"]["kwargs"]["alpha"], fit_intercept=True).fit(
            train["feature"].to_numpy(), train["label"].iloc[:, 0].to_numpy())
        prediction = record.load_object("pred.pkl").iloc[:, 0]
        restored = record.load_object("params.pkl").predict(data)
        if (record.status != "FINISHED" or not prediction.index.equals(test.index)
                or not np.array_equal(prediction.to_numpy(), restored.to_numpy())
                or not np.allclose(prediction, independent.predict(test["feature"]), atol=1e-12, rtol=1e-12)):
            raise ValueError("Distributed native model, recorder or independent regression result differs")
        results.append({"task_key": task["joint_task"]["key"], "task_id": str(native["_id"]),
                        "recorder_id": record.id, "native_status": native["status"], "test_rows": len(test),
                        "independent_predictions_match": True, "reload_predictions_exact": True})
    write_report(root / "trained_tasks.json", results)
    return {"passed": True, "models": results, "trainer": "Native TrainerRM + native task_train"}


def run(response: Path, output: Path, repo: Path, *, mongo_uri: str) -> dict[str, object]:
    """Verify native atomic workers, exceptions, staged tasks and abrupt-worker recovery on real MongoDB."""
    from pymongo import MongoClient

    validate_uri(mongo_uri)
    root, repo = output.resolve(), repo.resolve()
    root.mkdir(parents=True, exist_ok=False)
    source = response.resolve().read_bytes()
    source_hash = hashlib.sha256(source).hexdigest()
    (root / "provider_response.json").write_bytes(source)
    frame = load_daily(root / "provider_response.json")
    storage(frame, root / "qlib_data")
    prepare_tracking(root)
    database = "codex_qlib_" + uuid4().hex
    client = MongoClient(mongo_uri, serverSelectionTimeoutMS=5000)
    workers: list[subprocess.Popen] = []
    logs = []
    owned_database = False
    write_report(root / "status.json", {"state": "running", "database": database})
    try:
        version = client.server_info()["version"]
        if database in client.list_database_names():
            raise ValueError("Refusing an unexpectedly preexisting task database")
        owned_database = True
        initialize(root, mongo_uri, database)
        from qlib.workflow.task.manage import TaskManager, run_task

        tasks = definitions(dataset(frame, root), root, source_hash)
        manager = TaskManager("training")
        first_ids = manager.create_task(deepcopy(tasks))
        repeat_ids = manager.create_task(deepcopy(tasks))
        if first_ids != repeat_ids or manager.task_stat() != {"waiting": len(tasks)}:
            raise ValueError("Native sequential task-definition deduplication failed")
        write_report(root / "tasks.json", tasks)
        base = [sys.executable, "-m", "futu_bridge.joint.qlib_features_tasks", "--worker", "--output", str(root),
                "--mongo-uri", mongo_uri, "--database", database]
        for identity in ("a", "b"):
            log = (root / f"worker_{identity}.log").open("w", encoding="utf-8")
            logs.append(log)
            workers.append(subprocess.Popen(base + ["--identity", identity], cwd=repo,
                stdout=log, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0))
        deadline = monotonic() + 60
        while not all((root / f"worker_{i}_ready.json").exists() for i in ("a", "b")):
            if monotonic() > deadline or any(p.poll() is not None for p in workers):
                raise RuntimeError("A native worker failed before becoming ready")
            sleep(.05)
        write_report(root / "go.json", {"released": True})
        deadline = monotonic() + 180
        while any(process.poll() is None for process in workers):
            if monotonic() > deadline:
                raise TimeoutError("Native distributed training exceeded its bounded acceptance timeout")
            sleep(.1)
        if any(process.returncode != 0 for process in workers):
            raise RuntimeError("Native TrainerRM worker failed; inspect retained worker logs")
        if manager.task_stat() != {"done": len(tasks)}:
            raise ValueError("Some native training tasks did not finish")
        audit = list(client[database].audit.find({}, {"_id": 0}))
        keys = [t["joint_task"]["key"] for t in tasks]
        for kind in ("start", "complete"):
            if sorted(e["task_key"] for e in audit if e["kind"] == kind) != sorted(keys):
                raise ValueError("Missing or duplicate worker execution for a native task")
        pids = {e["pid"] for e in audit if e["kind"] == "start"}
        expected_pids = {json.loads((root / f"worker_{i}_ready.json").read_text())["pid"] for i in ("a", "b")}
        if pids != expected_pids:
            raise ValueError("Both spawned native workers must execute actual training tasks")
        first_claims = [min(e["time_ns"] for e in audit if e["kind"] == "start" and e["pid"] == pid)
                        for pid in pids]
        first_completion = min(e["time_ns"] for e in audit if e["kind"] == "complete")
        if max(first_claims) >= first_completion:
            raise ValueError("Native worker claims did not overlap in time")
        trained = verify_training(tasks, manager, root)
        retry = TaskManager("retry")
        task = deepcopy(tasks[0])
        task["joint_task"]["key"] = "retry_ridge"
        retry.create_task([task])
        try:
            run_task(retry_train, "retry", experiment_name=EXPERIMENT)
        except InjectedTaskFailure:
            if retry.task_stat() != {"waiting": 1}:
                raise ValueError("Native exception rollback did not restore waiting status")
        else:
            raise ValueError("First-attempt injection did not run")
        run_task(retry_train, "retry", experiment_name=EXPERIMENT)
        if retry.task_stat() != {"done": 1} or client[database].injections.find_one()["attempts"] != 2:
            raise ValueError("Native retry did not train and finish exactly on its second attempt")
        staged = TaskManager("staged")
        staged.create_task([{"source": str(root / "provider_response.json"), "sha256": source_hash}])
        run_task(first_stage, "staged", after_status=TaskManager.STATUS_PART_DONE)
        if staged.task_stat() != {"part_done": 1}:
            raise ValueError("Native staged task did not preserve PART_DONE")
        run_task(second_stage, "staged", before_status=TaskManager.STATUS_PART_DONE)
        stage_result = next(staged.query())["res"]
        if staged.task_stat() != {"done": 1} or stage_result["mean_close"] != float(frame.close.tail(5).mean()):
            raise ValueError("Native staged typed-data result differs from real source")
        orphan = TaskManager("orphan")
        orphan_ids = orphan.create_task([{"source": str(root / "provider_response.json"), "sha256": source_hash}])
        with (root / "orphan_worker.log").open("w", encoding="utf-8") as log:
            process = subprocess.run(base + ["--identity", "crash", "--crash"], cwd=repo,
                                     stdout=log, stderr=subprocess.STDOUT, timeout=60, check=False)
        if process.returncode != 23 or orphan.task_stat() != {"running": 1}:
            raise ValueError("Abrupt worker failure did not leave the expected native running task")
        orphan.reset_waiting({"_id": orphan_ids[0]})
        if orphan.task_stat() != {"waiting": 1}:
            raise ValueError("Native explicit orphan recovery failed")
        run_task(first_stage, "orphan")
        if orphan.task_stat() != {"done": 1}:
            raise ValueError("Recovered orphan task failed to complete")
        write_report(root / "worker_events.json", audit)
        report = {"passed": True, "mongo_version": version, "mongo_uri": mongo_uri, "database": database,
                  "workers": sorted(pids), "concurrent_native_claims": True, "duplicate_executions": 0,
                  "native_definition_deduplication": True, "training": trained,
                  "native_failure_retry": {"attempts": 2, "rollback_status": "waiting", "final_status": "done"},
                  "native_stages": {"transitions": ["waiting", "running", "part_done", "running", "done"],
                                    "result": stage_result},
                  "native_orphan_recovery": {"exit_code": 23, "before_reset": "running", "final_status": "done"},
                  "source_sha256": source_hash, "real_sessions": len(frame), "providers_modified": False,
                  "real_orders_enabled": False, "shared_python": sys.executable,
                  "limitations": ["Definition deduplication is sequential; multi-producer creation is not atomic",
                                  "Native TaskManager has no automatic lease expiry; orphan reset is explicit",
                                  "Two local processes, not a tested multi-host cluster"]}
        write_report(root / "report.json", report)
        write_report(root / "status.json", {"state": "completed"})
        return report
    except Exception:
        write_report(root / "status.json", {"state": "failed", "database": database})
        raise
    finally:
        for process in workers:
            if process.poll() is None:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], check=False,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
                else:
                    process.terminate()
                process.wait(timeout=15)
        for log in logs:
            log.close()
        # The database name was generated here; never remove an existing user database.
        if owned_database:
            client.drop_database(database)
        client.close()


def main() -> None:
    """Expose the native task coordinator and isolated child-worker entry points."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--response", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--mongo-uri", required=True)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--database")
    parser.add_argument("--identity", default="worker")
    parser.add_argument("--crash", action="store_true")
    args = parser.parse_args()
    if args.worker:
        worker(args.output.resolve(), args.mongo_uri, args.database, args.identity, args.crash)
    else:
        if args.response is None:
            parser.error("--response is required for the coordinator")
        print(json.dumps(run(args.response, args.output, args.repo, mongo_uri=args.mongo_uri), indent=2))


if __name__ == "__main__":
    main()
