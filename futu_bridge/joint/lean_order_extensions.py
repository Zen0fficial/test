"""Offline native LEAN trailing-stop and touched-limit integration with independent real-price oracles."""

from __future__ import annotations

import argparse
from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP
import json
from pathlib import Path
import shutil
import subprocess

import pandas as pd

from .nautilus_features_contingent import source_quotes
from .nautilus_order_matrix import task_for
from .paper_common import capture
from .paper_lean import build as build_paper, export
from .qlib_rl_data import ExecutionData
from .session import write_report

CASES = ("limit_if_touched_buy", "limit_if_touched_sell", "trailing_stop_buy", "trailing_stop_sell",
         "latched_limit_buy", "latched_limit_sell", "partial_limit_buy", "partial_limit_sell",
         "trailing_percent_buy", "trailing_percent_sell", "trailing_partial_buy", "trailing_partial_sell")


def make_task(frame: pd.DataFrame, case: str) -> dict[str, object]:
    """Build native conditional requests using only quotes known at submission."""
    template = ("trailing_stop_limit_" if case.startswith("trailing") else "limit_if_touched_")
    template += "sell" if case.endswith("sell") else "buy"
    task = task_for(frame, template)
    task["case"] = case
    if case.startswith("latched"):
        task["price"] = str(Decimal(task["trigger"]) + (Decimal(1) if case.endswith("sell") else Decimal(-1)))
    if case.startswith("partial") or "trailing_partial" in case:
        task["shares"] = 200
    return task


def execute(data: ExecutionData, task: dict[str, object], output: Path,
            source: Path, dotnet: Path) -> dict[str, object]:
    """Run native LEAN offline without changing the legacy lifecycle algorithm or its scenarios."""
    day = str(task["submit"])[:10]
    config_path = export(data, output, source, [day])
    (output / "commands.json").write_text("[]", encoding="utf-8")
    task_path = output / "task.json"
    write_report(task_path, task)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["algorithm-type-name"] = "LocalHKOrderExtensionsAlgorithm"
    config["parameters"]["task"] = str(task_path)
    write_report(config_path, config)
    with (output / "engine.log").open("w", encoding="utf-8") as log:
        subprocess.run([str(dotnet), str(source / "Launcher/bin/Release/QuantConnect.Lean.Launcher.dll"),
                        "--config", str(config_path)], stdout=log, stderr=subprocess.STDOUT, check=True, timeout=120)
    return json.loads((output / "native.json").read_text(encoding="utf-8"))


def expected(task: dict[str, object], frame: pd.DataFrame) -> tuple[list[dict[str, object]], bool, Decimal]:
    """Enumerate native conditional behavior from source quotes and the explicit finite-liquidity policy."""
    source = source_quotes(frame)
    buy = task["side"] == "buy"
    trigger, limit = Decimal(task["trigger"]), Decimal(task["price"])
    fills, active = [], False
    if task["seed"]:
        seed_times = [t for t in source if task["seed"] <= t < task["submit"]][:task["shares"] // 100]
        fills.extend({"id": "SEED", "time": t, "price": source[t]["ask"], "shares": 100} for t in seed_times)
    count = 0
    for time, quote in source.items():
        if not task["submit"] < time <= task["cancel"]:
            continue
        value = quote["ask" if buy else "bid"]
        if task["case"].startswith("trailing"):
            crossed = value >= trigger if buy else value <= trigger
            active |= crossed
            if active:
                fills.append({"id": "TARGET", "time": time, "price": value, "shares": 100})
                count += 100
                if count == task["shares"]:
                    break
            else:
                offset = value * Decimal(".001") if "percent" in task["case"] else Decimal(".4")
                trigger = min(trigger, value + offset) if buy else max(trigger, value - offset)
        else:
            active |= value <= trigger if buy else value >= trigger
            if active and (value <= limit if buy else value >= limit):
                fills.append({"id": "TARGET", "time": time, "price": limit, "shares": 100})
                count += 100
                if count == task["shares"]:
                    break
    return fills, active, trigger


def audit(task: dict[str, object], actual: dict[str, object], frame: pd.DataFrame) -> dict[str, object]:
    """Require every fill, native latch, fee and final account to match an independent source-price ledger."""
    if actual["failures"] or actual["real_orders_enabled"] is not False:
        raise ValueError("Native LEAN conditional simulation failed or enabled real routing")
    fills, touched, final_stop = expected(task, frame)
    native = [x for x in actual["events"] if x["type"] == "fill"]
    if len(native) != len(fills):
        raise ValueError(f"Native conditional fill count differs: {native} != {fills}")
    cash, holding, notionals, fees = Decimal(100000), 0, {}, {}
    for observed, wanted in zip(native, fills):
        for key, value in wanted.items():
            if (Decimal(str(observed[key])) != value if key == "price" else observed[key] != value):
                raise ValueError(f"Native conditional fill differs from source oracle: {observed} != {wanted}")
        key = wanted["id"]
        value = notionals.get(key, Decimal(0)) + wanted["price"] * wanted["shares"]
        total = (max(Decimal(18), value * Decimal(".0005")) + value * Decimal(".000127")
                 + (value * Decimal(".001")).to_integral_value(rounding=ROUND_CEILING)).quantize(
                     Decimal(".01"), rounding=ROUND_HALF_UP)
        incremental = total - fees.get(key, Decimal(0))
        if Decimal(str(observed["fee"])) != incremental:
            raise ValueError("Native conditional fee differs from independent cumulative cost accounting")
        sign = 1 if key == "SEED" or task["side"] == "buy" else -1
        cash -= sign * wanted["price"] * wanted["shares"] + incremental
        holding += sign * wanted["shares"]
        notionals[key], fees[key] = value, total
        if cash < 0 or holding < 0:
            raise ValueError("Conditional simulation violated the no-leverage/long-only account")
    filled = sum(x["shares"] for x in fills if x["id"] == "TARGET")
    status = "FILLED" if filled == task["shares"] else "CANCELED"
    if (actual["orders"]["TARGET"]["status"] != status or actual["orders"]["TARGET"]["filled"] != filled
            or Decimal(str(actual["cash"])) != cash or actual["holding"] != holding):
        raise ValueError("Native conditional final state differs from independently expected executions")
    if not task["case"].startswith("trailing") and actual["orders"]["TARGET"]["touched"] != touched:
        raise ValueError("Native touched-limit latch differs from the causal source-price trigger")
    if task["case"].startswith("trailing") and Decimal(str(actual["orders"]["TARGET"]["stop"])) != final_stop:
        raise ValueError("Native trailing stop price differs from the source-derived offset calculation")
    return {"passed": True, "fills": len(fills), "target_filled": filled, "cash": str(cash), "holding": holding}


def run(response: Path, output: Path, repo: Path) -> dict[str, object]:
    """Compile converter-owned native order additions and validate all five genuine holdout sessions."""
    response, output, repo = response.resolve(), output.resolve(), repo.resolve()
    data = capture(response)
    output.mkdir(parents=True, exist_ok=False)
    source, dotnet = repo / "tmp/lean-src", repo / "tmp/dotnet/dotnet.exe"
    shutil.copyfile(repo / "futu_bridge/converters/lean_order_extensions.cs",
                    source / "Algorithm.CSharp/LocalHKOrderExtensionsAlgorithm.cs")
    build_paper(repo, source, dotnet, output)
    results = {}
    for day in data.partitions["test"]:
        for case in CASES:
            task = make_task(data.minutes.loc[day], case)
            key = day + "_" + case
            actual = execute(data, task, output / key, source, dotnet)
            results[key] = audit(task, actual, data.minutes.loc[day])
            write_report(output / "progress.json", {"completed": len(results), "last": key})
    report = {"passed": True, "cases": results, "scenarios": len(results), "data": data.provenance,
        "real_orders_enabled": False, "providers_modified": False,
        "limitations": ["Quote-derived triggers and 100-share per-side assumed capacity",
                        "Native touched limit candidates are guarded by the actual executable side quote",
                        "Native trailing first trigger plus joint market-remainder latch for partial fills",
                        "No auction, option exercise, multileg or broker-specific execution is certified here"]}
    write_report(output / "report.json", report)
    return report


def main() -> None:
    """Expose only real-capture offline acceptance, with no brokerage routing option."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--response", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    args = parser.parse_args()
    print(json.dumps(run(args.response, args.output, args.repo), indent=2))


if __name__ == "__main__":
    main()
