"""Operator commands for canonical Parquet storage; providers and legacy outputs remain unchanged."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path

from .captures import event_table, futu_table
from .decisions import DECISION_VERSIONS, decision_table
from .incremental import BAR_VERSIONS, EVENT_VERSIONS, merge
from .maintenance import cleanup_candidates, compact, rollback
from .observations import OBSERVATION_VERSIONS, PROBABILITY_VERSIONS, observation_table, probability_table
from .provider_records import FAMILIES, ingest_provider
from .snapshots import SnapshotStore


def main(argv: list[str] | None = None) -> None:
    """Expose explicit ingestion, validation, backup, rollback and live capture commands."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("output/joint/lake"))
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("list", "verify", "cleanup-candidates"):
        commands.add_parser(name)
    ingestion = commands.add_parser("ingest")
    ingestion.add_argument("--source", type=Path, required=True)
    ingestion.add_argument("--dataset", required=True)
    ingestion.add_argument("--kind", choices=("bars", "events", "probabilities", "observations", "decisions"),
                           required=True)
    ingestion.add_argument("--expected-head")
    provider = commands.add_parser("provider-ingest")
    provider.add_argument("--source", type=Path, required=True)
    provider.add_argument("--dataset", required=True)
    provider.add_argument("--family", choices=FAMILIES, required=True)
    provider.add_argument("--expected-head")
    backup = commands.add_parser("backup")
    backup.add_argument("--destination", type=Path, required=True)
    compaction = commands.add_parser("compact")
    compaction.add_argument("--snapshot", required=True)
    compaction.add_argument("--compression", choices=("zstd", "snappy", "none"), default="zstd")
    compaction.add_argument("--row-group-size", type=int, default=128_000)
    revert = commands.add_parser("rollback")
    revert.add_argument("--dataset", required=True)
    revert.add_argument("--snapshot", required=True)
    revert.add_argument("--expected-head", required=True)
    live = commands.add_parser("live")
    live.add_argument("--output", type=Path, required=True)
    live.add_argument("--seconds", type=int, required=True)
    live.add_argument("--disconnect-after", type=int, default=0)
    live.add_argument("--dataset", default="futu_observations")
    migration = commands.add_parser("migrate-observations")
    migration.add_argument("--snapshot", required=True)
    migration.add_argument("--dataset", required=True)
    migration.add_argument("--expected-head")
    view = commands.add_parser("resolve-bars")
    view.add_argument("--snapshot", required=True)
    view.add_argument("--dataset", required=True)
    view.add_argument("--knowledge-cutoff-ns", type=int, required=True)
    view.add_argument("--event-cutoff-ns", type=int)
    view.add_argument("--expected-head")
    timing = commands.add_parser("benchmark")
    timing.add_argument("--snapshot", required=True)
    timing.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    store = SnapshotStore(args.root)
    if args.command == "ingest":
        converters = {"bars": (futu_table, BAR_VERSIONS), "events": (event_table, EVENT_VERSIONS),
                      "probabilities": (probability_table, PROBABILITY_VERSIONS),
                      "observations": (observation_table, OBSERVATION_VERSIONS),
                      "decisions": (decision_table, DECISION_VERSIONS)}
        converter, contract = converters[args.kind]
        payload = args.source.read_bytes()
        result = asdict(merge(store, args.dataset, converter(payload), contract, expected_head=args.expected_head,
                              metadata={"capture_sha256": store.archive(payload), "capture_kind": args.kind}))
    elif args.command == "provider-ingest":
        result = asdict(ingest_provider(store, args.source, args.dataset, args.family,
                                        expected_head=args.expected_head))
    elif args.command == "list":
        with store.connection() as database:
            result = [dict(row) for row in database.execute("SELECT * FROM heads ORDER BY dataset")]
    elif args.command == "verify":
        result = store.inspect()
    elif args.command == "backup":
        result = store.backup(args.destination)
    elif args.command == "compact":
        result = asdict(compact(store, args.snapshot, compression=args.compression,
                                row_group_size=args.row_group_size))
    elif args.command == "rollback":
        rollback(store, args.dataset, args.snapshot, expected_head=args.expected_head)
        result = {"dataset": args.dataset, "head": store.head(args.dataset)}
    elif args.command == "cleanup-candidates":
        result = {"candidates": cleanup_candidates(store), "deleted": 0, "retention": "all committed snapshots"}
    elif args.command == "migrate-observations":
        from .migrations import migrate_observations

        result = asdict(migrate_observations(store, args.snapshot, args.dataset, expected_head=args.expected_head))
    elif args.command == "resolve-bars":
        from .views import resolve_bars

        result = asdict(resolve_bars(store, args.snapshot, args.dataset, knowledge_cutoff_ns=args.knowledge_cutoff_ns,
                                    event_cutoff_ns=args.event_cutoff_ns, expected_head=args.expected_head))
    elif args.command == "live":
        from .live import capture_live

        result = capture_live(store, args.output, seconds=args.seconds, disconnect_after=args.disconnect_after,
                              dataset=args.dataset)
    else:
        from .benchmark import benchmark

        result = benchmark(store, args.snapshot, args.output)
    print(json.dumps(result, indent=2))
    if isinstance(result, dict) and result.get("passed") is False:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
