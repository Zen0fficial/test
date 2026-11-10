"""Idempotently compact acknowledged live messages and advance a recoverable replay cursor."""

from __future__ import annotations

import hashlib
import json

from .incremental import merge
from .journal import CaptureJournal, encode
from .observations import OBSERVATION_VERSIONS, observation_table
from .snapshots import SnapshotStore


def validate_cursor_head(store: SnapshotStore, dataset: str, cursor_snapshot: str) -> None:
    """Require the accepted cursor generation to remain in the current head's retained lineage."""
    current = store.head(dataset)
    visited: set[str] = set()
    while current is not None:
        if current == cursor_snapshot:
            return
        if current in visited:
            raise ValueError("Snapshot parent chain contains a cycle")
        visited.add(current)
        snapshot = store.snapshot(current)
        if snapshot.dataset != dataset:
            raise ValueError("Replay cursor parent chain crosses dataset boundaries")
        current = snapshot.parent
    raise ValueError("Replay cursor is outside the current head lineage; invalidate it and replay the full journal")


def replay(store: SnapshotStore, journal: CaptureJournal, *, dataset: str = "futu_observations") -> dict[str, object]:
    """Publish complete source messages before committing their replay position.

    The filesystem, snapshot transaction and cursor are not one transaction.
    Failure after publication leaves the cursor behind; stable message IDs make
    the repeated merge idempotent. A writer must own the journal during replay.
    """
    source = str(journal.root)
    with store.connection() as database:
        database.execute("BEGIN IMMEDIATE")
        database.execute("CREATE TABLE IF NOT EXISTS replay_cursors "
                         "(source TEXT, dataset TEXT, sequence INTEGER, snapshot TEXT REFERENCES snapshots(identity),"
                         "journal_hash TEXT,PRIMARY KEY(source,dataset))")
        columns = {column["name"] for column in database.execute("PRAGMA table_info(replay_cursors)")}
        if "journal_hash" not in columns:
            database.execute("ALTER TABLE replay_cursors ADD COLUMN journal_hash TEXT")
        row = database.execute("SELECT sequence,snapshot,journal_hash FROM replay_cursors "
                               "WHERE source=? AND dataset=?",
                               (source, dataset)).fetchone()
        if row is not None and row["journal_hash"] is None:
            # Earlier cursors were bound only to a path/offset, so they cannot safely skip any journal bytes.
            database.execute("DELETE FROM replay_cursors WHERE source=? AND dataset=?", (source, dataset))
            row = None
        database.commit()
    before = int(row[0]) if row else 0
    acknowledged = journal.messages()
    through = len(acknowledged)
    if before < 0 or before > through:
        raise ValueError("Replay cursor exceeds the acknowledged journal")
    hashes = [""]
    for message in acknowledged:
        hashes.append(hashlib.sha256(hashes[-1].encode() + encode(message)).hexdigest())
    if row is not None:
        if row["journal_hash"] != hashes[before]:
            raise ValueError("Journal prefix changed at the saved cursor; "
                             "use a new journal path or explicitly reset it")
        validate_cursor_head(store, dataset, row[1])
    if before == through:
        return {"snapshot": row[1] if row else None, "sequence": before, "replayed_messages": 0}
    messages = acknowledged[before:]
    payload = b"".join(json.dumps(message, ensure_ascii=False).encode() + b"\n" for message in messages)
    digest = store.archive(payload)
    snapshot = merge(store, dataset, observation_table(payload), OBSERVATION_VERSIONS,
                     expected_head=store.head(dataset), metadata={"capture_sha256": digest,
                                                                 "journal_source": source})
    with store.connection() as database:
        database.execute("BEGIN IMMEDIATE")
        # A concurrent rollback may have moved the head after file publication.
        # The write lock now prevents another head/cursor mutation until acceptance commits.
        validate_cursor_head(store, dataset, snapshot.identity)
        row = database.execute("SELECT sequence,journal_hash FROM replay_cursors "
                               "WHERE source=? AND dataset=?",
                               (source, dataset)).fetchone()
        if (int(row[0]) if row else 0) != before or (row[1] if row else "") != hashes[before]:
            raise ValueError("Replay cursor changed concurrently")
        database.execute("INSERT INTO replay_cursors(source,dataset,sequence,snapshot,journal_hash) "
                         "VALUES (?,?,?,?,?) "
                         "ON CONFLICT(source,dataset) DO UPDATE SET sequence=excluded.sequence,"
                         "snapshot=excluded.snapshot,journal_hash=excluded.journal_hash",
                         (source, dataset, through, snapshot.identity, hashes[through]))
        database.commit()
    return {"snapshot": snapshot.identity, "sequence": through, "replayed_messages": len(messages),
            "replay_started_at": before, "full_journal_replayed": before == 0}
