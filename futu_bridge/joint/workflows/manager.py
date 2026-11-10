"""Durable single-host Qlib task states, bounded retries and fenced worker leases.

This is a Qlib 0.9.7 TaskManager-compatible adapter, not a MongoDB emulation.
Task and result pickle payloads are trusted local artifacts only. SQLite provides
atomic ownership; Parquet preserves the append-only transition/result history.
Workers execute at least once. A lease accepts at most one winning result.
"""

from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import dataclass
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import pickle
import sqlite3
from time import monotonic, monotonic_ns, sleep, time_ns
from typing import Any, Iterator
from uuid import uuid4

import pyarrow as pa

from ..storage import SnapshotConflict, SnapshotStore
from .leases import owner_alive, process_birth, reconciliation_lock


class LeaseLost(RuntimeError):
    """A worker no longer owns the unexpired fenced claim it is trying to update."""


class TaskFailure(RuntimeError):
    """One or more requested tasks reached a terminal failure or cancellation."""


@dataclass(frozen=True)
class InsertResult:
    """Expose the inserted_id shape used by native TaskManager callers without requiring MongoDB."""

    inserted_id: str


def canonical(value: Any) -> Any:
    """Normalize dictionary insertion order without erasing Python value types."""
    if isinstance(value, dict):
        keys = sorted(value, key=lambda item: (type(item).__name__, repr(item)))
        return {key: canonical(value[key]) for key in keys}
    if isinstance(value, list):
        return [canonical(item) for item in value]
    if isinstance(value, tuple):
        return tuple(canonical(item) for item in value)
    return value


def definition_identity(value: Any) -> str:
    """Hash structural values independently of dictionary order and pickle string/object memoization."""
    def tagged(item: Any) -> Any:
        """Retain scalar/container types; opaque trusted-local objects use their serialized bytes."""
        if item is None or isinstance(item, (bool, int, str)):
            return [type(item).__name__, item]
        if isinstance(item, float):
            return ["float", item.hex()]
        if isinstance(item, bytes):
            return ["bytes", item.hex()]
        if isinstance(item, dict):
            pairs = [[tagged(key), tagged(val)] for key, val in item.items()]
            return ["dict", sorted(pairs, key=lambda pair: json.dumps(pair[0], sort_keys=True))]
        if isinstance(item, (tuple, list)):
            return [type(item).__name__, [tagged(part) for part in item]]
        return [f"{type(item).__module__}.{type(item).__qualname__}",
                sha256(pickle.dumps(item, protocol=5)).hexdigest()]

    return sha256(json.dumps(tagged(value), ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def matches(task: dict[str, Any], query: dict[str, Any]) -> bool:
    """Evaluate the documented equality, dotted-field and comparison query subset."""
    for name, expected in query.items():
        if name.startswith("$") and name not in ("$and", "$or"):
            raise ValueError(f"Unsupported task query operator: {name}")
        if name in ("$and", "$or"):
            values = [matches(task, item) for item in expected]
            if not (all(values) if name == "$and" else any(values)):
                return False
            continue
        actual: Any = task
        exists = True
        for part in name.split("."):
            if not isinstance(actual, dict) or part not in actual:
                actual, exists = None, False
                break
            actual = actual[part]
        if isinstance(expected, dict) and any(str(key).startswith("$") for key in expected):
            for operator, operand in expected.items():
                if operator not in ("$in", "$nin", "$eq", "$ne", "$gt", "$gte", "$lt", "$lte", "$exists"):
                    raise ValueError(f"Unsupported task query operator: {operator}")
                if operator == "$in" and actual not in operand or operator == "$nin" and actual in operand:
                    return False
                if operator == "$eq" and actual != operand or operator == "$ne" and actual == operand:
                    return False
                if operator == "$exists" and exists != bool(operand):
                    return False
                if operator in ("$gt", "$gte", "$lt", "$lte"):
                    if actual is None:
                        return False
                    comparison = {"$gt": actual > operand, "$gte": actual >= operand,
                                  "$lt": actual < operand, "$lte": actual <= operand}
                    if not comparison[operator]:
                        return False
        elif actual != expected:
            return False
    return True


class TaskManager:
    """Qlib-compatible task pool with single-host SQLite coordination and Parquet history."""

    STATUS_WAITING = "waiting"
    STATUS_RUNNING = "running"
    STATUS_PART_DONE = "part_done"
    STATUS_DONE = "done"
    TERMINAL = ("done", "failed", "cancelled", "removed")

    def __init__(self, task_pool: str, root: Path, *, lease_seconds: float = 30,
                 max_attempts: int = 3, owner: str | None = None,
                 retry_backoff_seconds: float = .1, max_retry_backoff_seconds: float = 30) -> None:
        """Open a dedicated task database; never modify the snapshot catalogue schema."""
        if not task_pool or len(task_pool) > 128 or lease_seconds <= 0 or max_attempts < 1:
            raise ValueError("A named pool, positive lease and at least one attempt are required")
        if (not math.isfinite(retry_backoff_seconds) or not math.isfinite(max_retry_backoff_seconds)
                or not 0 <= retry_backoff_seconds <= max_retry_backoff_seconds <= 3600):
            raise ValueError("Retry backoff requires 0 <= base <= maximum <= 3600 seconds")
        self.task_pool = task_pool
        self.root = root.resolve()
        if os.name == "nt" and str(self.root).startswith("\\\\"):
            raise ValueError("The single-host task coordinator cannot use a UNC/network root")
        self.root.mkdir(parents=True, exist_ok=True)
        self.database = self.root / "tasks.sqlite"
        self.store = SnapshotStore(self.root / "history")
        self.lease_ns = int(lease_seconds * 1_000_000_000)
        self.max_attempts = max_attempts
        self.retry_base_ns = int(retry_backoff_seconds * 1_000_000_000)
        self.retry_cap_ns = int(max_retry_backoff_seconds * 1_000_000_000)
        self.owner = owner or f"{os.getpid()}:{uuid4().hex}"
        with self.connection() as database:
            version = database.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2, 3):
                raise ValueError("Unsupported joint task schema version")
            database.execute("PRAGMA journal_mode=WAL")
            database.executescript("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, pool TEXT NOT NULL, fingerprint TEXT NOT NULL, definition BLOB NOT NULL,
                    status TEXT NOT NULL, resume_status TEXT NOT NULL, priority INTEGER NOT NULL DEFAULT 0,
                    owner TEXT, fence INTEGER NOT NULL DEFAULT 0, lease_ns INTEGER NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 0, failures INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL, result_sha TEXT, error TEXT, created_ns INTEGER NOT NULL,
                    UNIQUE(pool, fingerprint));
                CREATE INDEX IF NOT EXISTS eligible_tasks ON tasks(pool,status,priority DESC,created_ns);
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, pool TEXT NOT NULL, task_id TEXT NOT NULL,
                    status TEXT NOT NULL, kind TEXT NOT NULL, attempt INTEGER NOT NULL, fence INTEGER NOT NULL,
                    owner TEXT, occurred_ns INTEGER NOT NULL, error TEXT, result_sha TEXT);
                CREATE TABLE IF NOT EXISTS outbox (
                    task_id TEXT NOT NULL, fence INTEGER NOT NULL, result_sha TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending', error TEXT, PRIMARY KEY(task_id,fence));
                CREATE TABLE IF NOT EXISTS exports (pool TEXT PRIMARY KEY, sequence INTEGER, snapshot TEXT);
            """)
            database.execute("BEGIN IMMEDIATE")
            columns = {row["name"] for row in database.execute("PRAGMA table_info(tasks)")}
            additions = {"lease_clock": "TEXT NOT NULL DEFAULT 'wall_ns'",
                         "owner_pid": "INTEGER", "owner_birth": "TEXT",
                         "retry_base_ns": "INTEGER NOT NULL DEFAULT 100000000",
                         "retry_cap_ns": "INTEGER NOT NULL DEFAULT 30000000000",
                         "not_before_ns": "INTEGER NOT NULL DEFAULT 0", "retry_pid": "INTEGER", "retry_birth": "TEXT"}
            for name, declaration in additions.items():
                if name not in columns:
                    database.execute(f"ALTER TABLE tasks ADD COLUMN {name} {declaration}")
            legacy = database.execute("SELECT id FROM tasks WHERE status='running' AND lease_clock!='monotonic_ns'")
            for row in legacy.fetchall():
                database.execute("""UPDATE tasks SET status=resume_status,owner=NULL,owner_pid=NULL,owner_birth=NULL,
                    lease_ns=0,fence=fence+1,error='Legacy wall-clock lease invalidated by schema migration'
                    WHERE id=?""",
                                 (row["id"],))
                self._event(database, row["id"], "lease_clock_migration")
            database.execute("PRAGMA user_version=3")
            database.commit()

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """Close connections deterministically, with short transactions and durable WAL commits."""
        database = sqlite3.connect(self.database, isolation_level=None, timeout=30)
        database.row_factory = sqlite3.Row
        try:
            database.execute("PRAGMA synchronous=FULL")
            yield database
        finally:
            if database.in_transaction:
                database.rollback()
            database.close()

    def _decode(self, row: sqlite3.Row) -> dict[str, Any]:
        """Deserialize only local task artifacts and verify result content hashes first."""
        value = dict(row)
        value["_id"] = value.pop("id")
        value["def"] = pickle.loads(value.pop("definition"))
        value["filter"] = value["def"]
        if value["result_sha"]:
            payload = (self.store.root / "raw" / value["result_sha"]).read_bytes()
            if sha256(payload).hexdigest() != value["result_sha"]:
                raise ValueError("Task result archive is corrupt")
            value["res"] = pickle.loads(payload)
        return value

    def _event(self, database: sqlite3.Connection, identity: str, kind: str) -> None:
        """Append state history in the exact transaction that changed ownership or results."""
        database.execute("""INSERT INTO events
            (pool,task_id,status,kind,attempt,fence,owner,occurred_ns,error,result_sha)
            SELECT pool,id,status,?,attempts,fence,owner,?,error,result_sha FROM tasks WHERE id=?""",
                         (kind, time_ns(), identity))

    def create_task(self, task_def_l: list[dict[str, Any]], dry_run: bool = False,
                    print_nt: bool = False) -> list[str]:
        """Deduplicate definitions atomically and preserve submission order, including repeated IDs."""
        identities = []
        with self.connection() as database:
            database.execute("BEGIN IMMEDIATE")
            for definition in task_def_l:
                payload = pickle.dumps(canonical(definition), protocol=5)
                fingerprint = definition_identity(definition)
                row = database.execute("SELECT id FROM tasks WHERE pool=? AND fingerprint=?",
                                       (self.task_pool, fingerprint)).fetchone()
                if row is None:
                    if print_nt:
                        print(definition)
                    if dry_run:
                        continue
                    identity = uuid4().hex
                    database.execute("""INSERT INTO tasks
                        (id,pool,fingerprint,definition,status,resume_status,max_attempts,created_ns,
                        retry_base_ns,retry_cap_ns) VALUES (?,?,?,?,'waiting','waiting',?,?,?,?)""",
                                     (identity, self.task_pool, fingerprint, payload, self.max_attempts, time_ns(),
                                      self.retry_base_ns, self.retry_cap_ns))
                    self._event(database, identity, "created")
                else:
                    identity = row[0]
                identities.append(identity)
            database.commit()
        return [] if dry_run else identities

    @classmethod
    def list(cls, root: Path) -> list[str]:
        """List active task pools in a particular local coordinator, without configuring a global database."""
        database_path = root.resolve() / "tasks.sqlite"
        if not database_path.exists():
            return []
        with closing(sqlite3.connect(database_path)) as database:
            rows = database.execute("SELECT DISTINCT pool FROM tasks WHERE status!='removed' ORDER BY pool").fetchall()
        return [row[0] for row in rows]

    def insert_task_def(self, task_def: dict[str, Any]) -> InsertResult:
        """Insert or find one definition, strengthening native insertion with transactional deduplication."""
        return InsertResult(self.create_task([task_def])[0])

    def insert_task(self, task: dict[str, Any]) -> InsertResult:
        """Insert a native-shaped decoded task; raw BSON binary values are deliberately unsupported."""
        if not isinstance(task.get("def"), dict):
            raise ValueError("Task insertion requires a decoded dictionary definition")
        result = self.insert_task_def(task["def"])
        current = self.re_query(result.inserted_id)
        if "res" in task or task.get("status", "waiting") != "waiting" or task.get("priority", 0):
            self.replace_task(current, task)
        return result

    def replace_task(self, task: dict[str, Any], new_task: dict[str, Any]) -> None:
        """Replace a task through an explicit version check, retaining its immutable prior history."""
        definition = new_task["def"]
        payload = pickle.dumps(canonical(definition), protocol=5)
        status = new_task.get("status", "waiting")
        if status not in ("waiting", "part_done", "done", "failed", "cancelled"):
            raise ValueError("Replacement must be idle or terminal; running status requires a real claim")
        if status in ("part_done", "done") and "res" not in new_task:
            raise ValueError("A completed replacement stage requires a result")
        result_sha = self.store.archive(pickle.dumps(new_task["res"], protocol=5)) if "res" in new_task else None
        with self.connection() as database:
            database.execute("BEGIN IMMEDIATE")
            row = database.execute("SELECT * FROM tasks WHERE id=? AND pool=?",
                                   (task["_id"], self.task_pool)).fetchone()
            if row is None or row["fence"] != task["fence"]:
                raise LeaseLost("Replacement task version changed")
            if row["status"] == "running":
                self._owned(database, task)
            database.execute("""UPDATE tasks SET definition=?,fingerprint=?,status=?,resume_status=?,priority=?,
                owner=NULL,lease_ns=0,fence=fence+1,result_sha=?,error=NULL,not_before_ns=0,
                retry_pid=NULL,retry_birth=NULL WHERE id=?""",
                             (payload, definition_identity(definition), status, status, new_task.get("priority", 0),
                              result_sha, task["_id"]))
            self._event(database, task["_id"], "replaced")
            if result_sha is not None:
                database.execute("INSERT INTO outbox(task_id,fence,result_sha) VALUES (?,?,?)",
                                 (task["_id"], row["fence"] + 1, result_sha))
            database.commit()

    def _recover(self, database: sqlite3.Connection) -> None:
        """Recover expired monotonic leases or proven dead/PID-reused owners, including after system restart."""
        rows = database.execute("SELECT * FROM tasks WHERE pool=? AND status='running'", (self.task_pool,)).fetchall()
        now = monotonic_ns()
        for row in rows:
            expired = row["lease_clock"] != "monotonic_ns" or row["lease_ns"] <= now
            if not expired and owner_alive(row["owner_pid"], row["owner_birth"]):
                continue
            reason = "Worker monotonic lease expired" if expired else "Worker process exited or identity changed"
            database.execute("""UPDATE tasks SET failures=failures+1,
                status=CASE WHEN failures+1>=max_attempts THEN 'failed' ELSE resume_status END,
                owner=NULL,owner_pid=NULL,owner_birth=NULL,lease_ns=0,error=?,not_before_ns=0,
                retry_pid=NULL,retry_birth=NULL WHERE id=?""", (reason, row["id"]))
            self._event(database, row["id"], "lease_expired" if expired else "owner_exited")

    def _release_delays(self, database: sqlite3.Connection) -> None:
        """Release elapsed retry delays, or discard obsolete monotonic epochs after the scheduling process exits."""
        rows = database.execute("""SELECT * FROM tasks WHERE pool=? AND status IN ('waiting','part_done')
            AND not_before_ns>0""", (self.task_pool,)).fetchall()
        now = monotonic_ns()
        for row in rows:
            elapsed = row["not_before_ns"] <= now
            if not elapsed and owner_alive(row["retry_pid"], row["retry_birth"]):
                continue
            database.execute("UPDATE tasks SET not_before_ns=0,retry_pid=NULL,retry_birth=NULL WHERE id=?",
                             (row["id"],))
            self._event(database, row["id"], "retry_eligible" if elapsed else "retry_clock_owner_exited")

    def fetch_task(self, query: dict[str, Any] | None = None, status: str = "waiting") -> dict[str, Any] | None:
        """Atomically claim one eligible highest-priority task with a new monotonic fencing token."""
        if status not in ("waiting", "part_done"):
            raise ValueError("Only waiting or part_done tasks can be claimed")
        with self.connection() as database:
            database.execute("BEGIN IMMEDIATE")
            self._recover(database)
            self._release_delays(database)
            rows = database.execute("""SELECT * FROM tasks WHERE pool=? AND status=? AND not_before_ns<=?
                ORDER BY priority DESC,rowid""",
                                    (self.task_pool, status, monotonic_ns())).fetchall()
            identity = next((row["id"] for row in rows if matches(self._decode(row), query or {})), None)
            if identity is None:
                database.commit()
                return None
            pid = os.getpid()
            birth = process_birth(pid)
            if birth is None:
                raise RuntimeError("Cannot establish the claiming worker's process incarnation")
            database.execute("""UPDATE tasks SET status='running',resume_status=?,owner=?,fence=fence+1,
                lease_ns=?,lease_clock='monotonic_ns',owner_pid=?,owner_birth=?,attempts=attempts+1,error=NULL
                WHERE id=?""",
                             (status, self.owner, monotonic_ns() + self.lease_ns, pid, birth, identity))
            self._event(database, identity, "claimed")
            row = database.execute("SELECT * FROM tasks WHERE id=?", (identity,)).fetchone()
            database.commit()
        return self._decode(row)

    def _owned(self, database: sqlite3.Connection, task: dict[str, Any]) -> None:
        """Reject expired owners and stale results even if no replacement worker has claimed yet."""
        row = database.execute("SELECT owner,fence,status,lease_ns,lease_clock,owner_pid,owner_birth "
                               "FROM tasks WHERE id=? AND pool=?",
                               (task["_id"], self.task_pool)).fetchone()
        if (row is None or row["owner"] != task["owner"] or row["fence"] != task["fence"]
                or row["status"] != "running" or row["lease_clock"] != "monotonic_ns"
                or row["lease_ns"] <= monotonic_ns() or row["owner_pid"] != os.getpid()
                or not owner_alive(row["owner_pid"], row["owner_birth"])):
            raise LeaseLost(f"Task {task['_id']} no longer has this active fenced lease")

    def heartbeat(self, task: dict[str, Any]) -> None:
        """Renew an active lease; an expired lease cannot be revived by a late heartbeat."""
        with self.connection() as database:
            database.execute("BEGIN IMMEDIATE")
            self._owned(database, task)
            database.execute("UPDATE tasks SET lease_ns=? WHERE id=?", (monotonic_ns() + self.lease_ns, task["_id"]))
            database.commit()

    def commit_task_res(self, task: dict[str, Any], res: Any, status: str | None = "done") -> None:
        """Publish one accepted result and its reconciliation outbox entry under the same fenced claim."""
        status = status or "done"
        if status not in ("done", "part_done", "waiting"):
            raise ValueError("Task results must transition to done, part_done or waiting")
        result_sha = self.store.archive(pickle.dumps(res, protocol=5))
        with self.connection() as database:
            database.execute("BEGIN IMMEDIATE")
            self._owned(database, task)
            database.execute("""UPDATE tasks SET status=?,result_sha=?,owner=NULL,lease_ns=0,failures=0,error=NULL,
                not_before_ns=0,retry_pid=NULL,retry_birth=NULL
                WHERE id=?""", (status, result_sha, task["_id"]))
            self._event(database, task["_id"], "result_committed")
            database.execute("INSERT INTO outbox(task_id,fence,result_sha) VALUES (?,?,?)",
                             (task["_id"], task["fence"], result_sha))
            database.commit()

    def fail(self, task: dict[str, Any], error: BaseException, *, permanent: bool = False) -> None:
        """Schedule monotonic bounded exponential backoff unless the stage's attempt policy is exhausted."""
        with self.connection() as database:
            database.execute("BEGIN IMMEDIATE")
            self._owned(database, task)
            row = database.execute("SELECT failures,max_attempts,retry_base_ns,retry_cap_ns FROM tasks WHERE id=?",
                                   (task["_id"],)).fetchone()
            failure = row["failures"] + 1
            terminal = permanent or failure >= row["max_attempts"]
            delay = min(row["retry_cap_ns"], row["retry_base_ns"] * (2 ** min(failure - 1, 62)))
            deadline = monotonic_ns() + delay if not terminal and delay else 0
            pid = os.getpid() if deadline else None
            birth = process_birth(pid) if pid else None
            database.execute("""UPDATE tasks SET failures=failures+1,
                status=CASE WHEN ? OR failures+1>=max_attempts THEN 'failed' ELSE resume_status END,
                owner=NULL,lease_ns=0,error=?,not_before_ns=?,retry_pid=?,retry_birth=? WHERE id=?""",
                             (permanent, f"{type(error).__name__}: {error}"[:4096], deadline, pid, birth, task["_id"]))
            self._event(database, task["_id"], "failed_attempt")
            database.commit()

    def return_task(self, task: dict[str, Any], status: str | None = "waiting") -> None:
        """Return a current claim without changing its prior result, matching Qlib rollback semantics."""
        status = status or "waiting"
        if status not in ("waiting", "part_done"):
            raise ValueError("A returned claim must be waiting or part_done")
        with self.connection() as database:
            database.execute("BEGIN IMMEDIATE")
            self._owned(database, task)
            database.execute("""UPDATE tasks SET status=?,owner=NULL,lease_ns=0,not_before_ns=0,
                retry_pid=NULL,retry_birth=NULL WHERE id=?""", (status, task["_id"]))
            self._event(database, task["_id"], "returned")
            database.commit()

    @contextmanager
    def safe_fetch_task(self, query: dict[str, Any] | None = None,
                        status: str = "waiting") -> Iterator[dict[str, Any] | None]:
        """Restore the original stage on callback exceptions, preserving native context-manager behavior."""
        task = self.fetch_task(query, status)
        try:
            yield task
        except (Exception, KeyboardInterrupt):
            if task is not None:
                try:
                    self.return_task(task, status)
                except LeaseLost:
                    pass
            raise

    def query(self, query: dict[str, Any] | None = None, decode: bool = True) -> Iterator[dict[str, Any]]:
        """Read a stable task list with the documented query subset; decode=False retains serialized fields."""
        with self.connection() as database:
            rows = database.execute("SELECT * FROM tasks WHERE pool=? ORDER BY rowid",
                                    (self.task_pool,)).fetchall()
        for row in rows:
            decoded = self._decode(row)
            if decoded["status"] != "removed" and matches(decoded, query or {}):
                yield decoded if decode else dict(row)

    def re_query(self, _id: str) -> dict[str, Any]:
        """Read exactly one task and fail explicitly when its pool or identity is unknown."""
        value = next(self.query({"_id": _id}), None)
        if value is None:
            raise KeyError(_id)
        return value

    def task_stat(self, query: dict[str, Any] | None = None) -> dict[str, int]:
        """Count task states, including terminal failed and cancelled tasks."""
        result: dict[str, int] = {}
        for task in self.query(query):
            result[task["status"]] = result.get(task["status"], 0) + 1
        return result

    def prioritize(self, task: dict[str, Any], priority: int) -> None:
        """Update scheduling priority without disturbing a running worker's fenced ownership."""
        with self.connection() as database:
            database.execute("BEGIN IMMEDIATE")
            database.execute("UPDATE tasks SET priority=? WHERE id=? AND pool=?",
                             (int(priority), task["_id"], self.task_pool))
            self._event(database, task["_id"], "prioritized")
            database.commit()

    def reset_status(self, query: dict[str, Any], status: str) -> int:
        """Explicitly reset or cancel matching tasks, invalidating any existing worker claim."""
        if status not in ("waiting", "part_done", "cancelled", "removed"):
            raise ValueError("Administrative reset accepts waiting, part_done, cancelled or removed")
        count = 0
        with self.connection() as database:
            database.execute("BEGIN IMMEDIATE")
            for row in database.execute("SELECT * FROM tasks WHERE pool=?", (self.task_pool,)).fetchall():
                if matches(self._decode(row), query):
                    if status == "part_done" and not row["result_sha"]:
                        raise ValueError("A part_done reset requires a saved intermediate result")
                    database.execute("""UPDATE tasks SET status=?,owner=NULL,lease_ns=0,fence=fence+1,
                        failures=0,not_before_ns=0,retry_pid=NULL,retry_birth=NULL WHERE id=?""", (status, row["id"]))
                    if status == "removed":
                        database.execute("UPDATE tasks SET fingerprint=? WHERE id=?",
                                         ("removed_" + uuid4().hex, row["id"]))
                    self._event(database, row["id"], "administrative_reset")
                    count += 1
            database.commit()
        return count

    def reset_waiting(self, query: dict[str, Any] | None = None) -> int:
        """Explicitly return running tasks to waiting and fence any surviving worker."""
        return self.reset_status({"status": "running", **(query or {})}, "waiting")

    def remove(self, query: dict[str, Any] | None = None) -> int:
        """Tombstone tasks while preserving immutable execution and accepted-result history."""
        return self.reset_status(query or {}, "removed")

    def task_fetcher_iter(self, query: dict[str, Any] | None = None) -> Iterator[dict[str, Any]]:
        """Yield each available native-style claim and roll it back when consumer execution raises."""
        while True:
            with self.safe_fetch_task(query) as task:
                if task is None:
                    return
                yield task

    def wait(self, query: dict[str, Any] | None = None, *, timeout: float = 300,
             terminal_statuses: tuple[str, ...] = ("done",)) -> None:
        """Wait boundedly, recovering expired claims and surfacing terminal failures instead of hanging."""
        deadline = monotonic() + timeout
        while True:
            with self.connection() as database:
                database.execute("BEGIN IMMEDIATE")
                self._recover(database)
                database.commit()
            states = self.task_stat(query)
            if states.get("failed", 0) or states.get("cancelled", 0):
                raise TaskFailure(f"Task pool contains terminal failures: {states}")
            if all(status in terminal_statuses for status in states):
                return
            if monotonic() >= deadline:
                raise TimeoutError(f"Tasks did not finish before deadline: {states}")
            sleep(.05)

    def export_history(self) -> str | None:
        """Publish immutable full task history and result payloads; reconcile interrupted exports idempotently."""
        dataset = "tasks_" + sha256(self.task_pool.encode()).hexdigest()[:24]
        with self.connection() as database:
            rows = database.execute("SELECT * FROM events WHERE pool=? ORDER BY sequence",
                                    (self.task_pool,)).fetchall()
        if not rows:
            return None
        sequence = rows[-1]["sequence"]
        values = []
        for row in rows:
            value = dict(row)
            result_sha = value["result_sha"]
            payload = (self.store.root / "raw" / result_sha).read_bytes() if result_sha else None
            if payload is not None and sha256(payload).hexdigest() != result_sha:
                raise ValueError("Task result bytes failed integrity verification during export")
            value["result_payload"] = payload
            values.append(value)
        schema = pa.schema([("sequence", pa.int64()), ("pool", pa.string()), ("task_id", pa.string()),
                            ("status", pa.string()), ("kind", pa.string()), ("attempt", pa.int64()),
                            ("fence", pa.int64()), ("owner", pa.string()), ("occurred_ns", pa.int64()),
                            ("error", pa.string()), ("result_sha", pa.string()), ("result_payload", pa.binary())])
        table = pa.Table.from_pylist(values, schema=schema)
        for unused in range(8):
            head = self.store.head(dataset)
            if head and self.store.snapshot(head).metadata["sequence"] >= sequence:
                self.store.read(head)
                identity = head
                break
            try:
                snapshot = self.store.publish(dataset, table, {"sequence": sequence, "pool": self.task_pool,
                    "schema_version": 1, "real_orders_enabled": False}, expected_head=head)
                identity = snapshot.identity
                break
            except SnapshotConflict:
                continue
        else:
            raise SnapshotConflict("Task history publication remained contended")
        with self.connection() as database:
            database.execute("""INSERT INTO exports(pool,sequence,snapshot) VALUES (?,?,?)
                ON CONFLICT(pool) DO UPDATE SET sequence=excluded.sequence,snapshot=excluded.snapshot
                WHERE exports.sequence<excluded.sequence""", (self.task_pool, sequence, identity))
        return identity

    def reconcile(self, *, lock_timeout: float = 30) -> dict[str, Any]:
        """Serialize outbox reads and recorder writes so older stages cannot overwrite newer accepted lineage."""
        lock = self.root / ("reconcile-" + sha256(self.task_pool.encode()).hexdigest() + ".lock")
        with reconciliation_lock(lock, lock_timeout):
            return self._reconcile_owned()

    def _reconcile_owned(self) -> dict[str, Any]:
        """Reconcile one fresh outbox view while holding only the pool's process lock during external I/O."""
        with self.connection() as database:
            rows = database.execute("""SELECT o.*,t.definition,t.fence AS current_fence,
                t.result_sha AS current_result_sha FROM outbox o JOIN tasks t ON t.id=o.task_id
                WHERE t.pool=? AND o.state='pending' ORDER BY o.task_id,o.fence""", (self.task_pool,)).fetchall()
        errors = []
        for row in rows:
            try:
                if row["current_fence"] != row["fence"] or row["current_result_sha"] != row["result_sha"]:
                    with self.connection() as database:
                        database.execute("UPDATE outbox SET state='superseded' WHERE task_id=? AND fence=?",
                                         (row["task_id"], row["fence"]))
                    continue
                payload = (self.store.root / "raw" / row["result_sha"]).read_bytes()
                if sha256(payload).hexdigest() != row["result_sha"]:
                    raise ValueError("Outbox result archive is corrupt")
                result = pickle.loads(payload)
                if hasattr(result, "set_tags"):
                    definition = pickle.loads(row["definition"])
                    lineage = definition.get("joint_task", {})
                    keys = ("snapshot_id", "snapshot_sha256", "source_sha256", "feature_sha256",
                            "code_sha256", "lock_sha256")
                    tags = {f"joint_{key}": lineage[key] for key in keys if key in lineage}
                    result.set_tags(joint_task_id=row["task_id"], joint_task_fence=row["fence"],
                                    joint_result_sha256=row["result_sha"], joint_result_accepted="true", **tags)
                with self.connection() as database:
                    database.execute("UPDATE outbox SET state='done',error=NULL WHERE task_id=? AND fence=?",
                                     (row["task_id"], row["fence"]))
            except Exception as error:
                errors.append({"task_id": row["task_id"], "error": str(error)})
                with self.connection() as database:
                    database.execute("UPDATE outbox SET error=? WHERE task_id=? AND fence=?",
                                     (str(error)[:4096], row["task_id"], row["fence"]))
        return {"snapshot": self.export_history(), "pending_errors": errors}
