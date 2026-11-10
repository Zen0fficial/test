"""Single-writer durable capture journal with hash-chain validation and crash recovery.

The OS releases the writer lock on process death. File fsync precedes SQLite's
durable watermark. Recovery admits a complete verified suffix and quarantines an
unacknowledged incomplete tail; a damaged acknowledged prefix always fails closed.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
from typing import Any
from uuid import uuid4


def encode(value: Any) -> bytes:
    """Encode captured JSON deterministically, rejecting nonfinite values."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":")).encode("utf-8")


class CaptureJournal:
    """Bound memory and acknowledge only flushed messages, with one local writer."""

    def __init__(self, root: Path, *, batch_size: int = 10_000, interval: float = 1.0) -> None:
        """Acquire a process-scoped writer lock and recover the last verified sequence."""
        if batch_size < 1 or interval <= 0:
            raise ValueError("Journal flush limits must be positive")
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = (self.root / "writer.lock").open("a+b")
        self.database: sqlite3.Connection | None = None
        self.stream: Any = None
        self.failed = False
        try:
            self.lock.seek(0, 2)
            if self.lock.tell() == 0:
                self.lock.write(b"0")
                self.lock.flush()
            self.lock.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.database = sqlite3.connect(self.root / "journal.sqlite", isolation_level=None, timeout=10)
            self.database.execute("PRAGMA synchronous=FULL")
            self.database.execute("PRAGMA journal_mode=WAL")
            self.database.execute("CREATE TABLE IF NOT EXISTS watermark "
                                  "(id INTEGER PRIMARY KEY CHECK(id=1), sequence INTEGER, offset INTEGER, hash TEXT)")
            self.database.execute("INSERT OR IGNORE INTO watermark VALUES (1,0,0,'')")
            self.path = self.root / "messages.jsonl"
            self.stream = self.path.open("a+b")
            self.sequence, self.offset, self.digest = self.recover()
            self.durable_sequence = self.sequence
            self.batch_size, self.interval, self.last_flush = batch_size, interval, time.monotonic()
        except BaseException:
            self._release()
            raise

    def recover(self) -> tuple[int, int, str]:
        """Verify every complete record and preserve an interrupted final fragment for inspection."""
        committed, committed_offset, committed_hash = self.database.execute(
            "SELECT sequence,offset,hash FROM watermark WHERE id=1").fetchone()
        self.stream.seek(0)
        sequence, offset, digest = 0, 0, ""
        committed_seen = committed == 0 and committed_offset == 0 and committed_hash == ""
        while True:
            raw = self.stream.readline(2 * 1024 * 1024 + 1)
            if not raw:
                break
            if len(raw) > 2 * 1024 * 1024:
                raise ValueError("Journal record exceeds maximum length")
            if not raw.endswith(b"\n"):
                if sequence < committed:
                    raise ValueError("Acknowledged journal prefix is truncated")
                tail = self.root / ("quarantine-" + uuid4().hex + ".bin")
                with tail.open("xb") as quarantine:
                    quarantine.write(raw)
                    quarantine.flush()
                    os.fsync(quarantine.fileno())
                self.stream.truncate(offset)
                break
            row = json.loads(raw)
            expected = hashlib.sha256(digest.encode() + encode(row["message"])).hexdigest()
            if row["sequence"] != sequence + 1 or row["previous"] != digest or row["sha256"] != expected:
                raise ValueError("Journal sequence or hash-chain corruption")
            sequence, offset, digest = row["sequence"], self.stream.tell(), expected
            if sequence == committed:
                committed_seen = offset == committed_offset and digest == committed_hash
        if not committed_seen:
            raise ValueError("Journal disagrees with its acknowledged durable watermark")
        self.stream.flush()
        os.fsync(self.stream.fileno())
        self.database.execute("UPDATE watermark SET sequence=?,offset=?,hash=? WHERE id=1",
                              (sequence, offset, digest))
        self.recovered_messages = sequence - committed
        self.stream.seek(0, 2)
        return sequence, offset, digest

    def append(self, message: dict[str, object]) -> int:
        """Append one complete source message; return its assigned, not necessarily durable, sequence."""
        if self.failed or self.stream is None:
            raise RuntimeError("Journal is closed or failed; recover it before appending")
        digest = hashlib.sha256(self.digest.encode() + encode(message)).hexdigest()
        record = encode({"sequence": self.sequence + 1, "previous": self.digest,
                         "sha256": digest, "message": message}) + b"\n"
        if len(record) > 2 * 1024 * 1024:
            raise ValueError("Journal message exceeds 2 MiB")
        try:
            written = self.stream.write(record)
            if written != len(record):
                raise OSError("Journal write was incomplete")
            self.sequence += 1
            self.digest = digest
            self.offset = self.stream.tell()
            self.flush_due()
        except BaseException:
            self.failed = True
            raise
        return self.sequence

    def flush_due(self) -> None:
        """Allow a quiet live consumer to flush pending data at its configured group-commit interval."""
        if (self.sequence - self.durable_sequence >= self.batch_size
                or time.monotonic() - self.last_flush >= self.interval):
            self.flush()

    def flush(self) -> int:
        """Flush file bytes before updating the transactional durable watermark."""
        if self.failed or self.stream is None:
            raise RuntimeError("Failed/closed journal cannot acknowledge data")
        try:
            self.stream.flush()
            os.fsync(self.stream.fileno())
            self.database.execute("UPDATE watermark SET sequence=?,offset=?,hash=? WHERE id=1",
                                  (self.sequence, self.offset, self.digest))
            self.durable_sequence = self.sequence
            self.last_flush = time.monotonic()
        except BaseException:
            self.failed = True
            raise
        return self.durable_sequence

    def messages(self) -> list[dict[str, object]]:
        """Revalidate the acknowledged prefix against its durable hash and offset before returning data.

        Recovery at construction cannot detect later disk/file corruption. Every
        publication read therefore independently verifies the complete committed
        chain, while ignoring any unacknowledged suffix still owned by this writer.
        """
        if self.failed or self.database is None or self.stream is None:
            raise RuntimeError("Journal is closed or failed; recover it before reading")
        try:
            committed, committed_offset, committed_hash = self.database.execute(
                "SELECT sequence,offset,hash FROM watermark WHERE id=1").fetchone()
            if committed != self.durable_sequence or committed < 0 or committed_offset < 0:
                raise ValueError("Journal durable watermark changed outside its owned writer")
            messages, digest = [], ""
            with self.path.open("rb") as stream:
                for sequence in range(1, committed + 1):
                    raw = stream.readline(2 * 1024 * 1024 + 1)
                    if not raw.endswith(b"\n") or len(raw) > 2 * 1024 * 1024:
                        raise ValueError("Acknowledged journal record is truncated or oversized")
                    row = json.loads(raw)
                    expected = hashlib.sha256(digest.encode() + encode(row["message"])).hexdigest()
                    if row["sequence"] != sequence or row["previous"] != digest or row["sha256"] != expected:
                        raise ValueError("Acknowledged journal sequence or hash-chain corruption")
                    messages.append(row["message"])
                    digest = expected
                if stream.tell() != committed_offset or digest != committed_hash:
                    raise ValueError("Acknowledged journal bytes disagree with the durable watermark")
            return messages
        except BaseException:
            self.failed = True
            raise

    def status(self) -> dict[str, object]:
        """Expose the precise acknowledgement boundary and bounded pending range."""
        return {"sequence": self.sequence, "durable_sequence": self.durable_sequence,
                "uncommitted_messages": self.sequence - self.durable_sequence,
                "recovered_messages": self.recovered_messages, "failed": self.failed}

    def _release(self) -> None:
        """Close owned handles; closing the OS lock also releases it after errors."""
        if self.stream is not None:
            self.stream.close()
            self.stream = None
        if self.database is not None:
            self.database.close()
            self.database = None
        self.lock.close()

    def close(self) -> None:
        """Flush a healthy journal then release ownership even when persistence fails."""
        try:
            if self.stream is not None and not self.failed:
                self.flush()
        finally:
            self._release()

    def __enter__(self) -> CaptureJournal:
        """Return the already locked journal for bounded lifecycle management."""
        return self

    def __exit__(self, kind: Any, value: Any, traceback: Any) -> None:
        """Release owned resources without discarding an unacknowledged tail."""
        self.close()
