"""Quiescent task/control-history backups with explicit external recorder dependencies.

Native MLflow databases and artifact directories remain separately owned. This is
not a portable whole-system backup: restoration verifies that the same referenced
local recorders and exact artifact bytes still exist at their original locations.
Only trusted local task pickle payloads may be inspected by this operator tool.
"""

from __future__ import annotations

from contextlib import closing, ExitStack
from hashlib import sha256
import json
from pathlib import Path
import pickle
import shutil
import sqlite3
from typing import Any
from urllib.parse import unquote, urlsplit
from urllib.request import url2pathname

import pyarrow as pa
import pyarrow.parquet as pq

from ..storage import SnapshotStore
from ..storage.snapshots import file_hash
from .leases import reconciliation_lock


def database_rows(database: sqlite3.Connection) -> dict[str, Any]:
    """Fingerprint every task table and exact BLOB without executing its serialized Python payloads."""
    tables = [row[0] for row in database.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    result = {}
    for table in tables:
        quoted = '"' + table.replace('"', '""') + '"'
        rows = database.execute("SELECT * FROM " + quoted + " ORDER BY rowid").fetchall()
        values = [[["bytes", value.hex()] if isinstance(value, bytes) else [type(value).__name__, value]
                   for value in row] for row in rows]
        result[table] = {"rows": len(rows), "sha256": sha256(json.dumps(values, separators=(",", ":"),
                                                                     ensure_ascii=False).encode()).hexdigest()}
    result["schema_version"] = database.execute("PRAGMA user_version").fetchone()[0]
    return result


def check_quiescent(database: sqlite3.Connection) -> None:
    """Reject active claims instead of copying a snapshot that could revive an old worker."""
    if database.execute("PRAGMA user_version").fetchone()[0] != 3:
        raise ValueError("Task backup requires the current schema3 coordinator")
    if database.execute("SELECT COUNT(*) FROM tasks WHERE status='running' OR owner IS NOT NULL "
                        "OR lease_ns!=0").fetchone()[0]:
        raise ValueError("Task backup requires quiescence: running or owned tasks remain")


def pools(database: sqlite3.Connection) -> list[str]:
    """Include tombstoned pools so a pending historical outbox cannot bypass backup locking."""
    return [row[0] for row in database.execute("SELECT DISTINCT pool FROM tasks ORDER BY pool")]


def local_artifacts(uri: str) -> Path:
    """Resolve only original local file artifact roots; remote stores need another backup implementation."""
    parsed = urlsplit(uri)
    if parsed.scheme != "file" or parsed.netloc not in ("", "localhost"):
        raise ValueError("Task backup supports only existing local MLflow artifact directories")
    result = Path(url2pathname(unquote(parsed.path))).resolve()
    if not result.is_dir():
        raise FileNotFoundError("Required native MLflow artifact directory is absent")
    return result


def recorder_reference(recorder: Any) -> dict[str, Any]:
    """Read native recorder identity directly from its existing SQLite store, without migrating that store."""
    uri = recorder.uri
    if not isinstance(uri, str) or not uri.startswith("sqlite:///"):
        raise ValueError("Task backup requires an existing local SQLite MLflow recorder backend")
    path = Path(uri[len("sqlite:///"):]).resolve()
    if not path.is_file():
        raise FileNotFoundError("Required native MLflow tracking database is absent")
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=10)) as database:
        row = database.execute("SELECT experiment_id,artifact_uri FROM runs WHERE run_uuid=?",
                               (recorder.id,)).fetchone()
    if row is None or str(row[0]) != str(recorder.experiment_id):
        raise ValueError("Required native MLflow recorder identity does not exist")
    root = local_artifacts(row[1])
    artifacts = {}
    for artifact in sorted(root.rglob("*")):
        if artifact.is_file():
            if not artifact.resolve().is_relative_to(root):
                raise ValueError("Native artifact link escapes its original local root")
            artifacts[artifact.relative_to(root).as_posix()] = file_hash(artifact)
    return {"tracking_uri": uri, "run_id": recorder.id, "experiment_id": str(recorder.experiment_id),
            "artifact_uri": row[1], "artifacts": artifacts}


def recorders(value: Any, seen: set[int] | None = None) -> list[Any]:
    """Find native recorder objects inside supported trusted-local result containers."""
    from qlib.workflow.recorder import MLflowRecorder

    seen = set() if seen is None else seen
    if id(value) in seen:
        return []
    seen.add(id(value))
    if isinstance(value, MLflowRecorder):
        return [value]
    nested = value.values() if isinstance(value, dict) else value if isinstance(value, (list, tuple)) else ()
    return [recorder for child in nested for recorder in recorders(child, seen)]


def external_references(root: Path, database: sqlite3.Connection) -> list[dict[str, Any]]:
    """Verify every accepted/intermediate result archive and collect native recorder dependencies."""
    digests = database.execute("SELECT result_sha FROM tasks WHERE result_sha IS NOT NULL UNION "
                               "SELECT result_sha FROM events WHERE result_sha IS NOT NULL UNION "
                               "SELECT result_sha FROM outbox WHERE result_sha IS NOT NULL").fetchall()
    references = {}
    for (digest,) in digests:
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise ValueError("Malformed task result archive identity")
        path = root / "history/raw" / digest
        if not path.is_file() or file_hash(path) != digest:
            raise ValueError("Required accepted task result archive is missing or corrupt")
        for recorder in recorders(pickle.loads(path.read_bytes())):
            key = (recorder.uri, recorder.id)
            if key not in references:
                references[key] = recorder_reference(recorder)
    return [references[key] for key in sorted(references)]


def history_integrity(root: Path) -> dict[str, Any]:
    """Verify an immutable history backup without opening the initializing/writing store constructor."""
    path = root / "control.sqlite"
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as database:
        database.row_factory = sqlite3.Row
        if (database.execute("PRAGMA integrity_check").fetchone()[0] != "ok"
                or database.execute("PRAGMA foreign_key_check").fetchall()):
            raise ValueError("History backup catalogue failed integrity verification")
        rows = database.execute("SELECT * FROM snapshots").fetchall()
    known = set()
    for row in rows:
        data = (root / row["relative_path"]).resolve()
        if not data.is_relative_to(root / "parquet") or not data.is_file() or file_hash(data) != row["sha256"]:
            raise ValueError("History backup has missing or corrupt committed Parquet data")
        parquet = pq.ParquetFile(data)
        if (parquet.metadata.num_rows != row["rows"] or not parquet.schema_arrow.equals(
                pa.ipc.read_schema(pa.BufferReader(row["arrow_schema"])), check_metadata=True)):
            raise ValueError("History backup Parquet schema/count differs from its catalogue")
        known.add(row["relative_path"])
    for raw in (root / "raw").iterdir():
        if not raw.is_file() or file_hash(raw) != raw.name:
            raise ValueError("History backup raw archive failed hash verification")
    files = {p.relative_to(root).as_posix() for p in (root / "parquet").rglob("*.parquet")}
    return {"passed": True, "committed_snapshots": len(rows), "orphan_files": sorted(files - known)}


def verify_backup(root: Path) -> dict[str, Any]:
    """Fail closed on changed task/history bytes or missing/changed original MLflow references."""
    root = root.resolve()
    manifest = json.loads((root / "backup_manifest.json").read_text(encoding="utf-8"))
    if manifest.get("backup_schema") != 1:
        raise ValueError("Unsupported task backup manifest")
    for relative, digest in manifest["files"].items():
        path = (root / relative).resolve()
        if not path.is_relative_to(root) or not path.is_file() or file_hash(path) != digest:
            raise ValueError("Task backup file is missing, outside its root or corrupt: " + relative)
    with closing(sqlite3.connect((root / "tasks.sqlite").as_uri() + "?mode=ro", uri=True)) as database:
        if database.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Task backup SQLite integrity check failed")
        check_quiescent(database)
        if database_rows(database) != manifest["task_tables"]:
            raise ValueError("Task backup logical rows changed")
        references = external_references(root, database)
    if references != manifest["external_recorders"]:
        raise ValueError("Required original MLflow recorder/artifact references changed after task backup")
    history = history_integrity(root / "history")
    return {"passed": True, "task_tables": manifest["task_tables"], "history": history,
            "native_recorders_verified": len(references), "external_recorders_copied": False,
            "real_orders_enabled": False}


def new_destination(source: Path, destination: Path) -> Path:
    """Require a new external destination, preserving every previous task root and backup."""
    target = destination.resolve()
    if target == source or target.is_relative_to(source) or source.is_relative_to(target):
        raise ValueError("Task backup/restore destination must be outside its source root")
    if target.exists():
        raise FileExistsError("Task backup/restore destination must be new")
    return target


def backup_tasks(root: Path, destination: Path, *, lock_timeout: float = 30) -> dict[str, Any]:
    """Reserve an inactive task cut and back up its coordinator/history, retaining external recorder locations."""
    root = root.resolve()
    path = root / "tasks.sqlite"
    if not path.is_file():
        raise FileNotFoundError("No existing task coordinator to back up")
    target = new_destination(root, destination)
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=10)) as reader:
        selected_pools = pools(reader)
    with ExitStack() as locks, closing(sqlite3.connect(path, timeout=10, isolation_level=None)) as reservation:
        for pool in selected_pools:
            name = "reconcile-" + sha256(pool.encode()).hexdigest() + ".lock"
            locks.enter_context(reconciliation_lock(root / name, lock_timeout))
        reservation.execute("BEGIN IMMEDIATE")
        try:
            if pools(reservation) != selected_pools:
                raise ValueError("Task pool set changed during backup locking; retry a quiescent backup")
            check_quiescent(reservation)
            logical = database_rows(reservation)
            references = external_references(root, reservation)
            target.mkdir(parents=True, exist_ok=False)
            # A separate reader can copy the committed WAL snapshot while this
            # connection reserves writes. Backing up the write connection itself can busy-loop.
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=10)) as reader, \
                    closing(sqlite3.connect(target / "tasks.sqlite")) as destination_database:
                reader.backup(destination_database)
            SnapshotStore(root / "history").backup(target / "history")
            files = {p.relative_to(target).as_posix(): file_hash(p) for p in sorted(target.rglob("*"))
                     if p.is_file() and not p.name.endswith(("-wal", "-shm"))}
            manifest = {"backup_schema": 1, "source_root": str(root), "files": files,
                        "task_tables": logical, "external_recorders": references,
                        "source_hashes": {name: file_hash(Path(__file__).with_name(name))
                                          for name in ("backup.py", "manager.py", "leases.py", "__main__.py")},
                        "scope": "tasks.sqlite and history only; native MLflow remains at original URIs",
                        "quiescent": True, "claims_invalidated": 0, "real_orders_enabled": False}
            (target / "backup_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            result = verify_backup(target)
        finally:
            reservation.rollback()
    return {**result, "destination": str(target), "manifest_sha256": file_hash(target / "backup_manifest.json")}


def restore_tasks(backup: Path, destination: Path) -> dict[str, Any]:
    """Restore a verified inactive task root only while all original native recorder dependencies remain valid."""
    source = backup.resolve()
    target = new_destination(source, destination)
    verify_backup(source)
    manifest = json.loads((source / "backup_manifest.json").read_text(encoding="utf-8"))
    target.mkdir(parents=True, exist_ok=False)
    for relative in [*manifest["files"], "backup_manifest.json"]:
        path = target / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / relative, path)
    result = verify_backup(target)
    return {**result, "destination": str(target), "restored_claims": 0,
            "external_dependency_policy": "original local MLflow database and exact artifact bytes required"}
