"""Faithful Qlib recorder trainers over the joint task store.

Lifecycle and recorder ordering follow Qlib 0.9.7 (Microsoft, MIT license).
Native task_train, begin_task_train and end_task_train remain unmodified.
Only task persistence and ownership differ; there is no MongoDB service.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from threading import Event, Thread
from time import monotonic, sleep
from typing import Any, Callable, Iterator

from qlib.model.trainer import Trainer, begin_task_train, end_task_train, task_train
from qlib.workflow.recorder import Recorder

from .manager import LeaseLost, TaskManager


@contextmanager
def renewing(manager: TaskManager, task: dict[str, Any]) -> Iterator[None]:
    """Maintain a bounded renewable lease while native training executes outside SQLite transactions."""
    stopped = Event()
    failures: list[Exception] = []

    def heartbeat() -> None:
        """Stop lease renewal immediately after ownership is lost or storage fails."""
        interval = max(.01, manager.lease_ns / 3_000_000_000)
        while not stopped.wait(interval):
            try:
                manager.heartbeat(task)
            except Exception as error:
                failures.append(error)
                return

    thread = Thread(target=heartbeat, name="joint-task-lease", daemon=True)
    thread.start()
    try:
        yield
        if failures:
            raise LeaseLost("Task heartbeat failed; its training result cannot be accepted") from failures[0]
    finally:
        stopped.set()
        thread.join(timeout=35)
        if thread.is_alive():
            raise TimeoutError("Task heartbeat did not stop within the SQLite lock timeout")


def run_task(task_func: Callable[..., Any], task_pool: str, *, root: Path,
             query: dict[str, Any] | None = None, force_release: bool = False,
             before_status: str = "waiting", after_status: str = "done", retries: bool = True,
             lease_seconds: float = 30, max_attempts: int = 3, idle_timeout: float = 300,
             retry_backoff_seconds: float = .1, max_retry_backoff_seconds: float = 30,
             **kwargs: Any) -> bool:
    """Run eligible stages, preserving native callbacks with bounded retries and fenced result commits."""
    if before_status not in ("waiting", "part_done"):
        raise ValueError("The fetched task must be waiting or part_done")
    manager = TaskManager(task_pool, root, lease_seconds=lease_seconds, max_attempts=max_attempts,
                          retry_backoff_seconds=retry_backoff_seconds,
                          max_retry_backoff_seconds=max_retry_backoff_seconds)
    if force_release:
        from qlib.config import C
        from qlib.utils.paral import call_in_subproc

        task_func = call_in_subproc(task_func, C)
    ever_run = False
    idle_deadline = monotonic() + idle_timeout
    while True:
        task = manager.fetch_task(query, before_status)
        if task is None:
            states = manager.task_stat(query)
            if states.get("running", 0) or states.get(before_status, 0):
                if monotonic() >= idle_deadline:
                    raise TimeoutError("Worker waited too long for other running claims to finish or expire")
                sleep(.05)
                continue
            break
        idle_deadline = monotonic() + idle_timeout
        try:
            with renewing(manager, task):
                result = task_func(task["def"] if before_status == "waiting" else task["res"], **kwargs)
            manager.commit_task_res(task, result, after_status)
            ever_run = True
        except LeaseLost:
            raise
        except KeyboardInterrupt:
            manager.return_task(task, before_status)
            raise
        except Exception as error:
            if not retries:
                manager.return_task(task, before_status)
                raise
            manager.fail(task, error)
    manager.reconcile()
    return ever_run


class TrainerRM(Trainer):
    """Qlib-compatible native recorder training using local fenced task workers."""

    STATUS_KEY = "train_status"
    STATUS_BEGIN = "begin_task_train"
    STATUS_END = "end_task_train"
    TM_ID = "_id in TaskManager"

    def __init__(self, experiment_name: str | None = None, task_pool: str | None = None,
                 train_func: Callable[..., Any] = task_train, skip_run_task: bool = False,
                 default_rec_name: str | None = None, *, root: Path,
                 lease_seconds: float = 30, max_attempts: int = 3, wait_timeout: float = 300,
                 retry_backoff_seconds: float = .1, max_retry_backoff_seconds: float = 30) -> None:
        """Configure the same recorder contract with an explicit local task-store directory."""
        super().__init__()
        self.experiment_name = experiment_name
        self.task_pool = task_pool
        self.train_func = train_func
        self.skip_run_task = skip_run_task
        self.default_rec_name = default_rec_name
        self.root = root
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts
        self.wait_timeout = wait_timeout
        self.retry_backoff_seconds = retry_backoff_seconds
        self.max_retry_backoff_seconds = max_retry_backoff_seconds

    def _manager(self, experiment_name: str | None) -> TaskManager:
        """Resolve the task pool using native experiment-name fallback rules."""
        pool = self.task_pool or experiment_name
        if pool is None:
            raise ValueError("A task pool or experiment name is required")
        return TaskManager(pool, self.root, lease_seconds=self.lease_seconds, max_attempts=self.max_attempts,
                           retry_backoff_seconds=self.retry_backoff_seconds,
                           max_retry_backoff_seconds=self.max_retry_backoff_seconds)

    def train(self, tasks: list[dict[str, Any]] | dict[str, Any], train_func: Callable[..., Any] | None = None,
              experiment_name: str | None = None, before_status: str = "waiting", after_status: str = "done",
              default_rec_name: str | None = None, **kwargs: Any) -> list[Recorder]:
        """Train or collect submitted tasks in original order, including repeated definitions."""
        tasks = [tasks] if isinstance(tasks, dict) else tasks
        if not tasks:
            return []
        experiment_name = experiment_name or self.experiment_name
        manager = self._manager(experiment_name)
        identities = manager.create_task(tasks)
        query = {"_id": {"$in": identities}}
        if not self.skip_run_task:
            run_task(train_func or self.train_func, manager.task_pool, root=self.root, query=query,
                     experiment_name=experiment_name, before_status=before_status, after_status=after_status,
                     recorder_name=default_rec_name or self.default_rec_name, lease_seconds=self.lease_seconds,
                     max_attempts=self.max_attempts, idle_timeout=self.wait_timeout, **kwargs)
        manager.wait(query, timeout=self.wait_timeout,
                     terminal_statuses=("done", "part_done") if self.is_delay() else ("done",))
        records = []
        for identity in identities:
            record = manager.re_query(identity)["res"]
            record.set_tags(**{self.STATUS_KEY: self.STATUS_BEGIN, self.TM_ID: identity})
            records.append(record)
        return records

    def end_train(self, recs: list[Recorder] | Recorder, **kwargs: Any) -> list[Recorder]:
        """Apply native completion tags after eager training without changing returned recorder order."""
        recs = [recs] if isinstance(recs, Recorder) else recs
        for record in recs:
            record.set_tags(**{self.STATUS_KEY: self.STATUS_END})
        return recs

    def worker(self, train_func: Callable[..., Any] | None = None,
               experiment_name: str | None = None) -> None:
        """Execute available tasks from this pool using native training and renewable claims."""
        experiment_name = experiment_name or self.experiment_name
        manager = self._manager(experiment_name)
        run_task(train_func or self.train_func, manager.task_pool, root=self.root, experiment_name=experiment_name,
                 lease_seconds=self.lease_seconds, max_attempts=self.max_attempts, idle_timeout=self.wait_timeout)

    def has_worker(self) -> bool:
        """Advertise the native trainer's externally startable worker capability."""
        return True


class DelayTrainerRM(TrainerRM):
    """Native delayed preparation and fitting with durable intermediate recorder results."""

    def __init__(self, experiment_name: str | None = None, task_pool: str | None = None,
                 train_func: Callable[..., Any] = begin_task_train,
                 end_train_func: Callable[..., Any] = end_task_train,
                 skip_run_task: bool = False, **kwargs: Any) -> None:
        """Retain Qlib's begin/end callbacks while storing part_done stages in the joint coordinator."""
        super().__init__(experiment_name, task_pool, train_func, skip_run_task, **kwargs)
        self.end_train_func = end_train_func
        self.delay = True

    def train(self, tasks: list[dict[str, Any]] | dict[str, Any], train_func: Callable[..., Any] | None = None,
              experiment_name: str | None = None, **kwargs: Any) -> list[Recorder]:
        """Always perform native recorder preparation, including when fitting is delegated to workers."""
        skipped = self.skip_run_task
        self.skip_run_task = False
        try:
            return super().train(tasks, train_func, experiment_name, after_status="part_done", **kwargs)
        finally:
            self.skip_run_task = skipped

    def end_train(self, recs: list[Recorder] | Recorder, end_train_func: Callable[..., Any] | None = None,
                  experiment_name: str | None = None, **kwargs: Any) -> list[Recorder]:
        """Finish native fitting from saved recorder objects and wait for ordered accepted results."""
        recs = [recs] if isinstance(recs, Recorder) else recs
        if not recs:
            return []
        experiment_name = experiment_name or self.experiment_name
        manager = self._manager(experiment_name)
        identities = [record.list_tags()[self.TM_ID] for record in recs]
        query = {"_id": {"$in": identities}}
        if not self.skip_run_task:
            run_task(end_train_func or self.end_train_func, manager.task_pool, root=self.root, query=query,
                     experiment_name=experiment_name, before_status="part_done", lease_seconds=self.lease_seconds,
                     max_attempts=self.max_attempts, idle_timeout=self.wait_timeout, **kwargs)
        manager.wait(query, timeout=self.wait_timeout)
        for record in recs:
            record.set_tags(**{self.STATUS_KEY: self.STATUS_END})
        return recs

    def worker(self, end_train_func: Callable[..., Any] | None = None,
               experiment_name: str | None = None) -> None:
        """Run only prepared part_done tasks, preserving native delayed trainer semantics."""
        experiment_name = experiment_name or self.experiment_name
        manager = self._manager(experiment_name)
        run_task(end_train_func or self.end_train_func, manager.task_pool, root=self.root,
                 experiment_name=experiment_name, before_status="part_done", lease_seconds=self.lease_seconds,
                 max_attempts=self.max_attempts, idle_timeout=self.wait_timeout)
