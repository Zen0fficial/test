"""Explicit, archive-verified observation v1 to v2 migrations into a separate dataset.

V2 canonicalizes semantic JSON payloads so transport formatting cannot create
false revisions. Original source bytes and every existing snapshot remain intact.
An automatic schema fallback is deliberately not part of incremental ingestion.
"""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Any

import pyarrow as pa

from .incremental import canonical, merge
from .observations import OBSERVATION_SCHEMA, OBSERVATION_VERSIONS, observation_table
from .snapshots import Snapshot, SnapshotStore, file_hash


def record_key(row: dict[str, Any]) -> tuple[str, int, str]:
    """Bind a normalized delivery to its exact semantic source envelope and receipt time."""
    payload = json.dumps(json.loads(row["payload_json"]), sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False)
    return str(row["message_id"]), int(row["received_ns"]), payload


def source_records(store: SnapshotStore, digest: str) -> dict[tuple[str, int, str], dict[str, Any]]:
    """Hash-check original bytes and independently regenerate normalized records from the archived source."""
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ValueError("Observation migration requires an exact source archive identity")
    path = store.root / "raw" / digest
    if not path.is_file() or file_hash(path) != digest:
        raise ValueError("Observation migration source archive is missing or corrupt")
    rows = observation_table(path.read_bytes()).to_pylist()
    result = {}
    for row in rows:
        key = record_key(row)
        if key in result and result[key] != row:
            raise ValueError("Archived source contains ambiguous normalized observation identities")
        result[key] = row
    return result


def migrate_observations(store: SnapshotStore, identity: str, dataset: str, *,
                         expected_head: str | None = None) -> Snapshot:
    """Rebuild v1 payload/version identities only after exact source and normalized-field validation.

    The destination must be a different logical dataset. Callers explicitly
    switch consumers after verifying it; old snapshots and their heads remain
    readable for rollback. Changed prices, quantities or timing are never fixed
    silently by this migration.
    """
    previous = store.snapshot(identity)
    if dataset == previous.dataset:
        raise ValueError("Observation schema migration requires an explicitly different destination dataset")
    table = store.read(identity)
    if table.schema.metadata.get(b"joint_schema") != b"futu_observation_v1":
        raise ValueError("This migration accepts only observation schema v1")
    versioned = "_version_id" in table.column_names
    source_schema = table.drop(["_version_id"]).schema if versioned else table.schema
    if not source_schema.equals(OBSERVATION_SCHEMA, check_metadata=False):
        raise ValueError("V1 normalized fields differ from the declared observation schema")
    if versioned and previous.metadata.get("version_contract") != OBSERVATION_VERSIONS.metadata():
        raise ValueError("V1 observation version contract differs from the declared migration")
    originals: dict[str, dict[tuple[str, int, str], dict[str, Any]]] = {}
    normalized = []
    for row in table.to_pylist():
        if versioned:
            key = canonical([*[row[name] for name in OBSERVATION_VERSIONS.keys], row[OBSERVATION_VERSIONS.observed]])
            value = canonical([row[name] for name in OBSERVATION_VERSIONS.values])
            if sha256((key + value).encode()).hexdigest() != row["_version_id"]:
                raise ValueError("V1 observation version identity is inconsistent with its original payload")
        digest = row["source_sha256"]
        if digest not in originals:
            originals[digest] = source_records(store, digest)
        expected = originals[digest].get(record_key(row))
        if expected is None:
            raise ValueError("V1 observation payload is absent from its claimed source archive")
        for field in OBSERVATION_SCHEMA.names:
            if field != "payload_json" and row[field] != expected[field]:
                raise ValueError(f"V1 normalized field differs from its source archive: {field}")
        normalized.append(expected)
    if not normalized:
        raise ValueError("An empty observation dataset cannot certify this migration")
    return merge(store, dataset, pa.Table.from_pylist(normalized, schema=OBSERVATION_SCHEMA),
                 OBSERVATION_VERSIONS, expected_head=expected_head,
                 metadata={"migrated_from": identity, "migration": "futu_observation_v1_to_v2",
                           "source_archives_verified": sorted(originals), "normalized_rows_verified": len(normalized)})
