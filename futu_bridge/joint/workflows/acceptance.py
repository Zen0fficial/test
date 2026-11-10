"""Real HK.00700 native Qlib training and fault acceptance without a MongoDB service."""

from __future__ import annotations

import argparse
from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
from time import monotonic, monotonic_ns, sleep, time_ns
from typing import Any
from unittest.mock import patch

import numpy as np
import pandas as pd
import psutil

from ..qlib_features import segments, storage
from ..session import write_report
from ..storage import SnapshotStore
from ..storage.captures import ingest
from ..tracking import EXPERIMENT, prepare_tracking, tracking_uri
from .manager import LeaseLost, TaskFailure, TaskManager
from .trainer import DelayTrainerRM, TrainerRM, run_task


def initialize(root: Path) -> None:
    """Configure native Qlib in the shared interpreter with local MLflow and one numerical worker."""
    import qlib
    from qlib.config import REG_US

    qlib.init(provider_uri=str(root / "qlib_data"), region=REG_US, kernels=1, logging_level=40,
              expression_cache=None, dataset_cache=None,
              exp_manager={"class": "MLflowExpManager", "module_path": "qlib.workflow.expm",
                           "kwargs": {"uri": tracking_uri(root), "default_exp_name": EXPERIMENT}})


def prepare_data(response: Path, root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Derive causal features from the immutable canonical Parquet snapshot, preserving real source prices."""
    store = SnapshotStore(root / "data")
    snapshot = ingest(store, response, "hk00700_daily", "futu-bars")
    raw = store.read(snapshot.identity).to_pandas()
    if set(raw.timeframe) != {"1d"} or set(raw.symbol) != {"HK.00700"} or len(raw) < 600:
        raise ValueError("Task acceptance requires at least 600 genuine HK.00700 daily observations")
    frame = raw[["open", "high", "low", "close", "volume"]].astype(float)
    frame.index = pd.DatetimeIndex(pd.to_datetime(raw.trading_date), name="date")
    storage(frame, root / "qlib_data")
    features = pd.DataFrame({"return_1": frame.close.pct_change(),
                            "close_mean_5": frame.close / frame.close.rolling(5).mean() - 1,
                            "volume_mean_5": frame.volume / frame.volume.rolling(5).mean() - 1})
    labels = (frame.close.shift(-1) / frame.close - 1).rename("LABEL0").to_frame()
    values = pd.concat({"feature": features, "label": labels}, axis=1).dropna()
    values.index = pd.MultiIndex.from_arrays([values.index, ["HK_00700"] * len(values)],
                                            names=["datetime", "instrument"])
    path = root / "task_features.parquet"
    values.to_parquet(path)
    lineage = {"snapshot_id": snapshot.identity, "snapshot_sha256": snapshot.sha256,
               "source_sha256": sha256(response.read_bytes()).hexdigest(), "rows": len(raw),
               "feature_sha256": sha256(path.read_bytes()).hexdigest()}
    config = {"class": "DatasetH", "module_path": "qlib.data.dataset", "kwargs": {
        "handler": {"class": "DataHandlerLP", "module_path": "qlib.data.dataset.handler", "kwargs": {
            "data_loader": {"class": "StaticDataLoader", "module_path": "qlib.data.dataset.loader",
                            "kwargs": {"config": str(path)}}}}, "segments": segments(frame)}}
    return config, lineage


def definitions(data_config: dict[str, Any], lineage: dict[str, Any], root: Path,
                pool: str, workers: int, count: int) -> list[dict[str, Any]]:
    """Create deterministic ridge tasks sharing a pinned real-data snapshot and native recorder contract."""
    return [{"model": {"class": "LinearModel", "module_path": "qlib.contrib.model.linear",
                        "kwargs": {"estimator": "ridge", "alpha": 10. ** (index - 2), "fit_intercept": True}},
             "dataset": deepcopy(data_config),
             "record": [{"class": "SignalRecord", "module_path": "qlib.workflow.record_temp"}],
             "joint_task": {**lineage, "key": f"{pool}_{index}", "root": str(root),
                            "pool": pool, "workers": workers}}
            for index in range(count)]


def train_logged(task: dict[str, Any], experiment_name: str, recorder_name: str | None = None) -> Any:
    """Retain native fitting while proving distinct worker claims overlap before the first model completes."""
    from qlib.model.trainer import task_train

    info = task["joint_task"]
    root, pool = Path(info["root"]), info["pool"]
    manager = TaskManager(pool, root / "tasks")
    state = next(manager.query({"filter.joint_task.key": info["key"]}))
    if state["status"] != "running" or not state["owner"].startswith(f"{os.getpid()}:"):
        raise ValueError("Native training callback did not hold its own atomically claimed task")
    marker = root / f"{pool}_claimed_{os.getpid()}.json"
    if not marker.exists():
        write_report(marker, {"pid": os.getpid(), "task": state["_id"], "time_ns": time_ns()})
    deadline = monotonic() + 60
    while len(list(root.glob(f"{pool}_claimed_*.json"))) < info["workers"]:
        if monotonic() > deadline:
            raise TimeoutError("All native worker claims did not overlap")
        sleep(.02)
    record = task_train(task, experiment_name=experiment_name, recorder_name=recorder_name)
    record.set_tags(joint_worker_pid=os.getpid(), joint_snapshot_id=info["snapshot_id"],
                    joint_snapshot_sha256=info["snapshot_sha256"], joint_source_sha256=info["source_sha256"])
    return record


def worker(root: Path, pool: str, identity: str, mode: str) -> None:
    """Execute a real worker, including owned processes that the coordinator will forcibly interrupt."""
    initialize(root)
    with patch("pymongo.MongoClient", side_effect=RuntimeError("MongoDB construction is forbidden")), \
            patch("qlib.workflow.task.utils.MongoClient", side_effect=RuntimeError("MongoDB is forbidden")):
        if mode == "normal":
            write_report(root / f"{pool}_ready_{identity}.json", {"pid": os.getpid()})
            deadline = monotonic() + 60
            while not (root / f"{pool}_go.json").exists():
                if monotonic() > deadline:
                    raise TimeoutError("Coordinator did not release worker")
                sleep(.02)
            TrainerRM(EXPERIMENT, pool, train_logged, root=root / "tasks").worker()
            return
        manager = TaskManager(pool, root / "tasks", lease_seconds=120)
        task = manager.fetch_task()
        if task is None:
            raise ValueError("No owned task was available for interruption")
        marker = {"pid": os.getpid(), "task": task["_id"], "fence": task["fence"], "stage": mode}
        if mode == "training":
            from qlib.contrib.model.linear import LinearModel
            from qlib.model.trainer import task_train

            original = LinearModel.fit

            def pause_inside_fit(model: Any, dataset: Any, *args: Any, **kwargs: Any) -> Any:
                """Pause inside the training callback after real fitting, before artifact/result publication."""
                original(model, dataset, *args, **kwargs)
                from qlib.workflow import R

                marker["uncommitted_recorder"] = R.get_recorder().id
                marker["checkpoint"] = "Native fit completed; model callback has not returned or saved artifacts"
                write_report(root / f"{pool}_kill_ready.json", marker)
                while True:
                    sleep(.1)

            with patch.object(LinearModel, "fit", pause_inside_fit):
                task_train(task["def"], experiment_name=EXPERIMENT)
        if mode in ("artifact", "committed"):
            from qlib.model.trainer import task_train

            record = task_train(task["def"], experiment_name=EXPERIMENT)
            marker["uncommitted_recorder"] = record.id
            if mode == "committed":
                manager.commit_task_res(task, record)
                marker["accepted_recorder"] = marker.pop("uncommitted_recorder")
        write_report(root / f"{pool}_kill_ready.json", marker)
        while True:
            sleep(.1)


def verify_records(tasks: list[dict[str, Any]], records: list[Any]) -> list[dict[str, Any]]:
    """Independently fit sklearn ridge, verify index/value parity and native model checkpoint reload."""
    from qlib.utils import init_instance_by_config
    from sklearn.linear_model import Ridge

    results = []
    for task, record in zip(tasks, records):
        dataset = init_instance_by_config(task["dataset"])
        train = dataset.prepare("train", col_set=["feature", "label"], data_key="learn")
        test = dataset.prepare("test", col_set=["feature", "label"], data_key="learn")
        independent = Ridge(alpha=task["model"]["kwargs"]["alpha"], fit_intercept=True).fit(
            train["feature"].to_numpy(), train["label"].iloc[:, 0].to_numpy())
        prediction = record.load_object("pred.pkl").iloc[:, 0]
        restored = record.load_object("params.pkl").predict(dataset)
        if (record.status != "FINISHED" or not prediction.index.equals(test.index)
                or not np.array_equal(prediction.to_numpy(), restored.to_numpy())
                or not np.allclose(prediction, independent.predict(test["feature"]), atol=1e-12, rtol=1e-12)):
            raise ValueError("Native model, saved checkpoint or independent regression prediction differs")
        results.append({"task_key": task["joint_task"]["key"], "recorder_id": record.id,
                        "test_rows": len(test), "reload_exact": True, "independent_predictions_match": True})
    return results


def spawn(root: Path, repo: Path, pool: str, identity: str, mode: str) -> tuple[subprocess.Popen, Any]:
    """Start one hidden owned worker in the same interpreter with bounded numerical thread pools."""
    command = [sys.executable, "-m", "futu_bridge.joint.workflows.acceptance", "--worker", "--output", str(root),
               "--pool", pool, "--identity", identity, "--mode", mode]
    environment = dict(os.environ)
    environment.update({key: "1" for key in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
                                              "NUMEXPR_NUM_THREADS")})
    environment["LOKY_MAX_CPU_COUNT"] = "2"
    log = (root / f"{pool}_{identity}.log").open("w", encoding="utf-8")
    process = subprocess.Popen(command, cwd=repo, env=environment, stdout=log, stderr=subprocess.STDOUT,
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    return process, log


def stop_worker(process: subprocess.Popen) -> list[int]:
    """Kill only the owned process tree, including Windows venv redirector children, and verify exit."""
    children = []
    if process.poll() is None:
        try:
            children = psutil.Process(process.pid).children(recursive=True)
        except psutil.NoSuchProcess:
            children = []
        for child in reversed(children):
            try:
                child.kill()
            except psutil.NoSuchProcess:
                pass
        if process.poll() is None:
            process.kill()
    process.wait(timeout=30)
    gone, alive = psutil.wait_procs(children, timeout=30)
    if alive:
        raise RuntimeError("An owned task worker descendant survived forced interruption")
    return [child.pid for child in gone]


def concurrent_training(root: Path, repo: Path, tasks: list[dict[str, Any]],
                        workers: int) -> dict[str, Any]:
    """Verify two or four independent workers and ordered recorder collection on real Parquet features."""
    pool = tasks[0]["joint_task"]["pool"]
    manager = TaskManager(pool, root / "tasks")
    identities = manager.create_task(tasks)
    if manager.create_task(deepcopy(tasks)) != identities or manager.create_task(tasks, dry_run=True) != []:
        raise ValueError("Definition deduplication or dry_run differs from the native contract")
    processes, logs = [], []
    try:
        for index in range(workers):
            process, log = spawn(root, repo, pool, str(index), "normal")
            processes.append(process)
            logs.append(log)
        deadline = monotonic() + 120
        while not all((root / f"{pool}_ready_{index}.json").exists() for index in range(workers)):
            if monotonic() > deadline or any(process.poll() is not None for process in processes):
                raise RuntimeError("Worker failed before becoming ready; inspect retained worker logs")
            sleep(.05)
        write_report(root / f"{pool}_go.json", {"released": True})
        deadline = monotonic() + 180
        while any(process.poll() is None for process in processes):
            if monotonic() > deadline:
                raise TimeoutError("Real task workers exceeded acceptance deadline")
            sleep(.05)
        if any(process.returncode != 0 for process in processes):
            raise RuntimeError("A real task worker failed; inspect retained worker logs")
        trainer = TrainerRM(EXPERIMENT, pool, root=root / "tasks", skip_run_task=True)
        records = trainer.end_train(trainer.train(tasks))
        verified = verify_records(tasks, records)
        claimed = [json.loads(path.read_text()) for path in root.glob(f"{pool}_claimed_*.json")]
        expected = {json.loads((root / f"{pool}_ready_{index}.json").read_text())["pid"]
                    for index in range(workers)}
        actual = {int(record.list_tags()["joint_worker_pid"]) for record in records}
        if expected != actual or len(claimed) != workers or manager.task_stat() != {"done": len(tasks)}:
            raise ValueError("Every worker must train real tasks and all accepted records must be accounted for")
        from qlib.workflow.task.collect import RecorderCollector

        collector = RecorderCollector(lambda: records)
        collected = collector.collect(only_exist=False)["pred"]
        if set(collected) != {record.id for record in records}:
            raise ValueError("Native RecorderCollector did not recover every accepted prediction")
        return {"passed": True, "workers": sorted(actual), "overlapping_claims": claimed,
                "models": verified, "native_collector": True, "history": manager.reconcile()}
    finally:
        for process in processes:
            stop_worker(process)
        for log in logs:
            log.close()


def fault_cases(root: Path, repo: Path, task: dict[str, Any]) -> dict[str, Any]:
    """Exercise genuine worker kills, stale fencing, retry exhaustion and typed staged results."""
    from qlib.model.trainer import task_train

    reports: dict[str, Any] = {}
    for mode in ("claim", "training", "artifact", "committed"):
        pool = "killed_" + mode
        manager = TaskManager(pool, root / "tasks", lease_seconds=.8)
        identities = manager.create_task([task])
        process, log = spawn(root, repo, pool, mode, mode)
        try:
            marker = root / f"{pool}_kill_ready.json"
            deadline = monotonic() + 90
            while not marker.exists():
                if process.poll() is not None or monotonic() > deadline:
                    raise RuntimeError("Fault worker did not reach its checkpoint")
                sleep(.02)
            old = manager.re_query(identities[0])
            terminated = stop_worker(process)
            checkpoint = json.loads(marker.read_text())
            if checkpoint["pid"] not in (process.pid, *terminated):
                raise ValueError("The interruption did not terminate the actual owned Python worker")
            if mode == "committed":
                before = manager.re_query(identities[0])
                reconciliation = manager.reconcile()
                after = manager.re_query(identities[0])
                if (before["status"] != "done" or before["fence"] != after["fence"]
                        or before["res"].id != checkpoint["accepted_recorder"]
                        or after["res"].list_tags().get("joint_result_accepted") != "true"
                        or reconciliation["pending_errors"]):
                    raise ValueError("Committed result/outbox recovery lost or duplicated the winning recorder")
                reports[pool] = {"passed": True, "killed_pid": checkpoint["pid"], "kill_checkpoint": checkpoint,
                                 "outbox_recovered_without_retraining": True, "accepted_fence": after["fence"]}
                continue
            recovery_started = monotonic()
            replacement = manager.fetch_task()
            if replacement is None or replacement["fence"] <= old["fence"]:
                raise ValueError("Killed worker's expired task was not reclaimed with a new fence")
            recovery_seconds = monotonic() - recovery_started
            if old["lease_ns"] <= monotonic_ns() or recovery_seconds > 5:
                raise ValueError("A proven dead worker must be reclaimed promptly before its monotonic lease expires")
            rejected = []
            for operation in (manager.heartbeat, lambda value: manager.commit_task_res(value, "stale")):
                try:
                    operation(old)
                except LeaseLost:
                    rejected.append(True)
                else:
                    raise ValueError("A stale worker was allowed to alter a replacement claim")
            manager.return_task(replacement)
            run_task(task_train, pool, root=root / "tasks", experiment_name=EXPERIMENT)
            state = manager.re_query(identities[0])
            verify_records([task], [state["res"]])
            reports[pool] = {"passed": state["status"] == "done", "killed_pid": checkpoint["pid"],
                             "kill_checkpoint": checkpoint, "stale_rejected": len(rejected),
                             "accepted_fence": state["fence"], "attempts": state["attempts"],
                             "recovered_before_lease_expiry": True, "recovery_seconds": recovery_seconds}
        finally:
            stop_worker(process)
            log.close()
    retry = TaskManager("retry", root / "tasks")
    retry.create_task([task])
    attempts = 0
    attempt_times = []

    def transient(value: dict[str, Any], experiment_name: str) -> Any:
        """Inject one pre-fit error, then invoke the unchanged native training function."""
        nonlocal attempts
        attempts += 1
        attempt_times.append(monotonic_ns())
        if attempts == 1:
            raise RuntimeError("Deliberate transient storage-independent failure")
        return task_train(value, experiment_name=experiment_name)

    run_task(transient, "retry", root=root / "tasks", experiment_name=EXPERIMENT)
    if attempts != 2 or retry.task_stat() != {"done": 1} or attempt_times[1] - attempt_times[0] < 100_000_000:
        raise ValueError("Transient native training did not recover on the second attempt")
    permanent = TaskManager("permanent", root / "tasks", max_attempts=2)
    permanent.create_task([task])

    def fail_always(value: dict[str, Any]) -> None:
        """Exercise bounded terminal failure without altering the real market dataset."""
        raise RuntimeError("Deliberate persistent callback failure")

    run_task(fail_always, "permanent", root=root / "tasks")
    try:
        permanent.wait(timeout=.1)
    except TaskFailure:
        reports["permanent"] = {"passed": permanent.task_stat() == {"failed": 1}, "wait_terminated": True}
    else:
        raise ValueError("Terminal failure did not stop waiting")
    reports["retry"] = {"passed": True, "attempts": attempts, "bounded_backoff_verified": True,
                        "monotonic_attempt_ns": attempt_times, "minimum_backoff_seconds": .1}
    return reports


def run(response: Path, output: Path, repo: Path) -> dict[str, Any]:
    """Validate native normal/delayed training, worker contention and durable recovery on real daily data."""
    root = output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    if sys.executable.lower() != str(repo / ".venv-joint" / "Scripts" / "python.exe").lower():
        raise ValueError("Task acceptance must use the shared Python environment")
    data, lineage = prepare_data(response.resolve(), root)
    source_hashes = {path.name: sha256(path.read_bytes()).hexdigest()
                     for path in sorted(Path(__file__).parent.glob("*.py"))}
    lineage["code_sha256"] = sha256(json.dumps(source_hashes, sort_keys=True).encode()).hexdigest()
    lineage["lock_sha256"] = sha256((repo / "requirements/joint-py312-windows.lock").read_bytes()).hexdigest()
    prepare_tracking(root)
    initialize(root)
    with patch("pymongo.MongoClient", side_effect=RuntimeError("MongoDB construction is forbidden")), \
            patch("qlib.workflow.task.utils.MongoClient", side_effect=RuntimeError("MongoDB is forbidden")):
        results = []
        for workers, count in ((2, 6), (4, 8)):
            tasks = definitions(data, lineage, root, f"training_{workers}", workers, count)
            results.append(concurrent_training(root, repo, tasks, workers))
        tasks = definitions(data, lineage, root, "delayed", 1, 2)
        delayed = DelayTrainerRM(EXPERIMENT, "delayed", root=root / "tasks")
        records = delayed.train(tasks)
        manager = TaskManager("delayed", root / "tasks")
        if manager.task_stat() != {"part_done": 2}:
            raise ValueError("Delayed trainer did not preserve native part_done preparation")
        for record in records:
            if "params.pkl" in record.list_artifacts():
                raise ValueError("Delayed preparation unexpectedly trained a model")
        records = delayed.end_train(records)
        delayed_result = verify_records(tasks, records)
        fault_result = fault_cases(root, repo, tasks[0])
        if not all(value["passed"] for value in fault_result.values()):
            raise ValueError("A task fault acceptance case failed")
        histories = []
        pools = ("training_2", "training_4", "delayed", "retry", "permanent",
                 "killed_claim", "killed_training", "killed_artifact", "killed_committed")
        for pool in pools:
            value = TaskManager(pool, root / "tasks").reconcile()
            if value["pending_errors"]:
                raise ValueError("Accepted recorder outbox remained unreconciled")
            histories.append({"pool": pool, **value})
        report = {"passed": True, "implementation": "Qlib native training + joint SQLite/Parquet TaskManager",
                  "native_training_callbacks": True, "mongo_client_construction_guarded": True,
                  "input": lineage, "concurrent_training": results, "delayed_training": delayed_result,
                  "fault_cases": fault_result, "histories": histories, "real_orders_enabled": False,
                  "delivery_semantics": "At least once execution; one accepted result per fenced stage",
                  "task_schema_version": 3, "lease_clock": "system-wide monotonic_ns",
                  "owner_identity": "Windows PID + exact GetProcessTimes creation FILETIME",
                  "retry_policy": "Bounded exponential monotonic backoff; default0.1s capped30s",
                  "recorder_reconciliation": "Per-pool OS lock before outbox read; 30s default wait; crash released",
                  "workflow_sources": source_hashes,
                  "multi_host": {"verified": False, "reason": "SQLite coordinator is one-host only"}}
        after_hashes = {path.name: sha256(path.read_bytes()).hexdigest()
                        for path in sorted(Path(__file__).parent.glob("*.py"))}
        if after_hashes != source_hashes:
            raise ValueError("Task implementation changed during acceptance")
        write_report(root / "report.json", report)
        return report


def main() -> None:
    """Run acceptance or one explicitly owned worker, without provider or trading connections."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--response", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--pool", default="training")
    parser.add_argument("--identity", default="a")
    parser.add_argument("--mode", choices=("normal", "claim", "training", "artifact", "committed"), default="normal")
    args = parser.parse_args()
    if args.worker:
        worker(args.output.resolve(), args.pool, args.identity, args.mode)
    else:
        if args.response is None:
            parser.error("--response is required for acceptance")
        print(json.dumps(run(args.response, args.output, args.repo.resolve()), indent=2))


if __name__ == "__main__":
    main()
