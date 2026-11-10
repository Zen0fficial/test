"""Correctness-qualified Parquet layouts on actual captures, without synthetic market rows."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import platform
import statistics
import time

import pyarrow as pa
import pyarrow.parquet as pq

from .snapshots import SnapshotStore, equivalent


def benchmark(store: SnapshotStore, identity: str, output: Path, repetitions: int = 11) -> dict[str, object]:
    """Rotate equal-workload trials and retain the incumbent if improvements are within timing noise.

    Timings separate write, full read and projected read. This is a warm OS-cache
    workload; no claim of OS-cold storage speed or a universally fastest layout.
    """
    if repetitions < 11:
        raise ValueError("Layout comparison requires at least eleven measured trials")
    output.mkdir(parents=True, exist_ok=False)
    table = store.read(identity)
    candidates = {"zstd_128k": ("zstd", 128_000), "zstd_1024": ("zstd", 1024),
                  "snappy_128k": ("snappy", 128_000), "snappy_1024": ("snappy", 1024)}
    trials: dict[str, dict[str, list[float]]] = {
        name: {"write_ms": [], "read_ms": [], "projection_ms": []} for name in candidates}
    names, sizes = list(candidates), {}
    columns = [name for name in ("event_ns", "close") if name in table.column_names] or table.column_names[:2]
    for trial in range(repetitions + 2):
        for name in names[trial % len(names):] + names[:trial % len(names)]:
            codec, size = candidates[name]
            path = output / (name + ".parquet")
            start = time.perf_counter_ns()
            pq.write_table(table, path, compression=codec, row_group_size=size)
            written = time.perf_counter_ns()
            restored = pq.read_table(path)
            read = time.perf_counter_ns()
            projected = pq.read_table(path, columns=columns)
            end = time.perf_counter_ns()
            if (not restored.schema.equals(table.schema, check_metadata=True)
                    or not equivalent(restored.to_pydict(), table.to_pydict())
                    or not equivalent(projected.to_pydict(), table.select(columns).to_pydict())):
                raise ValueError("Benchmark candidate failed exact semantic parity")
            if trial >= 2:
                for metric, value in zip(trials[name], (written - start, read - written, end - read)):
                    trials[name][metric].append(value / 1_000_000)
            sizes[name] = path.stat().st_size
    summary = {name: {metric: {"median_ms": statistics.median(values), "min_ms": min(values),
                               "max_ms": max(values), "samples_ms": values}
                      for metric, values in metrics.items()} for name, metrics in trials.items()}
    # Equal weights are a declared batch-ingestion workload, not a live-feed throughput model.
    totals = {name: sum(statistics.median(v) for v in metrics.values()) for name, metrics in trials.items()}
    winner = min(totals, key=totals.get)
    selected = winner if totals[winner] < totals["zstd_128k"] * 0.8 else "zstd_128k"
    report = {"passed": True, "snapshot": identity, "source_sha256": store.snapshot(identity).sha256,
              "rows": table.num_rows, "arrow_version": pa.__version__, "machine": platform.machine(),
              "implementation_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "trials": summary, "bytes": sizes, "selected": selected, "configuration": candidates[selected],
              "selection_policy": "equal weighted batch write/full read/projection; require >20% improvement",
              "repetitions": repetitions, "warmups": 2, "cache": "warm OS cache; no eviction",
              "memory_policy": "whole snapshot fits RAM; current captures only", "real_orders_enabled": False}
    (output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report
