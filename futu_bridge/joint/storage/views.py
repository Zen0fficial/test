"""Resolve immutable version histories into source-verified analytical bar views."""

from __future__ import annotations

import pyarrow as pa

from ..qlib_parquet_lineage import intervals, reconstruct
from .captures import BAR_SCHEMA
from .incremental import BAR_VERSIONS, as_of
from .snapshots import Snapshot, SnapshotStore


def resolve_bars(store: SnapshotStore, identity: str, dataset: str, *, knowledge_cutoff_ns: int,
                 event_cutoff_ns: int | None = None, expected_head: str | None = None) -> Snapshot:
    """Publish one version per bar for native readers, retaining exact source row and cutoff lineage.

    Original history and all old views remain immutable. This is an explicit
    knowledge-time selection, never an assertion that historical bars were
    received at their exchange timestamps. Empty pre-observation queries fail.
    """
    previous = store.snapshot(identity)
    if previous.metadata.get("version_contract") != BAR_VERSIONS.metadata():
        raise ValueError("Bar view requires the canonical bar revision contract")
    if dataset == previous.dataset:
        raise ValueError("Publish a resolved view into a different logical dataset")
    if type(knowledge_cutoff_ns) is not int or knowledge_cutoff_ns <= 0:
        raise ValueError("Knowledge cutoff requires positive integer UTC nanoseconds")
    if event_cutoff_ns is not None and (type(event_cutoff_ns) is not int or event_cutoff_ns <= 0):
        raise ValueError("Event cutoff requires positive integer UTC nanoseconds")
    table = as_of(store, identity, knowledge_cutoff_ns, event_cutoff_ns=event_cutoff_ns).drop(["_version_id"])
    if not table.schema.equals(BAR_SCHEMA, check_metadata=True) or not table.num_rows:
        raise ValueError("No complete canonical bars are visible at this cutoff")
    rows = table.to_pylist()
    selected: dict[str, list[int]] = {}
    for row in rows:
        selected.setdefault(row["source_sha256"], []).append(row["source_row"])
    selections = {digest: intervals(ordinals) for digest, ordinals in sorted(selected.items())}
    independently = reconstruct(store, selections)
    if independently.rows != rows:
        raise ValueError("Resolved history differs from its untouched original source rows")
    anchor = max(rows, key=lambda row: (row["capture_observed_ns"], row["source_sha256"]))["source_sha256"]
    metadata = {"capture_sha256": anchor, "capture_hashes": sorted(selections), "capture_selections": selections,
                "history_snapshot": identity, "history_sha256": previous.sha256,
                "knowledge_cutoff_ns": knowledge_cutoff_ns, "event_cutoff_ns": event_cutoff_ns,
                "availability": "unknown_historical_availability", "real_orders_enabled": False}
    return store.publish(dataset, pa.Table.from_pylist(rows, schema=BAR_SCHEMA), metadata,
                         expected_head=expected_head)
