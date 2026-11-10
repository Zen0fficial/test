"""Authenticated capture publication for Qlib calendar, instrument and feature updates.

Qlib workers remain pinned to their old snapshot. This converter-owned facade
merges new original captures and publishes a replacement snapshot instead of
mutating native feature files. It does not manufacture exchange calendars,
historical memberships or market revisions that were never observed.
"""

from __future__ import annotations

from dataclasses import asdict
import hashlib
from pathlib import Path
from typing import Any

import pyarrow as pa

from .storage import SnapshotConflict, SnapshotStore
from .storage.captures import BAR_SCHEMA, futu_table
from .qlib_parquet_lineage import expand, intervals, manifest, reconstruct, verify

KEY = ("symbol", "timeframe", "event_ns")
VALUE = ("trading_date", "open", "high", "low", "close", "volume", "turnover_raw", "available_ns")


def publish_capture(root: Path, pinned_snapshot: str, response: Path, *,
                    source_rows: list[int] | None = None) -> dict[str, Any]:
    """Publish source-backed calendar extensions, observed lifetimes and feature corrections.

    Duplicate payloads and identical observations are no-ops. Later receipts of
    unchanged values advance observed-at provenance to reject older corrections.
    A conflicting older observation is rejected. The snapshot identity is the
    optimistic concurrency token; a stale writer must explicitly rebase.
    Optional source_rows selects original ordinals without editing the capture.
    """
    store = SnapshotStore(root)
    previous = store.snapshot(pinned_snapshot)
    if store.head(previous.dataset) != pinned_snapshot:
        raise SnapshotConflict("Qlib update was based on a stale snapshot")
    original = store.read(pinned_snapshot)
    if not original.schema.equals(BAR_SCHEMA, check_metadata=True):
        raise ValueError("Qlib updates require the canonical Futu bar schema")
    verify(store, pinned_snapshot)
    payload = response.read_bytes()
    incoming = futu_table(payload)
    selected = None if source_rows is None else intervals(source_rows)
    indices = expand(selected, len(incoming))
    incoming = incoming.take(pa.array(indices, type=pa.int64()))
    frequencies = set(original.column("timeframe").to_pylist()) | set(incoming.column("timeframe").to_pylist())
    if len(frequencies) != 1:
        raise ValueError("Do not merge different frequencies into one Qlib snapshot")
    records = {tuple(row[name] for name in KEY): row for row in original.to_pylist()}
    if len(records) != len(original):
        raise ValueError("Resolve duplicate/revised rows before using the Qlib view updater")
    counts = {"inserted": 0, "corrected": 0, "unchanged": 0}
    refreshed = 0
    for row in incoming.to_pylist():
        key = tuple(row[name] for name in KEY)
        old = records.get(key)
        if old is not None and all(old[name] == row[name] for name in VALUE):
            counts["unchanged"] += 1
            if row["capture_observed_ns"] > old["capture_observed_ns"]:
                records[key] = row
                refreshed += 1
            continue
        if old is not None and row["capture_observed_ns"] <= old["capture_observed_ns"]:
            raise ValueError("A correction must be observed after the stored version")
        records[key] = row
        counts["inserted" if old is None else "corrected"] += 1
    capture_hash = store.archive(payload)
    if not counts["inserted"] and not counts["corrected"] and not refreshed:
        return {"snapshot": asdict(previous), "changed": False, "counts": counts,
                "capture_sha256": capture_hash, "observations_refreshed": refreshed, "real_orders_enabled": False}
    ordered = [records[key] for key in sorted(records)]
    selections = manifest(previous)
    if capture_hash in selections:
        prior = selections[capture_hash]
        selections[capture_hash] = (None if prior is None or selected is None else
                                    intervals([*expand(prior, len(futu_table(payload))), *indices]))
    else:
        selections[capture_hash] = selected
    if reconstruct(store, selections).rows != ordered:
        raise ValueError("Proposed update differs from independently reconstructed source selections")
    metadata = {**previous.metadata, "capture_sha256": capture_hash,
                "capture_hashes": sorted(selections), "capture_selections": selections,
                "update_kind": "source_authenticated_calendar_instrument_feature_publication",
                "update_counts": counts, "observations_refreshed": refreshed, "parent_snapshot": pinned_snapshot,
                "future_calendar": "observed_only", "instrument_lifetimes": "observed_spans"}
    current = store.publish(previous.dataset, pa.Table.from_pylist(ordered, schema=BAR_SCHEMA), metadata,
                            expected_head=pinned_snapshot)
    return {"snapshot": asdict(current), "changed": True, "counts": counts,
            "capture_sha256": capture_hash, "observations_refreshed": refreshed, "real_orders_enabled": False}


def run(response: Path, output: Path, repo: Path) -> dict[str, Any]:
    """Verify a real later capture advances the view while preserving old pinned readers."""
    from .qlib_parquet import ParquetCalendarStorage
    from .session import write_report
    from .storage.captures import ingest

    output.mkdir(parents=True, exist_ok=False)
    store = SnapshotStore(output / "store")
    before = ingest(store, response, "hk00700_daily", "futu-bars")
    old_reader = ParquetCalendarStorage(str(store.root), before.identity, "day")
    old_dates = old_reader.data
    later = repo / "output/joint/ml_comparison_20261109/comparison/provider_response.json"
    update = publish_capture(store.root, before.identity, later)
    current = update["snapshot"]["identity"]
    expected = futu_table(later.read_bytes())
    if not store.read(current).equals(expected, check_metadata=True) or old_reader.data != old_dates:
        raise ValueError("Published update differs from the genuine later capture or changed a pinned calendar")
    if not update["changed"] or not update["counts"]["inserted"] or not update["observations_refreshed"]:
        raise ValueError("Update acceptance requires real new bars and later observed provenance")
    repeated = publish_capture(store.root, current, later)
    if repeated["changed"] or repeated["snapshot"]["identity"] != current:
        raise ValueError("Repeated source capture was not idempotent")
    stale_rejected = False
    try:
        publish_capture(store.root, before.identity, later)
    except SnapshotConflict:
        stale_rejected = True
    if not stale_rejected:
        raise ValueError("Stale Qlib updater replaced a newer view")
    report = {"passed": True, "before_snapshot": asdict(before), "update": update,
              "old_calendar_rows": len(old_dates), "new_calendar_rows": len(expected),
              "new_source_arrays_exact": True, "old_reader_stable": True,
              "repeat_idempotent": True, "stale_writer_rejected": True,
              "real_orders_enabled": False, "providers_modified": False,
              "source_hashes": {Path(__file__).name: hashlib.sha256(Path(__file__).read_bytes()).hexdigest()},
              "original_capture_sha256": hashlib.sha256(response.read_bytes()).hexdigest(),
              "later_capture_sha256": hashlib.sha256(later.read_bytes()).hexdigest(),
              "limitation": "No observed price correction in these captures; correction count is zero"}
    write_report(output / "report.json", report)
    return report
