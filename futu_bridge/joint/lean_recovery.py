"""Deterministic process-loss recovery for native offline LEAN execution.

Freeze market files and commands, kill a native .NET worker inside its durable
boundary callback, and reconstruct through replay in a fresh process. Native
scheduled actions and private fill-model latches are reconstructed by the same
event sequence, never described as serialized full-memory snapshots. All trades
are simulated; providers and the shared environment remain unchanged.
"""

from __future__ import annotations

import argparse
from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

import pandas as pd

from .lifecycle import quotes
from .paper_common import capture
from .paper_lean import build as build_paper, export
from .qlib_rl_data import ExecutionData
from .session import write_report

CASES = ("partial_limit_cancel", "partial_limit_amend", "working_stop_amend", "latched_stop_limit_amend",
         "working_trailing_amend", "trailing_partial_remainder")


def digest(path: Path) -> str:
    """Hash a frozen replay input or native binary without loading the entire file into memory."""
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def task_for(frame: pd.DataFrame, case: str) -> dict[str, object]:
    """Create causal initial-price instructions and future scheduled actions without future price selection."""
    if case not in CASES:
        raise ValueError("Unknown native LEAN recovery case")
    day = str(frame.index[0].date())
    initial = quotes(frame)[0]
    ask, bid = Decimal(initial["ask"]), Decimal(initial["bid"])
    kind = "limit" if "limit" in case and "stop_limit" not in case else "stop"
    if "stop_limit" in case:
        kind = "stop_limit"
    if "trailing" in case:
        kind = "trailing"
    task = {"case": case, "day": day, "submit": initial["time"], "kind": kind, "shares": 200,
            "price": str(ask + 10), "trigger": str(ask + 20), "offset": "20",
            "checkpoint_phase": "partial_fill" if case.startswith("partial")
            or case == "trailing_partial_remainder" else "scheduled",
            "checkpoint_at": day + " 09:30:30",
            "actions": [{"id": "final_cancel", "kind": "cancel", "time": day + " 15:29:00"}]}
    if case == "partial_limit_cancel":
        task["actions"].insert(0, {"id": "delayed_cancel", "kind": "cancel", "time": day + " 09:31:45"})
    elif case == "partial_limit_amend":
        task["actions"].insert(0, {"id": "delayed_amend", "kind": "amend", "time": day + " 09:31:45",
                                    "price": str(bid - 20)})
    elif case == "working_stop_amend":
        task["actions"].insert(0, {"id": "delayed_amend", "kind": "amend", "time": day + " 09:30:45",
                                    "trigger": str(ask)})
    elif case == "latched_stop_limit_amend":
        task.update(trigger=str(ask - 20), price=str(bid - 20), checkpoint_at=day + " 09:31:30")
        task["actions"].insert(0, {"id": "delayed_amend", "kind": "amend", "time": day + " 09:31:45",
                                    "price": str(ask + 10)})
    elif case == "working_trailing_amend":
        task["actions"].insert(0, {"id": "delayed_amend", "kind": "amend", "time": day + " 09:30:45",
                                    "trigger": str(ask + Decimal(".4")), "offset": ".4"})
    else:
        task.update(trigger=str(ask + Decimal(".4")), offset=".4")
    return task


def build(repo: Path, source: Path, dotnet: Path, output: Path) -> None:
    """Compile the new converter class alongside unchanged existing offline account/fill adapters."""
    for filename, target in (("lean_recovery.cs", "LocalHKRecoveryAlgorithm.cs"),
                             ("lean_lifecycle.cs", "LocalHKLifecycleAlgorithm.cs"),
                             ("lean_order_extensions.cs", "LocalHKOrderExtensionsAlgorithm.cs")):
        shutil.copyfile(repo / "futu_bridge/converters" / filename, source / "Algorithm.CSharp" / target)
    build_paper(repo, source, dotnet, output)


def freeze(data: ExecutionData, response: Path, output: Path, repo: Path, source: Path) -> Path:
    """Export shared immutable real observations and clock-only events for all holdout sessions once."""
    directory = output / "inputs"
    export(data, directory, source, data.partitions["test"])
    (directory / "commands.json").write_text("[]", encoding="utf-8")
    controls = [day + " " + instant for day in data.partitions["test"]
                for instant in ("09:30:30", "09:30:45", "09:31:30", "09:31:45", "16:00:00")]
    (directory / "data/recovery-clock.csv").write_text("\n".join(controls) + "\n", encoding="utf-8")
    shutil.copyfile(response, directory / "provider_response.json")
    return directory


def prepare(task: dict[str, object], directory: Path, inputs: Path, repo: Path, source: Path) -> Path:
    """Bind one replay scenario to real files, exact converter code, native binaries and the shared lock."""
    directory.mkdir(parents=True, exist_ok=False)
    write_report(directory / "task.json", task)
    files = [directory / "task.json", inputs / "provider_response.json", inputs / "commands.json",
             inputs / "config.json"]
    files.extend(path for path in (inputs / "data").rglob("*") if path.is_file())
    files.extend(repo / "futu_bridge/converters" / name for name in (
        "lean_recovery.cs", "lean_order_extensions.cs", "lean_lifecycle.cs", "lean_paper.cs"))
    files.extend(repo / "futu_bridge/joint" / name for name in (
        "lean_recovery.py", "paper_lean.py", "paper_common.py", "qlib_rl_data.py", "costs.py"))
    files.append(repo / "requirements/joint-py312-windows.lock")
    files.extend(source / "Launcher/bin/Release" / name for name in (
        "QuantConnect.Lean.Launcher.dll", "QuantConnect.Algorithm.CSharp.dll", "QuantConnect.Algorithm.dll",
        "QuantConnect.Common.dll", "QuantConnect.Lean.Engine.dll"))
    manifest = {"schema": 1, "real_orders_enabled": False, "recovery": "native_lean_prefix_replay",
                "files": {str(path.resolve()): digest(path) for path in files},
                "input_directory": str(inputs.resolve()), "task": str((directory / "task.json").resolve()),
                "data_directory": str((inputs / "data").resolve()), "day": task["day"], "capital": "100000"}
    write_report(directory / "manifest.json", manifest)
    return directory / "manifest.json"


def verify(manifest: Path) -> dict[str, object]:
    """Reject input, code or runtime drift before starting another native simulation process."""
    document = json.loads(manifest.read_bytes())
    if (document.get("schema") != 1 or document.get("real_orders_enabled") is not False
            or document.get("recovery") != "native_lean_prefix_replay"):
        raise ValueError("Invalid native LEAN recovery contract")
    for filename, wanted in document["files"].items():
        if digest(Path(filename)) != wanted:
            raise ValueError("Native LEAN recovery checksum mismatch: " + filename)
    return document


def configuration(manifest: Path, mode: str, source: Path) -> tuple[Path, Path, Path]:
    """Create an isolated worker output/config around the same immutable native market and command files."""
    document = verify(manifest)
    task = json.loads(Path(document["task"]).read_bytes())
    inputs, output = Path(document["input_directory"]), manifest.parent / mode
    output.mkdir(parents=True, exist_ok=False)
    config = json.loads((inputs / "config.json").read_bytes())
    result = output / "native.json"
    saved = manifest.parent / "interrupt/checkpoint.json" if mode == "resume" else output / "checkpoint.json"
    config.update({"algorithm-type-name": "LocalHKRecoveryAlgorithm",
                   "algorithm-location": str(source / "Launcher/bin/Release/QuantConnect.Algorithm.CSharp.dll"),
                   "results-destination-folder": str(output / "results"),
                   "object-store-root": str(output / "storage")})
    config["parameters"].update({"capture-start": task["day"], "capture-end": task["day"],
        "task": document["task"], "verification-output": str(result), "recovery-mode": mode,
        "recovery-manifest": str(manifest), "recovery-manifest-sha256": digest(manifest),
        "recovery-checkpoint": str(saved)})
    write_report(output / "config.json", config)
    return output / "config.json", saved, result


def arguments(dotnet: Path, source: Path, config: Path) -> list[str]:
    """Invoke only the pinned native backtesting launcher using an argument vector, never a shell."""
    return [str(dotnet), str(source / "Launcher/bin/Release/QuantConnect.Lean.Launcher.dll"),
            "--config", str(config)]


def prove(manifest: Path, source: Path, dotnet: Path) -> dict[str, object]:
    """Kill a native worker before cleanup and compare independently restarted and uninterrupted executions."""
    config, checkpoint, interrupted_result = configuration(manifest, "interrupt", source)
    env = {**os.environ, **{k: "1" for k in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS")}}
    with (config.parent / "engine.log").open("wb") as log:
        process = subprocess.Popen(arguments(dotnet, source, config), env=env, stdout=log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 90
        try:
            while not checkpoint.is_file():
                if process.poll() is not None:
                    raise RuntimeError("Native interrupt worker exited before checkpoint; inspect " + str(log.name))
                if time.monotonic() > deadline:
                    raise TimeoutError("Native interrupt worker did not publish a checkpoint within 90 seconds")
                time.sleep(.02)
            if interrupted_result.exists():
                raise ValueError("The interrupt worker completed cleanup before the process kill")
            process.kill()
            killed = process.wait(timeout=15)
            if killed == 0:
                raise ValueError("Native recovery did not actually kill a running process")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=15)
    results = {}
    for mode in ("reference", "resume"):
        config, _, result = configuration(manifest, mode, source)
        with (config.parent / "engine.log").open("wb") as log:
            completed = subprocess.run(arguments(dotnet, source, config), env=env, stdout=log,
                                       stderr=subprocess.STDOUT, timeout=120, check=False)
        if completed.returncode or not result.is_file():
            raise RuntimeError("Native " + mode + " worker failed; inspect " + str(config.parent / "engine.log"))
        results[mode] = json.loads(result.read_bytes())
    saved = json.loads(checkpoint.read_bytes())
    reference, resumed = results["reference"], results["resume"]
    if (not reference["boundary_verified"] or not resumed["boundary_verified"]
            or reference["checkpoint"] != saved or resumed["checkpoint"] != saved
            or reference["state"] != resumed["state"] or reference["suffix"] != resumed["suffix"]):
        raise ValueError("Native LEAN reconstruction boundary, suffix or final state differs")
    if saved["state"]["events"] + resumed["suffix"] != resumed["state"]["events"]:
        raise ValueError("Native LEAN resumed event journal lost or duplicated events")
    if resumed["state"]["failures"] or resumed["real_orders_enabled"] is not False:
        raise ValueError("Native recovered run contains failures or real routing")
    return {"passed": True, "killed_process_exit_code": killed, "boundary_verified": True,
            "exact_suffix": True, "exact_final_state": True, "interrupted_before_cleanup": True,
            "boundary": saved["state"], "suffix_events": len(resumed["suffix"])}


def audit_account(result: dict[str, object], frame: pd.DataFrame, task: dict[str, object]) -> dict[str, object]:
    """Validate real-observation fills and independent cumulative fees/cash without trusting native balances."""
    cash, holding, notional, charged = Decimal(100000), Decimal(0), Decimal(0), Decimal(0)
    fills = [x for x in result["state"]["events"] if x["type"] == "fill"]
    source = {x["time"]: Decimal(x["ask"]) for x in quotes(frame)}
    for event in fills:
        local = str(pd.Timestamp(event["time"], tz="UTC").tz_convert("Asia/Hong_Kong").tz_localize(None))
        quantity, value = Decimal(event["shares"]), Decimal(event["price"])
        if local not in source or quantity != 100:
            raise ValueError("Recovery filled on a non-market instant or exceeded declared one-lot capacity")
        if task["kind"] != "limit" and value != source[local]:
            raise ValueError("Native conditional recovery fill price differs from the actual captured derived ask")
        if task["kind"] == "limit" and value not in (source[local], Decimal(task["price"])):
            raise ValueError("Native recovery limit fill violates its declared side/limit-price policy")
        notional += quantity * value
        fee = (max(Decimal(18), notional * Decimal(".0005")) + notional * Decimal(".000127")
               + (notional * Decimal(".001")).to_integral_value(rounding=ROUND_CEILING)).quantize(
                   Decimal(".01"), rounding=ROUND_HALF_UP)
        if Decimal(event["fee"]) != fee - charged:
            raise ValueError("Native recovery duplicate/missing fee differs from independent cumulative charges")
        cash -= quantity * value + fee - charged
        holding += quantity
        charged = fee
        if cash < 0 or holding > task["shares"]:
            raise ValueError("Native recovery overfilled the request or exceeded funded cash")
    if Decimal(result["state"]["cash"]) != cash or Decimal(result["state"]["holding"]) != holding:
        raise ValueError("Native recovery final cash/inventory differs from independent actual-fill accounting")
    return {"passed": True, "fills": len(fills), "cash": str(cash), "holding": str(holding)}


def audit_boundary(state: dict[str, object], task: dict[str, object]) -> dict[str, object]:
    """Require each named interruption state to be exercised, not merely a passing restart at any boundary."""
    if len(state["orders"]) != 1 or not state["pending_commands"]:
        raise ValueError("Recovery boundary lacks its native ticket or pending scheduled commands")
    order = state["orders"][0]
    if Decimal(order["quantity"]) != 200:
        raise ValueError("Recovery boundary changed the original native order quantity")
    if task["checkpoint_phase"] == "partial_fill":
        if (order["status"] != "PartiallyFilled" or Decimal(order["filled"]) != 100
                or Decimal(state["holding"]) != 100 or len(state["events"]) != 1):
            raise ValueError("Partial recovery boundary must contain an actual one-lot fill and working remainder")
    elif order["status"] != "Submitted" or Decimal(order["filled"]) != 0 or Decimal(state["holding"]) != 0:
        raise ValueError("Working-order recovery boundary unexpectedly filled or closed its ticket")
    if task["case"] == "latched_stop_limit_amend" and order["lifecycle_triggered"] is not True:
        raise ValueError("Stop-limit recovery did not reach the already-triggered but unfilled native adapter state")
    current = pd.Timestamp(state["time"], tz="UTC").tz_convert("Asia/Hong_Kong").tz_localize(None)
    if any(pd.Timestamp(action["time"]) <= current for action in state["pending_commands"]):
        raise ValueError("Checkpoint mislabeled an already-due command as a future scheduled action")
    return {"passed": True, "status": order["status"], "filled": order["filled"],
            "pending_commands": len(state["pending_commands"]), "trigger_latched": order["lifecycle_triggered"]}


def run(response: Path, output: Path, repo: Path) -> dict[str, object]:
    """Prove native working-order, private-latch and scheduled-command reconstruction on five real HK sessions."""
    response, output, repo = response.resolve(), output.resolve(), repo.resolve()
    data = capture(response)
    output.mkdir(parents=True, exist_ok=False)
    source, dotnet = repo / "tmp/lean-src", repo / "tmp/dotnet/dotnet.exe"
    build(repo, source, dotnet, output)
    inputs = freeze(data, response, output, repo, source)
    results = {}
    for day in data.partitions["test"]:
        for case in CASES:
            key = day + "_" + case
            task = task_for(data.minutes.loc[day], case)
            manifest = prepare(task, output / key, inputs, repo, source)
            verified = prove(manifest, source, dotnet)
            verified["required_boundary"] = audit_boundary(verified["boundary"], task)
            actual = json.loads((manifest.parent / "resume/native.json").read_bytes())
            verified["independent_account"] = audit_account(actual, data.minutes.loc[day], task)
            results[key] = verified
            write_report(output / "progress.json", {"completed": len(results), "last": key})
    report = {"passed": True, "scenarios": len(results), "cases": results, "data": data.provenance,
        "real_orders_enabled": False, "providers_modified": False,
        "method": "Separate-process native LEAN prefix reconstruction; exact boundary/suffix/account verification",
        "limitations": ["Linear replay-from-genesis cost; no constant-time full-memory restore is claimed",
                        "Native scheduled actions and private fill-model latches are reconstructed, not serialized",
                        "Single HK equity, declared derived BBO and finite 100-share capacity",
                        "No external broker order recovery, live routing, multileg or options recovery"]}
    write_report(output / "report.json", report)
    return report


def main() -> None:
    """Expose only immutable real-capture offline recovery acceptance."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--response", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    args = parser.parse_args()
    print(json.dumps(run(args.response, args.output, args.repo), indent=2))


if __name__ == "__main__":
    main()
