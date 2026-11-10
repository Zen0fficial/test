"""Idempotent version histories over immutable snapshots, with explicit knowledge times.

Every delivery retains a source identity. Equal prices are never trade IDs. A
version is visible only after its observation/availability time; event time alone
cannot make retrospective metadata available to an earlier experiment.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import hashlib
import json
from typing import Any

import pyarrow as pa

from .snapshots import Snapshot, SnapshotConflict, SnapshotStore


def canonical(value: Any) -> str:
    """Encode exact typed values without rounding decimals or accepting nonfinite JSON."""
    if isinstance(value, Decimal):
        return json.dumps({"decimal": str(value)}, separators=(",", ":"))
    if isinstance(value, dict):
        return "{" + ",".join(json.dumps(k) + ":" + canonical(value[k]) for k in sorted(value)) + "}"
    if isinstance(value, (tuple, list)):
        return "[" + ",".join(canonical(item) for item in value) + "]"
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


@dataclass(frozen=True)
class VersionContract:
    """Define record identity separately from time of observation and economic payload."""

    keys: tuple[str, ...]
    observed: str
    values: tuple[str, ...]

    def metadata(self) -> dict[str, object]:
        """Return a JSON-safe schema contract used to reject accidental merge-policy changes."""
        return {"keys": list(self.keys), "observed": self.observed, "values": list(self.values)}

    def validate(self, table: pa.Table) -> None:
        """Require explicit non-null identities, knowledge times and existing payload fields."""
        required = (*self.keys, self.observed, *self.values)
        if not self.keys or not self.values or any(name not in table.column_names for name in required):
            raise ValueError("Version contract requires keys, observation time and payload columns")
        if "_version_id" in table.column_names:
            raise ValueError("Input contains reserved version column")
        if not pa.types.is_int64(table.schema.field(self.observed).type):
            raise ValueError("Observation time must be integer UTC nanoseconds")
        if any(table[name].null_count for name in (*self.keys, self.observed)):
            raise ValueError("Identity and observation time cannot be null")


BAR_VERSIONS = VersionContract(("symbol", "timeframe", "event_ns"), "capture_observed_ns",
                               ("open", "high", "low", "close", "volume", "turnover_raw"))
EVENT_VERSIONS = VersionContract(("event_id",), "observed_ns", ("payload_json",))


def merge(store: SnapshotStore, dataset: str, incoming: pa.Table, contract: VersionContract,
          *, expected_head: str | None, metadata: dict[str, object] | None = None) -> Snapshot:
    """Publish deduplicated revisions atomically; reject conflicting payloads at one knowledge time.

    Complete-table rewriting is deliberate for the measured small local captures.
    Existing snapshots stay readable during publication. Repeated identical input
    returns the existing identity, even after a replay cursor was lost in a crash.
    """
    contract.validate(incoming)
    if store.head(dataset) != expected_head:
        raise SnapshotConflict("Incremental input was based on a stale snapshot")
    fields = [*incoming.schema, pa.field("_version_id", pa.string(), nullable=False)]
    schema = pa.schema(fields, metadata=incoming.schema.metadata)
    previous = store.snapshot(expected_head) if expected_head else None
    if previous and previous.metadata.get("version_contract") != contract.metadata():
        raise ValueError("Version identity policy changed; use an explicit migration")
    old = store.read(expected_head) if expected_head else pa.Table.from_pylist([], schema=schema)
    if not old.schema.equals(schema, check_metadata=True):
        raise ValueError("Incremental schema changed; migrate explicitly before merging")
    rows = old.to_pylist()
    identities = {row["_version_id"] for row in rows}
    at_time = {canonical([*[row[k] for k in contract.keys], row[contract.observed]]): row["_version_id"]
               for row in rows}
    for row in incoming.to_pylist():
        key = canonical([*[row[k] for k in contract.keys], row[contract.observed]])
        digest = hashlib.sha256((key + canonical([row[k] for k in contract.values])).encode()).hexdigest()
        if key in at_time and at_time[key] != digest:
            raise ValueError("Two conflicting versions have the same identity and knowledge time")
        if digest not in identities:
            rows.append({**row, "_version_id": digest})
            identities.add(digest)
            at_time[key] = digest
    if previous and len(rows) == old.num_rows:
        # Check again: even an idempotent caller must not acknowledge a newer unseen head.
        if store.head(dataset) != expected_head:
            raise SnapshotConflict("Head changed during duplicate validation")
        return previous
    rows.sort(key=lambda row: (*[row[k] for k in contract.keys], row[contract.observed], row["_version_id"]))
    return store.publish(dataset, pa.Table.from_pylist(rows, schema=schema),
                         {**(metadata or {}), "version_contract": contract.metadata(),
                          "physical_layout": "complete_immutable_table", "real_orders_enabled": False},
                         expected_head=expected_head)


def as_of(store: SnapshotStore, identity: str, cutoff_ns: int, *, event_cutoff_ns: int | None = None) -> pa.Table:
    """Select the newest known version per identity, never backdating later observations."""
    snapshot = store.snapshot(identity)
    spec = snapshot.metadata.get("version_contract")
    if not isinstance(spec, dict):
        raise ValueError("As-of selection requires an explicit version contract")
    contract = VersionContract(tuple(spec["keys"]), str(spec["observed"]), tuple(spec["values"]))
    table = store.read(identity)
    selected: dict[str, dict[str, object]] = {}
    for row in table.to_pylist():
        if row[contract.observed] > cutoff_ns:
            continue
        event = row.get("event_ns")
        if event is not None and event > (cutoff_ns if event_cutoff_ns is None else event_cutoff_ns):
            continue
        key = canonical([row[k] for k in contract.keys])
        old = selected.get(key)
        if old is None or row[contract.observed] > old[contract.observed]:
            selected[key] = row
    rows = sorted(selected.values(), key=lambda row: tuple(row[k] for k in contract.keys))
    return pa.Table.from_pylist(rows, schema=table.schema)
