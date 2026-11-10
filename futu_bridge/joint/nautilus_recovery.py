"""Killed-process recovery by deterministic replay of an immutable native simulation.

This replays the prefix into a fresh BacktestEngine, validates the economic state
at its committed boundary, and only then continues the suffix. It reconstructs
internal matching, liquidity consumption, timers and latency queues through
native processing; it does not serialize private engine memory or Trader.save.
Recovery is O(prefix length). All execution remains offline and simulated.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pandas as pd
from nautilus_trader.model.data import CustomData, DataType

from .lifecycle_nautilus import SessionClock
from .nautilus_features_contingent import make_task
from .nautilus_order_extensions import build, feed, observations, partial_task, state
from .paper_common import capture, utc_ns
from .session import write_report

SOURCES = ("nautilus_recovery.py", "nautilus_order_extensions.py", "nautilus_features_contingent.py",
           "lifecycle_nautilus.py", "lifecycle.py", "costs.py", "paper_common.py", "qlib_rl_data.py")


def digest(payload: bytes) -> str:
    """Return a stable content identity for immutable replay inputs."""
    return hashlib.sha256(payload).hexdigest()


def encoded(value: object) -> bytes:
    """Serialize canonical finite JSON so state and manifests have portable content hashes."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def bindings(repo: Path) -> dict[str, str]:
    """Bind replay to the relevant exact converter code and the unchanged shared runtime lock."""
    paths = [repo / "futu_bridge" / "joint" / name for name in SOURCES]
    paths.extend(sorted((repo / "requirements").glob("*.lock")))
    result = {str(p.relative_to(repo)): digest(p.read_bytes()) for p in paths}
    result["nautilus_trader_version"] = importlib.metadata.version("nautilus_trader")
    result["python_version"] = sys.version
    return result


def durable(path: Path, value: object) -> None:
    """Publish a flushed immutable JSON artifact, refusing an existing destination."""
    temporary = path.with_suffix(path.suffix + ".pending")
    with temporary.open("xb") as stream:
        stream.write(encoded(value))
        stream.flush()
        os.fsync(stream.fileno())
    if path.exists():
        raise FileExistsError(path)
    temporary.rename(path)


def prepare(task: dict[str, object], boundary_ns: int, directory: Path, repo: Path,
            provenance: dict[str, object]) -> Path:
    """Freeze market observations and prescribed commands as separate hash-bound replay inputs."""
    directory.mkdir(parents=True, exist_ok=False)
    command = {k: v for k, v in task.items() if k != "quotes"}
    durable(directory / "market.json", task["quotes"])
    durable(directory / "commands.json", command)
    manifest = {"schema": 1, "boundary_ns": boundary_ns, "source": provenance, "bindings": bindings(repo),
        "inputs": {name: digest((directory / name).read_bytes()) for name in ("market.json", "commands.json")},
        "real_orders_enabled": False, "recovery": "deterministic_prefix_replay", "python_hash_seed": "0"}
    durable(directory / "manifest.json", manifest)
    return directory / "manifest.json"


def load(manifest_path: Path, repo: Path) -> tuple[dict[str, object], dict[str, object]]:
    """Reject altered journals, unknown schema and code/runtime drift before reconstructing any orders."""
    manifest = json.loads(manifest_path.read_bytes())
    if (manifest.get("schema") != 1 or manifest.get("real_orders_enabled") is not False
            or manifest.get("recovery") != "deterministic_prefix_replay" or manifest["bindings"] != bindings(repo)):
        raise ValueError("Recovery manifest schema, source bindings or offline contract changed")
    if set(manifest["inputs"]) != {"market.json", "commands.json"}:
        raise ValueError("Recovery requires exact market and command journal membership")
    inputs = {}
    for name, wanted in manifest["inputs"].items():
        payload = (manifest_path.parent / name).read_bytes()
        if digest(payload) != wanted:
            raise ValueError("Recovery input checksum mismatch: " + name)
        inputs[name] = json.loads(payload)
    task = {**inputs["commands.json"], "quotes": inputs["market.json"]}
    return manifest, task


def checkpoint(manifest_path: Path, boundary: int, actual: dict[str, object]) -> dict[str, object]:
    """Bind the verified boundary state to exact replay input bytes and the effective cursor."""
    frozen = json.loads(encoded(actual))
    return {"manifest_sha256": digest(manifest_path.read_bytes()), "boundary_ns": boundary,
            "state_sha256": digest(encoded(frozen)), "state": frozen}


def worker(manifest_path: Path, checkpoint_path: Path, result_path: Path, repo: Path, mode: str) -> None:
    """Reconstruct native state, optionally pause for a real process kill, or verify and resume its suffix."""
    manifest, task = load(manifest_path, repo)
    if os.environ.get("PYTHONHASHSEED") != manifest.get("python_hash_seed"):
        raise ValueError("Recovery requires the committed Python hash seed before interpreter startup")
    boundary = manifest["boundary_ns"]
    values = observations(task)
    marker = CustomData(DataType(SessionClock), SessionClock(boundary))
    values.append(marker)
    values.sort(key=lambda x: x.ts_init)
    if mode == "reference":
        engine, strategy, fees = build(task)
        try:
            feed(engine, values)
            result = state(engine, strategy, fees)
            engine.end()
        finally:
            engine.dispose()
        durable(result_path, {"state": result, "real_orders_enabled": False})
        return
    prefix, suffix = [x for x in values if x.ts_init <= boundary], [x for x in values if x.ts_init > boundary]
    if not prefix or not suffix:
        raise ValueError("A recovery boundary must split nonempty observation batches")
    engine, strategy, fees = build(task)
    try:
        feed(engine, prefix)
        boundary_state = state(engine, strategy, fees)
        current = checkpoint(manifest_path, boundary, boundary_state)
        if mode == "interrupt":
            durable(checkpoint_path, current)
            threading.Event().wait()
            raise RuntimeError("Interrupted worker unexpectedly resumed without a new process")
        if mode != "resume":
            raise ValueError("Unknown native replay worker mode")
        saved = json.loads(checkpoint_path.read_bytes())
        if saved != current:
            raise ValueError("Reconstructed native boundary differs; suffix execution refused")
        counts = {k: len(boundary_state[k]) for k in ("events", "updates", "exposure", "commands")}
        feed(engine, suffix)
        final_state = state(engine, strategy, fees)
        engine.end()
        durable(result_path, {"state": final_state, "boundary_verified": True, "checkpoint": current,
                             "suffix": {k: final_state[k][n:] for k, n in counts.items()},
                             "real_orders_enabled": False})
    finally:
        engine.dispose()


def command(repo: Path, manifest: Path, checkpoint_path: Path, result: Path, mode: str) -> list[str]:
    """Build an argument-vector-only worker invocation using the current shared Python interpreter."""
    return [sys.executable, "-m", "futu_bridge.joint.nautilus_recovery", "--worker", mode,
            "--manifest", str(manifest), "--checkpoint", str(checkpoint_path), "--result", str(result),
            "--repo", str(repo)]


def prove(manifest: Path, repo: Path) -> dict[str, object]:
    """Kill one worker after durable checkpoint publication and compare independent resumed/reference processes."""
    directory = manifest.parent
    saved, reference, recovered = (directory / name for name in (
        "checkpoint.json", "reference.json", "recovered.json"))
    thread_limits = {k: "1" for k in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
                                     "NUMEXPR_NUM_THREADS")}
    env = {**os.environ, "PYTHONHASHSEED": "0", **thread_limits}
    with (directory / "interrupted.log").open("wb") as log:
        process = subprocess.Popen(command(repo, manifest, saved, recovered, "interrupt"), cwd=repo,
                                   env=env, stdout=log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 60
        try:
            while not saved.exists():
                if process.poll() is not None:
                    raise RuntimeError("Recovery prefix worker exited before its checkpoint; inspect interrupted.log")
                if time.monotonic() > deadline:
                    raise TimeoutError("Recovery prefix worker did not publish a checkpoint within 60 seconds")
                time.sleep(.02)
            process.kill()
            killed_code = process.wait(timeout=15)
            if killed_code == 0:
                raise RuntimeError("Recovery test did not actually interrupt a live worker")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=15)
    for mode, result in (("reference", reference), ("resume", recovered)):
        with (directory / (mode + ".log")).open("wb") as log:
            completed = subprocess.run(command(repo, manifest, saved, result, mode), cwd=repo, env=env,
                                       stdout=log, stderr=subprocess.STDOUT, timeout=60, check=False)
        if completed.returncode:
            raise RuntimeError("Recovery " + mode + " failed; inspect " + str(directory / (mode + ".log")))
    ref, resumed = json.loads(reference.read_bytes()), json.loads(recovered.read_bytes())
    if not resumed.get("boundary_verified") or ref["state"] != resumed["state"]:
        raise ValueError("Recovered native suffix differs from uninterrupted real-data simulation")
    if resumed["state"]["failures"] or resumed["state"]["real_orders_enabled"] is not False:
        raise ValueError("Recovered simulation has native failures or real routing")
    prefix = resumed["checkpoint"]["state"]
    for key, suffix in resumed["suffix"].items():
        if prefix[key] + suffix != ref["state"][key]:
            raise ValueError("Recovered journal is missing or duplicating suffix events")
    return {"passed": True, "killed_process_exit_code": killed_code, "boundary_verified": True,
            "exact_final_state": True, "exact_suffix": True, "prefix_events": len(prefix["events"]),
            "suffix_events": len(resumed["suffix"]["events"]), "active_timers": prefix["timers"],
            "working_orders": {k: v for k, v in prefix["orders"].items()
                               if v["status"] not in ("FILLED", "CANCELED", "REJECTED")}}


def cases(frame: pd.DataFrame) -> dict[str, tuple[dict[str, object], int]]:
    """Select interruption boundaries with partial inventory, queued insertion/cancel/amend and native timers."""
    start = utc_ns(str(frame.index[0]))
    result = {}
    for name, seconds in (("cancel_before_next_quote", .5), ("fill_before_delayed_cancel", 30),
                          ("delayed_insert", 61)):
        result[name] = (make_task(frame, name), start + int(seconds * 1_000_000_000) + 1)
    partial = partial_task(frame)
    result["partial_parent_with_active_children"] = (partial, start + 30_000_000_001)
    amended = partial_task(frame, delayed_amend=True)
    result["queued_child_amendment"] = (amended, utc_ns(amended["modify"]) + 30_000_000_001)
    timer = make_task(frame, "fill_before_delayed_cancel")
    timer["case"] = "timer_cancel"
    result["native_timer_and_later_cancel"] = (timer, start + 1_000_000_001)
    return result


def run(response: Path, output: Path, repo: Path) -> dict[str, object]:
    """Prove process-loss recovery against independent uninterrupted executions on five real HK sessions."""
    repo, response, output = repo.resolve(), response.resolve(), output.resolve()
    data = capture(response)
    output.mkdir(parents=True, exist_ok=False)
    results = {}
    for day in data.partitions["test"]:
        for name, (task, boundary) in cases(data.minutes.loc[day]).items():
            key = day + "_" + name
            manifest = prepare(task, boundary, output / key, repo, data.provenance)
            results[key] = prove(manifest, repo)
            write_report(output / "progress.json", {"completed": len(results), "last": key})
    days = data.partitions["test"][:2]
    overnight = partial_task(data.minutes.loc[days[0]])
    overnight["quotes"] += make_task(data.minutes.loc[days[1]], "ouo_partial_exit")["quotes"]
    for key in ("modify", "cancel"):
        overnight[key] = overnight[key].replace(days[0], days[1])
    boundary = utc_ns(days[0] + " 15:59:00") + 1
    manifest = prepare(overnight, boundary, output / "overnight_gtc", repo, data.provenance)
    results["overnight_gtc"] = prove(manifest, repo)
    report = {"passed": True, "scenarios": len(results), "cases": results, "data": data.provenance,
        "bindings": bindings(repo), "recovery_method": "immutable_command_and_market_prefix_replay",
        "real_orders_enabled": False, "providers_modified": False,
        "limitations": ["Recovery replays from genesis and is linear in prefix size",
                        "Private native queues are reconstructed through replay, not serialized or directly inspected",
                        "Only this deterministic strategy, latency model and integral-lot venue are certified",
                        "No live broker reconciliation, live data gap recovery or externally sent order recovery"]}
    write_report(output / "report.json", report)
    return report


def main() -> None:
    """Expose the offline coordinator and its isolated recovery workers without any live routing option."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--response", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--worker", choices=("interrupt", "resume", "reference"))
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--checkpoint", type=Path)
    parser.add_argument("--result", type=Path)
    args = parser.parse_args()
    if args.worker:
        if any(x is None for x in (args.manifest, args.checkpoint, args.result)):
            parser.error("Worker mode requires manifest, checkpoint and result paths")
        worker(args.manifest, args.checkpoint, args.result, args.repo.resolve(), args.worker)
    else:
        if args.response is None or args.output is None:
            parser.error("Coordinator mode requires response and output paths")
        print(json.dumps(run(args.response, args.output, args.repo), indent=2))


if __name__ == "__main__":
    main()
