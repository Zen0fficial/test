"""Explicitly adapted IOC/FOK parent intents executed immediately through native LEAN tickets.

Pinned LEAN has no IOC/FOK TimeInForce classes. This converter checks the current
observed simulated side quote and capacity, submits guarded native market tickets
for eligible intent, and cancels ineligible requests or IOC remainders immediately.
It never calls next-observation cancellation faithful immediate execution.
"""

from __future__ import annotations

import argparse
from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

import pandas as pd

from .lean_contingent import build as build_isolated, verify_bindings
from .lifecycle import quotes
from .paper_common import capture
from .paper_lean import LEAN_COMMIT, export
from .session import write_report
from .storage.snapshots import file_hash

KINDS = ("ioc_full", "ioc_partial", "ioc_unmarketable", "fok_full", "fok_insufficient", "fok_unmarketable")
CASES = tuple(kind + "_" + side for kind in KINDS for side in ("buy", "sell"))


def progress(path: Path, result: dict[str, object]) -> None:
    """Retry bounded Windows sharing conflicts for the nonauthoritative progress pointer only."""
    for attempt in range(8):
        try:
            write_report(path, result)
            return
        except PermissionError:
            if attempt == 7:
                raise
            time.sleep(0.05 * (attempt + 1))


def task_for(frame: pd.DataFrame, case: str) -> dict[str, object]:
    """Price a prescribed immediate intent solely from its current observed simulated quote."""
    if case not in CASES:
        raise ValueError("Unknown joint immediate intent case")
    day, side = str(frame.index[0].date()), case.rsplit("_", 1)[1]
    submit = day + " 10:00:00"
    point = next(point for point in quotes(frame) if point["time"] == submit)
    quote = Decimal(point["ask" if side == "buy" else "bid"])
    direction = Decimal(1) if side == "buy" else Decimal(-1)
    limit = quote + direction * (-10 if "unmarketable" in case else 10)
    quantity = 200 if "partial" in case or "insufficient" in case else 100
    return {"case": case, "day": day, "side": side, "mode": case[:3].upper(), "shares": quantity,
            "limit": str(limit), "submit": submit, "seed": day + " 09:30:00", "capacity": 100,
            "integration": "adapted_immediate_controller", "native_tif": "GTC"}


def build(repo: Path, output: Path) -> tuple[Path, dict[str, str]]:
    """Extend a private copied runtime using the existing isolated compiler template, never shared DLLs."""
    runtime, bindings = build_isolated(repo, output, converter="lean_combos.cs", assembly="LocalHKCombos")
    source = repo / "futu_bridge/converters/lean_immediate.cs"
    project = output / "immediate_extension"
    project.mkdir()
    shutil.copyfile(source, project / source.name)
    shutil.copyfile(repo / "futu_bridge/converters/lean_paper.cs", project / "lean_paper.cs")
    xml = (output / "extension/LocalHKCombos.csproj").read_text(encoding="utf-8")
    xml = xml.replace("LocalHKCombos", "LocalHKImmediate").replace("lean_combos.cs", "lean_immediate.cs")
    project_path = project / "LocalHKImmediate.csproj"
    project_path.write_text(xml, encoding="utf-8")
    bindings[str(source)] = file_hash(source)
    bindings[str(Path(__file__).resolve())] = file_hash(Path(__file__).resolve())
    with (output / "immediate-build.log").open("w", encoding="utf-8") as log:
        subprocess.run([str(repo / "tmp/dotnet/dotnet.exe"), "build", str(project_path), "-c", "Release",
                        "-o", str(runtime), "--nologo", "-v", "quiet", "--ignore-failed-sources"],
                       cwd=repo, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=180)
    verify_bindings(bindings)
    path = runtime / "LocalHKImmediate.dll"
    bindings[str(path)] = file_hash(path)
    return runtime, bindings


def expected(task: dict[str, object], frame: pd.DataFrame) -> list[dict[str, object]]:
    """Derive immediate eligibility and all-or-none quantity from actual prices without native journals."""
    source = {point["time"]: point for point in quotes(frame)}
    result = []
    if task["side"] == "sell":
        for time in sorted(source)[:2]:
            result.append({"id": "SEED", "time": time, "shares": 100, "price": Decimal(source[time]["ask"])})
    buy = task["side"] == "buy"
    price, limit = Decimal(source[task["submit"]]["ask" if buy else "bid"]), Decimal(task["limit"])
    eligible = price <= limit if buy else price >= limit
    if eligible and (task["mode"] == "IOC" or task["shares"] <= task["capacity"]):
        quantity = min(task["shares"], task["capacity"])
        result.append({"id": "TARGET", "time": task["submit"], "shares": quantity if buy else -quantity,
                       "price": price})
    return result


def audit(task: dict[str, object], native: dict[str, object], frame: pd.DataFrame) -> dict[str, object]:
    """Require same-observation execution/cancellation and independent cumulative fee/account conservation."""
    if (native["failures"] or native["real_orders_enabled"] or native["joint_tif"] != task["mode"]
            or native["integration"] != "adapted_immediate_controller"):
        raise ValueError("Immediate intent failed or was misrepresented as a native IOC/FOK class")
    targets = expected(task, frame)
    fills = [event for event in native["events"] if event["shares"]]
    if len(targets) != len(fills):
        raise ValueError("Immediate intent fill count differs from the actual observed capacity")
    cash, holding, notionals, charged = Decimal(100000), 0, {}, {}
    for observed, target in zip(fills, targets):
        if any((Decimal(str(observed[key])) if key == "price" else observed[key]) != value
               for key, value in target.items()):
            raise ValueError("Immediate execution was delayed or differs from the current quote/limit oracle")
        identity = target["id"]
        notional = notionals.get(identity, Decimal(0)) + abs(target["shares"]) * target["price"]
        total = (max(Decimal(18), notional * Decimal(".0005")) + notional * Decimal(".000127")
                 + (notional * Decimal(".001")).to_integral_value(rounding=ROUND_CEILING)).quantize(
                     Decimal(".01"), rounding=ROUND_HALF_UP)
        fee = total - charged.get(identity, Decimal(0))
        cash -= target["shares"] * target["price"] + fee
        holding += target["shares"]
        if (Decimal(str(observed["fee"])) != fee or Decimal(str(observed["cash"])) != cash
                or observed["holding"] != holding or cash < 0 or holding < 0):
            raise ValueError("Immediate native fees/cash/holdings differ from independent actual-fill accounting")
        notionals[identity], charged[identity] = notional, total
    target = next(order for order in native["orders"] if order["id"] == "TARGET")
    quantity = sum(abs(fill["shares"]) for fill in targets if fill["id"] == "TARGET")
    status = "Filled" if quantity == task["shares"] else "Canceled"
    if (target["status"] != status or abs(target["filled"]) != quantity
            or abs(target["quantity"]) != task["shares"]
            or target["native_tif"] != "GoodTilCanceledTimeInForce"):
        raise ValueError("Immediate parent/native ticket completion differs")
    mapping = ("joint_limit_guard_to_native_market" if quantity
               else "native_limit_immediately_canceled_without_execution")
    if native["mapping"] != mapping or target["type"] != ("Market" if quantity else "Limit"):
        raise ValueError("Adapted native ticket mapping is not explicitly represented")
    canceled = [event for event in native["events"] if event["id"] == "TARGET" and event["status"] == "Canceled"]
    if (status == "Canceled" and (len(canceled) != 1 or canceled[0]["time"] != task["submit"])):
        raise ValueError("Immediate intent left a delayed working remainder")
    if task["mode"] == "FOK" and quantity not in (0, task["shares"]):
        raise ValueError("Joint FOK cannot accept a partial fill")
    if Decimal(str(native["cash"])) != cash or native["holding"] != holding:
        raise ValueError("Immediate native final account differs")
    return {"passed": True, "joint_tif": task["mode"], "target_filled": quantity, "target_status": status,
            "cash": str(cash), "holding": holding, "integration": "adapted_immediate_controller"}


def run(response: Path, output: Path, repo: Path, *, days: list[str] | None = None) -> dict[str, object]:
    """Execute explicitly adapted immediate intents using actual current observations and native simulated accounts."""
    response, output, repo = response.resolve(), output.resolve(), repo.resolve()
    data = capture(response)
    selected = days or data.partitions["test"]
    if not selected or set(selected) - set(data.partitions["test"]):
        raise ValueError("Choose actual holdout days")
    output.mkdir(parents=True, exist_ok=False)
    runtime, bindings = build(repo, output)
    config_path = export(data, output / "inputs", repo / "tmp/lean-src", selected)
    (output / "inputs/commands.json").write_text("[]", encoding="utf-8")
    template = json.loads(config_path.read_bytes())
    bindings[str(response)] = file_hash(response)
    bindings.update({str(path): file_hash(path) for path in (output / "inputs").rglob("*") if path.is_file()})
    results = {}
    env = {**os.environ, **{key: "1" for key in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS")}}
    for day in selected:
        for case in CASES:
            name = day + "_" + case
            target = output / name
            target.mkdir()
            task = task_for(data.minutes.loc[day], case)
            write_report(target / "task.json", task)
            config = json.loads(json.dumps(template))
            config.update({"algorithm-type-name": "LocalHKImmediateAlgorithm",
                           "algorithm-location": str(runtime / "LocalHKImmediate.dll"),
                           "results-destination-folder": str(target / "results"),
                           "object-store-root": str(target / "storage")})
            config["parameters"].update({"capture-start": day, "capture-end": day, "task": str(target / "task.json"),
                                         "verification-output": str(target / "native.json")})
            write_report(target / "config.json", config)
            for path in (target / "config.json", target / "task.json"):
                bindings[str(path)] = file_hash(path)
            with (target / "engine.log").open("w", encoding="utf-8") as log:
                command = [str(repo / "tmp/dotnet/dotnet.exe"), str(runtime / "QuantConnect.Lean.Launcher.dll"),
                           "--config", str(target / "config.json")]
                subprocess.run(command,
                               cwd=repo, env=env, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=120)
            actual = json.loads((target / "native.json").read_bytes())
            results[name] = audit(task, actual, data.minutes.loc[day])
            progress(output / "progress.json", {"passed_cases": len(results), "last": name})
    verify_bindings(bindings)
    report = {"passed": True, "scenarios": len(results), "cases": results, "source_bindings": bindings,
              "lean_commit": LEAN_COMMIT, "integration": "adapted_immediate_controller",
              "native_ioc_fok_classes": False, "real_orders_enabled": False, "providers_modified": False,
              "limits": ["Immediate intent uses declared simulated 100-share capacity, not exchange queue evidence",
                         "Marketable intents map to guarded native market tickets; this is not a native TIF",
                         "FOK atomicity is single-instrument only; partial combo ratios remain unsupported"]}
    write_report(output / "report.json", report)
    return report


def main() -> None:
    """Run local native-account acceptance for the explicit joint IOC/FOK controller."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--response", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--day", action="append")
    args = parser.parse_args()
    result = run(args.response, args.output, Path.cwd(), days=args.day)
    print(json.dumps({"passed": result["passed"], "scenarios": result["scenarios"]}))


if __name__ == "__main__":
    main()
