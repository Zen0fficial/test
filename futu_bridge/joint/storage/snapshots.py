"""Publish complete Parquet snapshots without exposing uncommitted or partial files.

SQLite owns snapshot membership and current pointers; analytical rows remain in
Parquet. Files are immutable and finalized before the catalogue transaction.
An interrupted publication can leave an orphan, never a partially visible head.
This initial store deliberately publishes complete snapshots, not row upserts.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
from time import time_ns
from typing import Iterator
from uuid import uuid4

import pyarrow as pa
import pyarrow.parquet as pq


class SnapshotConflict(RuntimeError):
    """Reject publication against a head that another writer has already replaced."""


@dataclass(frozen=True)
class Snapshot:
    """Immutable published identity and the provenance needed to reproduce a read."""

    identity: str
    dataset: str
    parent: str | None
    relative_path: str
    sha256: str
    rows: int
    created_ns: int
    metadata: dict[str, object]


def file_hash(path: Path) -> str:
    """Hash large data files incrementally without loading the archive into memory."""
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def dataset_name(value: str) -> str:
    """Limit logical dataset names to safe stable identifiers, independent of paths."""
    if not isinstance(value, str) or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", value) is None:
        raise ValueError("Dataset names require 1-64 lowercase ASCII letters, digits or underscores")
    return value


def equivalent(left: object, right: object) -> bool:
    """Compare decoded Arrow values while preserving nulls and accepting matching NaN positions."""
    if isinstance(left, float) and math.isnan(left):
        return isinstance(right, float) and math.isnan(right)
    if isinstance(left, dict):
        return (isinstance(right, dict) and left.keys() == right.keys()
                and all(equivalent(value, right[key]) for key, value in left.items()))
    if isinstance(left, (tuple, list)):
        return (isinstance(right, type(left)) and len(left) == len(right)
                and all(equivalent(a, b) for a, b in zip(left, right)))
    return left == right


class SnapshotStore:
    """Store immutable Arrow-compatible data on one host using the existing shared runtime."""

    def __init__(self, root: Path) -> None:
        """Create a versioned local catalogue without touching any provider or legacy dataset."""
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        for name in ("parquet", "staging", "raw"):
            (self.root / name).mkdir(exist_ok=True)
        self.database = self.root / "control.sqlite"
        with self.connection() as database:
            version = database.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise ValueError("Unsupported snapshot catalogue version")
            database.execute("PRAGMA journal_mode=WAL")
            database.executescript("""
                CREATE TABLE IF NOT EXISTS snapshots (
                    identity TEXT PRIMARY KEY, dataset TEXT NOT NULL, parent TEXT REFERENCES snapshots(identity),
                    relative_path TEXT NOT NULL UNIQUE, sha256 TEXT NOT NULL, rows INTEGER NOT NULL,
                    created_ns INTEGER NOT NULL, metadata TEXT NOT NULL, arrow_schema BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS heads (
                    dataset TEXT PRIMARY KEY, identity TEXT NOT NULL REFERENCES snapshots(identity));
                PRAGMA user_version=1;
            """)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """Use bounded lock waits and close connections deterministically on Windows."""
        database = sqlite3.connect(self.database, timeout=10, isolation_level=None)
        database.row_factory = sqlite3.Row
        try:
            database.execute("PRAGMA foreign_keys=ON")
            database.execute("PRAGMA synchronous=FULL")
            yield database
        finally:
            if database.in_transaction:
                database.rollback()
            database.close()

    def head(self, dataset: str) -> str | None:
        """Return a committed snapshot identifier without reading market-data files."""
        with self.connection() as database:
            row = database.execute("SELECT identity FROM heads WHERE dataset=?", (dataset_name(dataset),)).fetchone()
        return row[0] if row else None

    def snapshot(self, identity: str) -> Snapshot:
        """Resolve only committed IDs; staged or orphaned files have no readable snapshot identity."""
        with self.connection() as database:
            row = database.execute("SELECT * FROM snapshots WHERE identity=?", (identity,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown committed snapshot: {identity}")
        return Snapshot(row["identity"], row["dataset"], row["parent"], row["relative_path"], row["sha256"],
                        row["rows"], row["created_ns"], json.loads(row["metadata"]))

    def data_path(self, snapshot: Snapshot) -> Path:
        """Reject catalogue paths outside this store, including escaping symbolic links."""
        path = (self.root / snapshot.relative_path).resolve()
        if not path.is_relative_to(self.root / "parquet") or path.suffix != ".parquet":
            raise ValueError("Snapshot path escapes the analytical store")
        return path

    def archive(self, payload: bytes) -> str:
        """Retain exact original bytes by SHA-256, detecting corruption instead of overwriting it."""
        identity = hashlib.sha256(payload).hexdigest()
        final = self.root / "raw" / identity
        if final.exists():
            if file_hash(final) != identity:
                raise ValueError("Existing original-data archive is corrupt")
            return identity
        temporary = self.root / "staging" / (uuid4().hex + ".raw")
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(final)
        return identity

    def publish(self, dataset: str, table: pa.Table, metadata: dict[str, object], *,
                expected_head: str | None, allow_schema_change: bool = False,
                compression: str = "zstd", row_group_size: int = 128_000) -> Snapshot:
        """Finalize a complete immutable table, then atomically compare and replace its current pointer."""
        dataset_name(dataset)
        if compression not in ("zstd", "snappy", "none") or row_group_size < 1:
            raise ValueError("Unsupported Parquet compression or row group size")
        if not isinstance(table, pa.Table) or len(set(table.column_names)) != table.num_columns:
            raise ValueError("Snapshots require an Arrow table with unique column names")
        if not isinstance(metadata, dict):
            raise ValueError("Snapshot provenance must be an object")
        table.validate(full=True)
        encoded = json.dumps(metadata, sort_keys=True, allow_nan=False)
        identity = uuid4().hex
        temporary = self.root / "staging" / (identity + ".parquet")
        folder = self.root / "parquet" / dataset
        folder.mkdir(exist_ok=True)
        final = folder / (identity + ".parquet")
        pq.write_table(table, temporary, compression=compression, row_group_size=row_group_size, version="2.6")
        restored = pq.read_table(temporary)
        matches = restored.equals(table, check_metadata=True)
        if not matches and restored.schema.equals(table.schema, check_metadata=True):
            matches = equivalent(restored.to_pydict(), table.to_pydict())
        if not matches:
            raise ValueError("Parquet round trip changed Arrow values, types or schema metadata")
        with temporary.open("r+b") as stream:
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(final)
        result = Snapshot(identity, dataset, expected_head, final.relative_to(self.root).as_posix(), file_hash(final),
                          table.num_rows, time_ns(), json.loads(encoded))
        self._commit(result, table.schema, allow_schema_change)
        return result

    def _commit(self, snapshot: Snapshot, schema: pa.Schema, allow_schema_change: bool) -> None:
        """Serialize competing publishers and commit membership with its pointer in one transaction."""
        with self.connection() as database:
            database.execute("BEGIN IMMEDIATE")
            current = database.execute("SELECT identity FROM heads WHERE dataset=?", (snapshot.dataset,)).fetchone()
            if (current[0] if current else None) != snapshot.parent:
                raise SnapshotConflict("Dataset head changed; publish again against an explicitly chosen snapshot")
            if current and not allow_schema_change:
                previous = database.execute("SELECT arrow_schema FROM snapshots WHERE identity=?", current).fetchone()
                old_schema = pa.ipc.read_schema(pa.BufferReader(previous[0]))
                if not old_schema.equals(schema, check_metadata=True):
                    raise ValueError("Schema change requires an explicit versioned migration")
            database.execute("INSERT INTO snapshots VALUES (?,?,?,?,?,?,?,?,?)", (
                snapshot.identity, snapshot.dataset, snapshot.parent, snapshot.relative_path, snapshot.sha256,
                snapshot.rows, snapshot.created_ns, json.dumps(snapshot.metadata, allow_nan=False),
                schema.serialize().to_pybytes()))
            database.execute("INSERT INTO heads VALUES (?,?) "
                             "ON CONFLICT(dataset) DO UPDATE SET identity=excluded.identity",
                             (snapshot.dataset, snapshot.identity))
            database.commit()

    def read(self, identity: str, columns: list[str] | None = None) -> pa.Table:
        """Read a pinned snapshot after integrity validation, with optional column projection."""
        snapshot = self.snapshot(identity)
        path = self.data_path(snapshot)
        if file_hash(path) != snapshot.sha256:
            raise ValueError("Committed Parquet file failed its content hash")
        with self.connection() as database:
            schema = database.execute("SELECT arrow_schema FROM snapshots WHERE identity=?", (identity,)).fetchone()[0]
        actual = pq.read_schema(path)
        if not actual.equals(pa.ipc.read_schema(pa.BufferReader(schema)), check_metadata=True):
            raise ValueError("Committed Parquet schema differs from its catalogue")
        result = pq.read_table(path, columns=columns)
        if result.num_rows != snapshot.rows:
            raise ValueError("Committed Parquet row count differs from its catalogue")
        return result

    def inspect(self) -> dict[str, object]:
        """Audit committed files and list incomplete publications without deleting evidence."""
        with self.connection() as database:
            records = database.execute("SELECT identity,relative_path FROM snapshots ORDER BY created_ns").fetchall()
        errors = {}
        for record in records:
            try:
                self.read(record["identity"])
            except (ValueError, OSError, pa.ArrowException) as error:
                errors[record["identity"]] = str(error)
        known = {row["relative_path"] for row in records}
        raw_errors = {p.name: "Archive content hash mismatch" for p in (self.root / "raw").iterdir()
                      if p.is_file() and file_hash(p) != p.name}
        files = {p.relative_to(self.root).as_posix() for p in (self.root / "parquet").rglob("*.parquet")}
        return {"passed": not errors and not raw_errors, "committed_snapshots": len(records),
                "integrity_errors": errors, "raw_integrity_errors": raw_errors,
                "orphan_files": sorted(files - known),
                "staged_files": sorted(p.name for p in (self.root / "staging").iterdir())}

    def backup(self, destination: Path) -> dict[str, object]:
        """Copy a consistent SQLite snapshot and immutable files into a new independently verified root."""
        target = destination.resolve()
        if target == self.root or target.is_relative_to(self.root) or self.root.is_relative_to(target):
            raise ValueError("Backup destination must be outside the source store")
        target.mkdir(parents=True, exist_ok=False)
        with self.connection() as source:
            copy = sqlite3.connect(target / "control.sqlite")
            try:
                source.backup(copy)
            finally:
                copy.close()
        restored = SnapshotStore(target)
        with restored.connection() as database:
            rows = database.execute("SELECT identity FROM snapshots").fetchall()
        for row in rows:
            snapshot = restored.snapshot(row[0])
            path = restored.data_path(snapshot)
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(self.data_path(snapshot), path)
        for raw in (self.root / "raw").iterdir():
            if file_hash(raw) != raw.name:
                raise ValueError("Original archive failed backup integrity verification")
            shutil.copyfile(raw, target / "raw" / raw.name)
        report = restored.inspect()
        if not report["passed"]:
            raise ValueError("Backup failed independent snapshot verification")
        return report
