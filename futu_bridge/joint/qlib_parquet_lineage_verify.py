"""End-to-end acceptance of real capture partitions, canonical RL and independently audited ML portfolios."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

import pandas as pd

from .qlib_parquet_execution import execution_from_snapshot
from .qlib_parquet_lineage import execution_reference, verify
from .qlib_parquet_updates import publish_capture
from .qlib_rl_data import load_data
from .session import write_report
from .storage import SnapshotStore
from .storage.captures import futu_table


def prefix(store: SnapshotStore, response: Path, dataset: str, count: int) -> str:
    """Publish a declared structural partition while retaining the exact complete original source bytes."""
    payload = response.read_bytes()
    digest = store.archive(payload)
    return store.publish(dataset, futu_table(payload).slice(0, count),
        {"capture_sha256": digest, "capture_selections": {digest: [[0, count]]},
         "acceptance_scope": "Structural source partition; original prices and capture bytes unchanged"},
        expected_head=None).identity


def execute(arguments: list[str], log_path: Path, repo: Path, timeout: int = 1800) -> None:
    """Run public shared-Python entrypoints with fixed numerical threads and unchanged training budgets."""
    environment = {**os.environ, **{name: "1" for name in
        ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS")},
        "LOKY_MAX_CPU_COUNT": "2", "PYTHONHASHSEED": "0"}
    with log_path.open("w", encoding="utf-8") as log:
        subprocess.run([sys.executable, "-m", *arguments], cwd=repo, env=environment,
                       stdout=log, stderr=subprocess.STDOUT, timeout=timeout, check=True)


def run(output: Path, repo: Path) -> dict[str, Any]:
    """Exercise actual source unions through default-budget native RL and the full daily ML comparison."""
    from .qlib_rl_verify import verify as verify_rl

    output, repo = output.resolve(), repo.resolve()
    output.mkdir(parents=True, exist_ok=False)
    names = ("qlib_parquet_lineage.py", "qlib_parquet_execution.py", "qlib_parquet_updates.py",
             "qlib_rl_data.py", "parquet_training.py", "qlib_rl_verify.py", "comparison.py", "comparison_verify.py",
             "qlib_parquet_lineage_verify.py")
    sources = {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest() for name in names}
    store = SnapshotStore(output / "store")
    minute = repo / "output/joint/qlib_native_rl_data_20261109/provider_response.json"
    daily = repo / "output/joint/model_20261109/model/provider_response.json"
    recent = repo / "output/joint/ml_comparison_20261109/comparison/provider_response.json"
    minute_old = prefix(store, minute, "minute_partial", 3972)
    minute_new = publish_capture(store.root, minute_old, minute, source_rows=list(range(3972, 6289)))
    minute_id = minute_new["snapshot"]["identity"]
    original, canonical = load_data(minute), execution_from_snapshot(store.root, minute_id)
    independent = execution_reference(store, minute_id)
    if canonical.provenance != independent.provenance or canonical.partitions != original.partitions:
        raise ValueError("Real minute source partition changed native provenance or partitions")
    for day in original.blocks:
        pd.testing.assert_frame_equal(canonical.blocks[day], original.blocks[day], check_exact=True)
    daily_old = prefix(store, daily, "daily_partial", 1000)
    daily_new = publish_capture(store.root, daily_old, recent, source_rows=list(range(1000, 1685)))
    daily_id = daily_new["snapshot"]["identity"]
    lineage = verify(store, daily_id)
    if len(lineage.envelopes) != 2 or len(lineage.rows) != 1685:
        raise ValueError("Daily acceptance requires the actual two-capture partition union")
    native = {}
    for algorithm in ("simple", "nested"):
        target = output / algorithm
        execute(["futu_bridge.joint.parquet_training", "--algorithm", algorithm, "--store", str(store.root),
                 "--snapshot", minute_id, "--output", str(target)], output / f"{algorithm}.log", repo)
        report = json.loads((target / "report.json").read_text())
        if not report["canonical_input_verification"].get("passed"):
            raise ValueError("Native training omitted independent original-array verification")
        native[algorithm] = {"path": str(target / "report.json"),
                             "sha256": hashlib.sha256((target / "report.json").read_bytes()).hexdigest(),
                             "input": report["canonical_input_verification"]}
        if algorithm == "simple":
            native[algorithm]["independent_account_verification"] = verify_rl(target)
    comparison = output / "comparison"
    execute(["futu_bridge.joint.comparison", "--response", str(recent),
             "--baseline-dir", str(daily.parent), "--output", str(comparison),
             "--qlib-source", str(repo / "tmp/qlib-v0.9.7"), "--store", str(store.root),
             "--snapshot", daily_id], output / "comparison.log", repo)
    compared = json.loads((comparison / "report.json").read_text())
    if not compared.get("independent_verification", {}).get("passed"):
        raise ValueError("Daily model simulation did not pass independent decision and cash verification")
    previous = repo / "output/joint/plan_completion_20261110/legacy_comparison"
    old = pd.read_csv(previous / "predictions.csv", index_col=0, float_precision="round_trip")
    current = pd.read_csv(comparison / "predictions.csv", index_col=0, float_precision="round_trip")
    pd.testing.assert_frame_equal(old, current, check_exact=True)
    if sources != {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest() for name in names}:
        raise ValueError("Canonical lineage or account verifier changed during acceptance")
    result = {"passed": True, "source_hashes": sources, "sources_unchanged_during_verification": True,
              "minute_snapshot": minute_id, "daily_snapshot": daily_id, "native": native,
              "daily_original_sources": len(lineage.envelopes), "daily_rows": len(lineage.rows),
              "comparison_report": str(comparison / "report.json"), "prediction_legacy_parity_exact": True,
              "daily_independent_verification": compared["independent_verification"],
              "original_archives_unchanged": store.inspect()["passed"], "real_orders_enabled": False,
              "providers_modified": False,
              "scope": "Explicit partitions of real immutable captures; no fabricated market observations"}
    write_report(output / "report.json", result)
    return result


def main() -> None:
    """Run the complete original-source union and native training acceptance in the shared environment."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    args = parser.parse_args()
    run(args.output, args.repo)


if __name__ == "__main__":
    main()
