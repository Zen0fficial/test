"""Real-data scheduling, process-restart and validation-gated registry acceptance."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from time import monotonic, sleep
from typing import Any, TextIO

from .online_parquet import MODEL_NAME, POOL, Registry, poll
from .qlib_parquet_updates import publish_capture
from .session import write_report
from .storage import SnapshotStore
from .storage.captures import ingest
from .workflows import TaskManager
from .workflows.acceptance import stop_worker


def spawn(root: Path, store: Path, output: Path, repo: Path, name: str,
          *, mode: str = "poll", polls: int = 1) -> tuple[subprocess.Popen, TextIO]:
    """Launch an owned shared-environment process with bounded numerical threads."""
    environment = dict(os.environ)
    environment.update({key: "1" for key in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
                                              "NUMEXPR_NUM_THREADS")})
    environment["LOKY_MAX_CPU_COUNT"] = "2"
    command = [sys.executable, "-m", "futu_bridge.joint.online_parquet", mode, "--root", str(root),
               "--store", str(store), "--output", str(output), "--polls", str(polls), "--interval", "1"]
    log = (root / f"{name}.log").open("w", encoding="utf-8")
    process = subprocess.Popen(command, cwd=repo, env=environment, stdout=log, stderr=subprocess.STDOUT,
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    return process, log


def finish(process: subprocess.Popen, log: TextIO, *, timeout: float = 120) -> None:
    """Bound execution and clean up only the subprocess tree this verifier owns."""
    try:
        if process.wait(timeout=timeout) != 0:
            raise RuntimeError(f"Online worker failed; inspect {log.name}")
    finally:
        if process.poll() is None:
            stop_worker(process)
        log.close()


def run(response: Path, output: Path, repo: Path) -> dict[str, Any]:
    """Kill/restart polling, train real snapshot tasks and verify promotion/rollback/reconciliation."""
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    source_paths = [Path(__file__), Path(__file__).with_name("online_parquet.py")]
    sources = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in source_paths}
    store = SnapshotStore(output / "store")
    first = ingest(store, response, "hk00700_daily", "futu-bars")
    marker = output / "first_poll.json"
    process, log = spawn(output, store.root, marker, repo, "interrupted_scheduler", polls=100)
    deadline = monotonic() + 90
    try:
        while not marker.exists():
            if process.poll() is not None or monotonic() >= deadline:
                raise RuntimeError("Scheduler did not durably create its first observed task")
            sleep(.1)
        original_poll = json.loads(marker.read_text())
        terminated = [process.pid, *stop_worker(process)]
    finally:
        if process.poll() is None:
            stop_worker(process)
        log.close()
    restarted_marker = output / "restarted_poll.json"
    restarted, restarted_log = spawn(output, store.root, restarted_marker, repo, "restarted_scheduler")
    finish(restarted, restarted_log)
    restarted_poll = json.loads(restarted_marker.read_text())
    if original_poll != restarted_poll:
        raise ValueError("A restarted scheduler duplicated or changed the same snapshot/configuration task")
    later = repo / "output/joint/ml_comparison_20261109/comparison/provider_response.json"
    update = publish_capture(store.root, first.identity, later)
    latest = poll(output, store.root, "hk00700_daily")
    repeated = poll(output, store.root, "hk00700_daily")
    if latest != repeated or latest["task_id"] == original_poll["task_id"]:
        raise ValueError("New-data scheduling or repeated polling did not preserve task identity")
    manager = TaskManager(POOL, output / "tasks")
    if len(list(manager.query())) != 2:
        raise ValueError("Two observed snapshots must schedule exactly two accepted definitions")
    process, log = spawn(output, store.root, output / "worker_unused.json", repo, "native_worker", mode="worker")
    finish(process, log, timeout=180)
    tasks = list(manager.query())
    if len(tasks) != 2 or any(task["status"] != "done" for task in tasks):
        raise ValueError("The durable online worker did not finish both native learning tasks")
    registry = Registry(output)
    identities = [original_poll["task_id"], latest["task_id"]]
    accepted = registry.accepted(identities[0])
    for tag in ("joint_snapshot_id", "joint_snapshot_sha256", "joint_configuration_hash"):
        original = registry.client.get_run(accepted["res"].id).data.tags[tag]
        registry.client.set_tag(accepted["res"].id, tag, "invalid-acceptance-lineage")
        try:
            registry.accepted(identities[0])
        except ValueError:
            pass
        else:
            raise ValueError(f"Registry accepted an invalid {tag}")
        finally:
            registry.client.set_tag(accepted["res"].id, tag, original)
    scores = {identity: registry.evaluate(identity, latest["task_id"]) for identity in identities}
    ordered = sorted(identities, key=lambda identity: (scores[identity]["mse"], identity))
    best, weaker = ordered[0], ordered[-1]
    if scores[best]["mse"] == scores[weaker]["mse"]:
        raise ValueError("This capture cannot distinguish the promotion/rejection scenarios")
    # This isolated acceptance intentionally bootstraps the weaker validated
    # candidate, then proves only the actual better common-window score can replace it.
    initial = registry.decide(weaker, latest["task_id"])
    promotion = registry.decide(best, latest["task_id"])
    rejected = registry.decide(weaker, latest["task_id"])
    rollback = registry.decide(weaker, latest["task_id"], rollback=True)
    restored = registry.decide(best, latest["task_id"])
    if not all(item["approved"] for item in (initial, promotion, rollback, restored)) or rejected["approved"]:
        raise ValueError("Validation gating or explicit accepted-version rollback failed")
    best_version, weaker_version = registry.register(best), registry.register(weaker)
    registry.client.set_registered_model_alias(MODEL_NAME, "champion", weaker_version)
    recovered = Registry(output)
    recovered.reconcile()
    actual = recovered.client.get_model_version_by_alias(MODEL_NAME, "champion")
    if str(actual.version) != best_version:
        raise ValueError("Restart reconciliation failed to repair an interrupted alias publication")
    with registry.connection() as database:
        decisions = database.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
    if sources != {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in source_paths}:
        raise ValueError("Online scheduler source changed during acceptance")
    manager.reconcile()
    report = {"passed": True, "source_hashes": sources, "real_orders_enabled": False,
              "providers_modified": False, "scheduler_killed_processes": terminated,
              "restart_task_identity_exact": True, "snapshots": [first.identity, latest["snapshot_id"]],
              "real_capture_update": update, "task_ids": identities, "native_models_trained": 2,
              "task_states": manager.task_stat(), "common_validation": scores,
              "invalid_lineage_tags_rejected": 3,
              "promotion": promotion, "rejected_regression": rejected, "rollback": rollback,
              "alias_reconciliation_passed": True, "champion_version": best_version,
              "registry_decisions": decisions, "native_generator": "RollingGen",
              "scope": "Restartable local research polling, native fitting and local registry aliases",
              "limitations": ["Replay of genuine historical snapshots, not sustained live-operation evidence",
                              "Historical bar availability is unknown; no point-in-time claim",
                              "Single-host coordination; no multi-host or live trading support",
                              "Acceptance starts with weaker candidate only to exercise the validation gate"]}
    write_report(output / "report.json", report)
    return report


def main() -> None:
    """Run the bounded scheduler conformance workflow in the existing shared environment."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--response", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    args = parser.parse_args()
    run(args.response, args.output, args.repo)


if __name__ == "__main__":
    main()
