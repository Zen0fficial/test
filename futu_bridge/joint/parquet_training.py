"""Operational native Qlib RL entrypoints over canonical Parquet snapshots.

Native policies, simulators, rewards, seeds and training budgets remain unchanged.
Only the input persistence boundary changes. Legacy JSON is an explicit rollback
path; no broker or provider client is constructed here.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import pandas as pd

from .qlib_parquet_execution import execution_from_snapshot
from .storage import SnapshotStore
from .storage.captures import ingest
from .session import write_report


def run(output: Path, *, algorithm: str, response: Path | None = None, store_root: Path | None = None,
        snapshot: str | None = None, storage: str = "parquet", iterations: int | None = None,
        episodes: int | None = None) -> dict[str, object]:
    """Select a pinned input and delegate unchanged learning to the native simple or nested workflow."""
    if algorithm not in ("simple", "nested") or storage not in ("parquet", "legacy_json"):
        raise ValueError("Select simple/nested and parquet/legacy_json explicitly")
    sources = {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
               for name in ("parquet_training.py", "qlib_parquet_execution.py",
                            "qlib_parquet_lineage.py", "qlib_rl_data.py")}
    output = output.resolve()
    if output.exists():
        raise ValueError("Choose a new training output directory")
    data = None
    input_check: dict[str, object] = {"analytical_source": "legacy_json"}
    if storage == "parquet":
        if store_root is None:
            if response is None or snapshot is not None:
                raise ValueError("Provide a real response or an existing store/snapshot pair")
            store_root = output.with_name(output.name + "_store")
            if store_root.exists():
                raise ValueError("Canonical input directory already exists; pass an explicit pinned snapshot")
            store = SnapshotStore(store_root)
            snapshot = ingest(store, response, "hk00700_minute", "futu-bars").identity
        else:
            if snapshot is None or response is not None:
                raise ValueError("Existing-store training requires only its explicit snapshot")
            store = SnapshotStore(store_root)
        if output.is_relative_to(store.root):
            raise ValueError("Native training output must be outside the canonical store")
        data = execution_from_snapshot(store.root, snapshot)
        from .qlib_parquet_lineage import execution_reference

        independent = execution_reference(store, snapshot)
        for field in ("open", "high", "low", "close", "volume"):
            pd.testing.assert_series_equal(data.minutes[field], independent.minutes[field].astype(float),
                                           check_exact=True)
        if data.partitions != independent.partitions or data.provenance != independent.provenance:
            raise ValueError("Canonical training split/provenance differs from independently reconstructed sources")
        for day, block in data.blocks.items():
            pd.testing.assert_frame_equal(block, independent.blocks[day], check_exact=True)
        input_check = {"passed": True, "analytical_source": "parquet", "source_arrays_exact": True,
                       "source_rows_verified": data.provenance["raw_bars"], "snapshot_id": snapshot,
                       "snapshot_sha256": data.provenance["snapshot_sha256"]}
        response = store.root / "raw" / data.provenance["response_sha256"]
    elif response is None or store_root is not None or snapshot is not None:
        raise ValueError("Legacy input requires only its original response")
    if algorithm == "simple":
        from .qlib_rl import run as native

        result = native(response, output, iterations=24 if iterations is None else iterations,
                         episodes=32 if episodes is None else episodes, data=data)
    else:
        from .qlib_nested_rl import run as native

        result = native(response, output, iterations=8 if iterations is None else iterations,
                         episodes=16 if episodes is None else episodes, data=data)
    result["canonical_input_verification"] = input_check
    if sources != {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest() for name in sources}:
        raise ValueError("Canonical input adapter changed during training")
    result["input_adapter_sha256"] = sources
    write_report(output / "report.json", result)
    return result


def main() -> None:
    """Train from a saved capture or immutable store ID in the shared Python environment."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--algorithm", choices=("simple", "nested"), required=True)
    parser.add_argument("--response", type=Path)
    parser.add_argument("--store", type=Path)
    parser.add_argument("--snapshot")
    parser.add_argument("--storage", choices=("parquet", "legacy_json"), default="parquet")
    parser.add_argument("--iterations", type=int)
    parser.add_argument("--episodes", type=int)
    args = parser.parse_args()
    result = run(args.output, algorithm=args.algorithm, response=args.response, store_root=args.store,
                 snapshot=args.snapshot, storage=args.storage, iterations=args.iterations, episodes=args.episodes)
    print(json.dumps({"completed": result.get("completed", result.get("passed")),
                      "data": result.get("data"), "real_orders_enabled": False}, indent=2))


if __name__ == "__main__":
    main()
