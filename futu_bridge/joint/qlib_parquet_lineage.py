"""Independently reconstruct canonical bar unions from unchanged original capture archives.

Row selections describe source partitions, never edited provider responses.
Every source receipt retains its original hash and observation time. The latest
observed row wins; equal-time conflicting versions are rejected. Historical
availability remains unknown rather than being inferred from a bar label.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import hashlib
import json
from typing import Any

import pandas as pd

from .storage import Snapshot, SnapshotStore
from .storage.captures import BAR_SCHEMA

KEY = ("symbol", "timeframe", "event_ns")
FIELDS = ("open", "high", "low", "close", "volume")


def intervals(indices: list[int]) -> list[list[int]]:
    """Compress explicitly selected source ordinals into ordered half-open intervals."""
    result: list[list[int]] = []
    for value in sorted(set(indices)):
        if type(value) is not int or value < 0:
            raise ValueError("Source row selections require nonnegative integer ordinals")
        if result and value == result[-1][1]:
            result[-1][1] += 1
        else:
            result.append([value, value + 1])
    return result


def expand(ranges: list[list[int]] | None, count: int) -> list[int]:
    """Validate explicit source selections without manufacturing missing observations."""
    if ranges is None:
        return list(range(count))
    indices, previous = [], -1
    for bounds in ranges:
        if (len(bounds) != 2 or any(type(item) is not int for item in bounds)
                or not 0 <= bounds[0] < bounds[1] <= count or bounds[0] < previous):
            raise ValueError("Malformed or overlapping source row selection")
        indices.extend(range(*bounds))
        previous = bounds[1]
    if not indices:
        raise ValueError("A source selection cannot be empty")
    return indices


def manifest(snapshot: Snapshot) -> dict[str, list[list[int]] | None]:
    """Read explicit selections, or infer complete original archives for legacy snapshots."""
    selected = snapshot.metadata.get("capture_selections")
    if selected is not None:
        if not isinstance(selected, dict) or not selected:
            raise ValueError("Invalid canonical capture selection manifest")
        return selected
    hashes = snapshot.metadata.get("capture_hashes")
    if hashes is None:
        sources = snapshot.metadata.get("sources")
        hashes = [item["sha256"] for item in sources.values()] if isinstance(sources, dict) else [
            snapshot.metadata.get("capture_sha256")]
    if not hashes or any(not isinstance(value, str) for value in hashes):
        raise ValueError("Canonical bars require original source archive identities")
    return {value: None for value in hashes}


def original(store: SnapshotStore, digest: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Hash-check an original response and derive exact rows without calling the production converter."""
    if len(digest) != 64 or any(value not in "0123456789abcdef" for value in digest):
        raise ValueError("Malformed original capture identity")
    payload = (store.root / "raw" / digest).read_bytes()
    if hashlib.sha256(payload).hexdigest() != digest:
        raise ValueError("Original source archive failed its content hash")
    envelope = json.loads(payload, parse_float=Decimal)
    envelope["source"] = json.loads(payload)["source"]
    source, data = envelope["source"], envelope["data"]
    if (source.get("provider") != "futu-desktop-session" or source.get("kind") != "historical"
            or data.get("adjustment") != "none" or data.get("timeframe") not in ("1d", "1m")
            or not data.get("complete_requested_range") or data.get("truncated")):
        raise ValueError("Source archive is not a complete unadjusted real Futu capture")
    observed = pd.Timestamp(source["observed_at"])
    if pd.isna(observed) or observed.tzinfo is None:
        raise ValueError("Source receipt must retain an explicit timezone")
    rows, previous = [], None
    for ordinal, bar in enumerate(data["bars"]):
        stamp = pd.Timestamp(bar["timestamp"])
        values = {field: Decimal(str(bar[field])) for field in FIELDS}
        if (pd.isna(stamp) or stamp.tzinfo is None or previous is not None and stamp.value <= previous
                or bar.get("symbol", data["symbol"]) != data["symbol"]
                or any(not value.is_finite() for value in values.values()) or values["volume"] < 0
                or min(values[field] for field in FIELDS[:4]) <= 0
                or values["low"] > min(values["open"], values["close"])
                or values["high"] < max(values["open"], values["close"])):
            raise ValueError("Invalid independently decoded source identity, timestamps or prices")
        rows.append({"symbol": data["symbol"], "timeframe": data["timeframe"], "event_ns": stamp.value,
                     "trading_date": bar["date"], **values, "available_ns": None,
                     "turnover_raw": str(bar["turnover_raw"]) if bar.get("turnover_raw") is not None else None,
                     "capture_observed_ns": observed.value, "source_row": ordinal, "source_sha256": digest})
        previous = stamp.value
    if not rows:
        raise ValueError("An empty original archive cannot establish canonical lineage")
    return envelope, rows


@dataclass(frozen=True)
class CaptureLineage:
    """Independently verified union and original envelopes for offline audit and conversion."""

    rows: list[dict[str, Any]]
    envelopes: dict[str, dict[str, Any]]
    selections: dict[str, list[list[int]] | None]

    def bars(self) -> list[dict[str, Any]]:
        """Recover source bar dictionaries in canonical symbol/time order without editing their values."""
        return [self.envelopes[row["source_sha256"]]["data"]["bars"][row["source_row"]] for row in self.rows]

    def evidence(self) -> dict[str, Any]:
        """Describe hashes, exact source row selections and receipt times without a synthetic response hash."""
        return {"capture_selections": self.selections,
                "capture_sources": [{"sha256": digest, "source": envelope["source"]}
                                    for digest, envelope in sorted(self.envelopes.items())],
                "source_rows_verified": len(self.rows), "historical_availability": "unknown"}


def reconstruct(store: SnapshotStore, selections: dict[str, list[list[int]] | None]) -> CaptureLineage:
    """Resolve exact selected source receipts independently before publishing or reading a union."""
    envelopes, reconstructed = {}, {}
    for digest, selected in sorted(selections.items()):
        envelope, rows = original(store, digest)
        envelopes[digest] = envelope
        for ordinal in expand(selected, len(rows)):
            row = rows[ordinal]
            key = tuple(row[field] for field in KEY)
            old = reconstructed.get(key)
            if old is None or row["capture_observed_ns"] > old["capture_observed_ns"]:
                reconstructed[key] = row
            elif row["capture_observed_ns"] == old["capture_observed_ns"] and row != old:
                raise ValueError("Conflicting source rows have the same observation time")
    expected = [reconstructed[key] for key in sorted(reconstructed)]
    return CaptureLineage(expected, envelopes, selections)


def verify(store: SnapshotStore, identity: str) -> CaptureLineage:
    """Require all canonical fields to match the latest independently reconstructed source observation."""
    snapshot = store.snapshot(identity)
    table = store.read(identity)
    if not table.schema.equals(BAR_SCHEMA, check_metadata=True):
        raise ValueError("Source lineage requires the complete canonical Futu bar schema")
    lineage = reconstruct(store, manifest(snapshot))
    if table.to_pylist() != lineage.rows:
        raise ValueError("Canonical values, revisions, availability or row coverage differ from original archives")
    return lineage


def execution_reference(store: SnapshotStore, identity: str) -> Any:
    """Run the legacy session converter on independently decoded union rows for differential audit."""
    from .qlib_rl_data import from_envelope

    snapshot = store.snapshot(identity)
    lineage = verify(store, identity)
    anchor = snapshot.metadata.get("capture_sha256")
    if anchor not in lineage.envelopes:
        raise ValueError("Execution requires an original capture anchor belonging to its source manifest")
    envelope = lineage.envelopes[anchor]
    reconstructed = {**envelope, "data": {**envelope["data"], "bars": lineage.bars()}}
    result = from_envelope(reconstructed, anchor)
    result.provenance.update(snapshot_id=identity, snapshot_sha256=snapshot.sha256,
                             analytical_source="parquet", snapshot_root=str(store.root))
    if len(lineage.envelopes) > 1 or any(value is not None for value in lineage.selections.values()):
        result.provenance["capture_lineage"] = lineage.evidence()
    return result
