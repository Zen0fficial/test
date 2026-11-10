"""Real-capture acceptance for canonical histories, documents, journals and restoration."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from .benchmark import benchmark
from .captures import event_table, futu_table
from .decisions import DECISION_VERSIONS, decision_table
from .incremental import BAR_VERSIONS, EVENT_VERSIONS, as_of, merge
from .journal import CaptureJournal
from .maintenance import archive_document, compact, rollback, scan
from .observations import OBSERVATION_VERSIONS, PROBABILITY_VERSIONS, observation_table, probability_table
from .replay import replay
from .snapshots import SnapshotStore, equivalent, file_hash


def run(repo: Path, output: Path, *, timing: bool = True) -> dict[str, object]:
    """Validate actual source values, duplicate replay, as-of boundaries and independently restored snapshots."""
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    store = SnapshotStore(output / "store")
    specifications = {
        "minute": ("output/joint/qlib_native_rl_data_20261109/provider_response.json", futu_table, BAR_VERSIONS),
        "daily": ("output/joint/ml_comparison_20261109/comparison/provider_response.json", futu_table, BAR_VERSIONS),
        "events": ("output/platform_compatibility/capture_20261102_03/event.json", event_table, EVENT_VERSIONS),
        "probabilities": ("output/platform_compatibility/capture_20261102_03/probability_0.json",
                          probability_table, PROBABILITY_VERSIONS),
        "observations": ("output/joint/shared_live_20261109/nautilus/raw.jsonl",
                         observation_table, OBSERVATION_VERSIONS),
        "decisions": ("output/polymarket/equity_filter/http_filter_results.json", decision_table, DECISION_VERSIONS),
    }
    reports = {}
    for name, (relative, converter, contract) in specifications.items():
        source = repo / relative
        payload, table = source.read_bytes(), converter(source.read_bytes())
        digest = store.archive(payload)
        half = max(1, table.num_rows // 2)
        first = merge(store, name, table.slice(0, half), contract, expected_head=None,
                      metadata={"capture_sha256": digest})
        second = merge(store, name, table, contract, expected_head=first.identity,
                       metadata={"capture_sha256": digest})
        repeated = merge(store, name, table, contract, expected_head=second.identity)
        if repeated.identity != second.identity or second.rows != table.num_rows:
            raise ValueError("Overlapping capture replay was not idempotent")
        original = store.read(second.identity).drop(["_version_id"])
        # Incoming rows may have provider order rather than canonical primary-key order.
        expected = table.sort_by([(key, "ascending") for key in (*contract.keys, contract.observed)])
        if not equivalent(original.to_pydict(), expected.to_pydict()):
            raise ValueError("Canonical revision ordering changed exact source values")
        before = min(table[contract.observed].to_pylist()) - 1
        if as_of(store, second.identity, before).num_rows:
            raise ValueError("Data leaked backward before observation")
        repacked = compact(store, second.identity, row_group_size=1024)
        if not store.read(repacked.identity).equals(store.read(second.identity)):
            raise ValueError("Compaction changed canonical values")
        rollback(store, name, second.identity, expected_head=repacked.identity)
        if store.head(name) != second.identity or store.read(first.identity).num_rows != half:
            raise ValueError("Pinned snapshot or rollback failed")
        if "event_ns" in table.column_names and table["event_ns"].null_count == 0:
            end = max(table["event_ns"].to_pylist())
            result = scan(store, second.identity, columns=["event_ns"], filters=[("event_ns", "==", end)])
            if result.num_rows != sum(item == end for item in table["event_ns"].to_pylist()):
                raise ValueError("Projected predicate returned incorrect rows")
        reports[name] = {"rows": table.num_rows, "snapshot": second.identity, "source_sha256": digest,
                         "idempotent": True, "asof_no_backdating": True, "compaction_and_rollback": True}
    source = repo / specifications["observations"][0]
    with CaptureJournal(output / "journal") as journal:
        for line in source.read_bytes().splitlines():
            journal.append(json.loads(line))
        journal.flush()
        published = replay(store, journal, dataset="journal_observations")
        again = replay(store, journal, dataset="journal_observations")
        if again["replayed_messages"] != 0 or again["snapshot"] != published["snapshot"]:
            raise ValueError("Acknowledged journal replay was not idempotent")
        journal_status = journal.status()
    event_path = repo / specifications["events"][0]
    event_rows = event_table(event_path.read_bytes())
    document = archive_document(store, "documents", event_path, document_id="polymarket-event-capture",
                                observed_ns=event_rows["observed_ns"][0].as_py(), media_type="application/json",
                                expected_head=None)
    restored_report = store.backup(output / "restored")
    restored = SnapshotStore(output / "restored")
    for evidence in reports.values():
        if not restored.read(evidence["snapshot"]).equals(store.read(evidence["snapshot"])):
            raise ValueError("Restored canonical data changed")
    layout = benchmark(store, reports["minute"]["snapshot"], output / "benchmark") if timing else None
    report = {"passed": True, "real_data": reports, "journal": journal_status,
              "replay": published, "document_snapshot": document.identity, "restoration": restored_report,
              "benchmark": layout, "real_orders_enabled": False, "providers_modified": False,
              "source_hashes": {p.relative_to(repo).as_posix(): file_hash(p)
                                for p in (repo / "futu_bridge/joint/storage").glob("*.py")},
              "limitations": ["All committed generations retained; automatic garbage deletion disabled",
                              "Full-table rewriting measured on current small captures only",
                              "Historical availability is unknown; as-of uses observed capture time",
                              "Depth snapshots do not reconstruct exchange order queues",
                              "Backup verified in another local directory, not on an independent disk",
                              "No genuine revised event resolution is present in this input set"]}
    (output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def main() -> None:
    """Run reproducible acceptance using fixed private captures and no provider changes."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--skip-timing", action="store_true")
    args = parser.parse_args()
    print(json.dumps(run(args.repo.resolve(), args.output, timing=not args.skip_timing), indent=2))


if __name__ == "__main__":
    main()
