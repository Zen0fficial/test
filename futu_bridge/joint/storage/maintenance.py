"""Verified scans, lossless compaction, explicit rollback and conservative retention.

All committed generations are retained. Cleanup considers only unreferenced files
older than its grace period; pinned experiments therefore never lose input data.
"""

from __future__ import annotations

from pathlib import Path
import time
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from .snapshots import Snapshot, SnapshotConflict, SnapshotStore, file_hash


def scan(store: SnapshotStore, identity: str, *, columns: list[str] | None = None,
         filters: Any = None) -> pa.Table:
    """Use Parquet row-group predicates after a complete file hash/schema integrity check.

    Integrity hashing reads the full compressed file. Predicate pruning reduces
    decompression/materialization, not the bytes needed for integrity validation.
    """
    snapshot = store.snapshot(identity)
    path = store.data_path(snapshot)
    if file_hash(path) != snapshot.sha256:
        raise ValueError("Cannot scan a corrupted snapshot")
    with store.connection() as database:
        schema = database.execute("SELECT arrow_schema FROM snapshots WHERE identity=?", (identity,)).fetchone()[0]
    parquet = pq.ParquetFile(path)
    if (parquet.metadata.num_rows != snapshot.rows
            or not parquet.schema_arrow.equals(pa.ipc.read_schema(pa.BufferReader(schema)), check_metadata=True)):
        raise ValueError("Snapshot metadata mismatch")
    return pq.read_table(path, columns=columns, filters=filters)


def compact(store: SnapshotStore, identity: str, *, row_group_size: int = 128_000,
            compression: str = "zstd") -> Snapshot:
    """Repack the same rows into a new generation; the old pinned snapshot remains readable."""
    old = store.snapshot(identity)
    return store.publish(old.dataset, store.read(identity),
                         {**old.metadata, "compacted_from": identity,
                          "layout": {"row_group_size": row_group_size, "compression": compression}},
                         expected_head=identity, compression=compression, row_group_size=row_group_size)


def rollback(store: SnapshotStore, dataset: str, identity: str, *, expected_head: str) -> None:
    """Select a verified generation and invalidate replay cursors in the same transaction.

    A cursor may describe observations absent from the older generation. The
    next replay therefore starts at zero and idempotently restores its complete
    acknowledged journal prefix; no acknowledged middle segment can be skipped.
    """
    target = store.snapshot(identity)
    if target.dataset != dataset:
        raise ValueError("Rollback target belongs to a different dataset")
    store.read(identity)
    with store.connection() as database:
        database.execute("BEGIN IMMEDIATE")
        row = database.execute("SELECT identity FROM heads WHERE dataset=?", (dataset,)).fetchone()
        if row is None or row[0] != expected_head:
            raise SnapshotConflict("Head changed before rollback")
        database.execute("UPDATE heads SET identity=? WHERE dataset=?", (identity, dataset))
        cursors = database.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                                   "AND name='replay_cursors'").fetchone()
        if cursors is not None:
            database.execute("DELETE FROM replay_cursors WHERE dataset=?", (dataset,))
        database.commit()


def cleanup_candidates(store: SnapshotStore, grace_seconds: float = 86_400) -> list[str]:
    """List old unpublished files for operator review without removing any evidence.

    The coordinator cannot know whether a slow writer still owns an orphan file.
    Automatic deletion therefore remains disabled until writer fencing is added
    to file staging; a grace interval by itself cannot prove a writer is dead.
    """
    if grace_seconds < 0:
        raise ValueError("Retention grace must be nonnegative")
    report = store.inspect()
    candidates = [store.root / name for name in report["orphan_files"]]
    candidates += [store.root / "staging" / name for name in report["staged_files"]]
    threshold = time.time() - grace_seconds
    return [path.relative_to(store.root).as_posix() for path in candidates if path.stat().st_mtime < threshold]


def archive_document(store: SnapshotStore, dataset: str, path: Path, *, document_id: str,
                     observed_ns: int, media_type: str, expected_head: str | None) -> Snapshot:
    """Version original document bytes by actual observation time, without guessing publication dates."""
    from .incremental import VersionContract, merge

    if not document_id or observed_ns <= 0 or not media_type:
        raise ValueError("Document identity, observed time and media type are required")
    payload = path.read_bytes()
    digest = store.archive(payload)
    schema = pa.schema([("document_id", pa.string()), ("observed_ns", pa.int64()),
                        ("media_type", pa.string()), ("content_sha256", pa.string()), ("bytes", pa.int64())],
                       metadata={b"joint_schema": b"original_document_v1"})
    table = pa.Table.from_pylist([{"document_id": document_id, "observed_ns": observed_ns,
                                  "media_type": media_type, "content_sha256": digest, "bytes": len(payload)}],
                                schema=schema)
    return merge(store, dataset, table, VersionContract(("document_id",), "observed_ns",
                                                       ("media_type", "content_sha256", "bytes")),
                 expected_head=expected_head)
