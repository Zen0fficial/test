"""Native LEAN OCO/OUO/complete-parent OTO integration with independent real-data ledgers.

Build a separate extension assembly in a private copy of the pinned launcher
runtime. Shared native DLLs, providers and legacy simulations remain untouched.
The native engine owns contingency links, activation, cancellation and resizing;
the converter supplies only explicit finite liquidity and existing HK fees.
"""

from __future__ import annotations

import argparse
from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP
import json
import os
from pathlib import Path
import shutil
import subprocess
from xml.sax.saxutils import escape

import pandas as pd

from .lifecycle import quotes
from .paper_common import capture
from .paper_lean import LEAN_COMMIT, export
from .session import write_report
from .storage.snapshots import file_hash

CASES = ("oco_partial", "ouo_partial", "oco_cancel", "oto_complete", "oto_parent_cancel",
         "oto_held_amend", "oto_child_cancel", "oto_active_amend",
         "reject_single_oco", "reject_duplicate_ouo", "reject_combo_bracket")


def task_for(frame: pd.DataFrame, case: str) -> dict[str, object]:
    """Create prescribed group actions whose prices depend only on their actual submission observations."""
    if case not in CASES:
        raise ValueError("Unknown native contingent case")
    day = str(frame.index[0].date())
    submit = day + (" 10:00:00" if case.startswith(("oco_", "ouo_")) else " 09:30:00")
    points = quotes(frame)
    initial = next(point for point in points if point["time"] == submit)
    actions = []
    if case in ("oco_partial", "ouo_partial"):
        actions.append({"id": "TP", "kind": "amend", "time": day + " 10:01:00"})
    elif case == "oco_cancel":
        actions.append({"id": "TP", "kind": "cancel", "time": day + " 10:01:00"})
    elif case == "oto_held_amend":
        actions.extend([{"id": "TP", "kind": "amend", "time": day + " 09:31:00"},
                        {"id": "ENTRY", "kind": "amend", "time": day + " 09:33:00"}])
    elif case == "oto_child_cancel":
        actions.extend([{"id": "TP", "kind": "cancel", "time": day + " 09:31:00"},
                        {"id": "ENTRY", "kind": "cancel", "time": day + " 09:32:00"}])
    elif case == "oto_active_amend":
        actions.append({"id": "TP", "kind": "amend", "time": day + " 10:00:00"})
    return {"case": case, "day": day, "start": day + " 09:30:00", "submit": submit,
            "cleanup": day + " 15:29:00", "initial": initial, "actions": actions,
            "parent_shares": 200, "liquidity_per_side": 100, "oto_policy": "complete_parent_only"}


def build(repo: Path, output: Path, *, converter: str = "lean_contingent.cs",
          assembly: str = "LocalHKContingent") -> tuple[Path, dict[str, str]]:
    """Compile a converter-only extension against copied native DLLs, never rebuilding shared projects."""
    source = repo / "tmp/lean-src"
    revision = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    if revision != LEAN_COMMIT:
        raise ValueError("Contingent acceptance requires the pinned LEAN revision")
    runtime = output / "runtime"
    shutil.copytree(source / "Launcher/bin/Release", runtime)
    project = output / "extension"
    project.mkdir()
    if (converter, assembly) not in (("lean_contingent.cs", "LocalHKContingent"),
                                     ("lean_combos.cs", "LocalHKCombos")):
        raise ValueError("Only declared offline converter assemblies may be compiled")
    originals = [repo / "futu_bridge/converters" / converter, repo / "futu_bridge/converters/lean_paper.cs"]
    for path in originals:
        shutil.copyfile(path, project / path.name)
    references = ["QuantConnect.Common", "QuantConnect.Algorithm", "QuantConnect.Indicators",
                  "QuantConnect.Logging", "QuantConnect.Configuration", "QuantConnect.Brokerages",
                  "QuantConnect.Algorithm.Framework", "Newtonsoft.Json", "NodaTime", "Python.Runtime"]
    xml = '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><TargetFramework>net10.0</TargetFramework>'
    xml += '<AssemblyName>' + assembly + '</AssemblyName>'
    xml += '<EnableDefaultCompileItems>false</EnableDefaultCompileItems>'
    xml += '<AppendTargetFrameworkToOutputPath>false</AppendTargetFrameworkToOutputPath></PropertyGroup><ItemGroup>'
    for path in originals:
        xml += '<Compile Include="' + escape(path.name) + '" />'
    for name in references:
        xml += '<Reference Include="' + name + '"><HintPath>' + escape(str(runtime / (name + ".dll")))
        xml += '</HintPath><Private>false</Private></Reference>'
    xml += '</ItemGroup></Project>'
    project_file = project / (assembly + ".csproj")
    project_file.write_text(xml, encoding="utf-8")
    bindings = {str(path): file_hash(path) for path in [*originals, Path(__file__).resolve(),
                                                     repo / "requirements/joint-py312-windows.lock"]}
    bindings.update({str(path): file_hash(path) for path in runtime.glob("*.dll")})
    with (output / "isolated-build.log").open("w", encoding="utf-8") as log:
        subprocess.run([str(repo / "tmp/dotnet/dotnet.exe"), "build", str(project_file), "-c", "Release",
                        "-o", str(runtime), "--nologo", "-v", "quiet", "--ignore-failed-sources"],
                       cwd=repo, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=180)
    verify_bindings(bindings)
    bindings[str(runtime / (assembly + ".dll"))] = file_hash(runtime / (assembly + ".dll"))
    return runtime, bindings


def verify_bindings(bindings: dict[str, str]) -> None:
    """Reject source/runtime/input drift before accepting native simulation evidence."""
    for filename, digest in bindings.items():
        if file_hash(Path(filename)) != digest:
            raise ValueError("Native contingent source/runtime/input binding changed: " + filename)


def oracle(task: dict[str, object], frame: pd.DataFrame) -> dict[str, object]:
    """Enumerate native complete-parent and sibling transitions without reading native execution journals."""
    name = task["case"]
    if name.startswith("reject_"):
        return {"fills": [], "orders": {}, "cancel_times": {}, "activation": None}
    orders, fills, cancel_times = {}, [], {}
    initial = {side: Decimal(task["initial"][side]) for side in ("bid", "ask")}
    activation = None

    def cancel(identity: str, time: str) -> None:
        """Cancel open siblings or held descendants according to the pinned native contingency rules."""
        if orders[identity]["status"] in ("Filled", "Canceled"):
            return
        orders[identity]["status"] = "Canceled"
        cancel_times[identity] = time
        peers = ("TP", "SL") if identity in ("TP", "SL", "ENTRY") else ()
        for peer in peers:
            if peer != identity and peer in orders and orders[peer]["status"] not in ("Filled", "Canceled"):
                orders[peer]["status"] = "Canceled"
                cancel_times[peer] = time

    def match(time: str, price: dict[str, Decimal], used: dict[str, int]) -> None:
        """Match eligible native candidate predicates with finite per-side capacity and native group consequences."""
        nonlocal activation
        for identity, order in orders.items():
            side = order["side"]
            if order["status"] in ("Filled", "Canceled") or order["held"] or used[side] >= 100:
                continue
            if identity != "SEED" and (time <= order["created"] or time <= (order["triggered"] or "")):
                continue
            value = price["ask" if side == "buy" else "bid"]
            eligible = (identity == "SEED" or (value > order["price"] if side == "sell" else value < order["price"]))
            if identity == "SL":
                eligible = value <= order["price"]
            if not eligible:
                continue
            size = min(order["quantity"] - order["filled"], 100 - used[side])
            order["filled"] += size
            used[side] += size
            order["status"] = "Filled" if order["filled"] == order["quantity"] else "PartiallyFilled"
            fills.append({"id": identity, "time": time, "shares": size if side == "buy" else -size, "price": value})
            if identity == "ENTRY":
                if name == "oto_parent_cancel":
                    cancel(identity, time)
                elif order["status"] == "Filled":
                    activation = time
                    for peer in ("TP", "SL"):
                        if orders[peer]["status"] != "Canceled":
                            orders[peer].update(held=False, triggered=time)
            elif identity in ("TP", "SL"):
                peer = "SL" if identity == "TP" else "TP"
                if name.startswith("oco_") or order["status"] == "Filled":
                    # Native backtesting cancellation propagation cancels an OCO
                    # partial winner's remainder too; preserve this pinned behavior.
                    cancel(peer, time)
                else:
                    orders[peer]["quantity"] -= size

    for point in quotes(frame):
        time = point["time"]
        price = {side: Decimal(point[side]) for side in ("bid", "ask")}
        used = {"buy": 0, "sell": 0}
        match(time, price, used)
        if time == task["start"] and name.startswith(("oco_", "ouo_")):
            orders["SEED"] = {"side": "buy", "price": None, "quantity": 200, "filled": 0, "held": False,
                              "triggered": None, "created": time, "status": "Submitted"}
        if time == task["submit"]:
            oto = name.startswith("oto_")
            if oto:
                orders["ENTRY"] = {"side": "buy", "price": initial["bid"] - 20
                                   if name in ("oto_held_amend", "oto_child_cancel") else initial["ask"] + 10,
                                   "quantity": 200, "filled": 0, "held": False,
                                   "triggered": None, "created": time, "status": "Submitted"}
            for identity in ("TP", "SL"):
                limit = initial["bid"] + (20 if identity == "TP" else -20)
                if identity == "TP" and name == "oto_complete":
                    limit = initial["bid"] - 10
                orders[identity] = {"side": "sell", "price": limit, "quantity": 200, "filled": 0,
                                    "held": oto, "triggered": None, "created": time, "status": "Submitted"}
        for action in task["actions"]:
            if action["time"] != time:
                continue
            identity = action["id"]
            if action["kind"] == "cancel":
                cancel(identity, time)
            else:
                orders[identity]["price"] = price["ask"] + 10 if identity == "ENTRY" else price["bid"] - 10
        if time == task["cleanup"]:
            for identity in orders:
                cancel(identity, time)
        match(time, price, used)
    return {"fills": fills, "orders": orders, "cancel_times": cancel_times, "activation": activation}


def audit(task: dict[str, object], actual: dict[str, object], frame: pd.DataFrame) -> dict[str, object]:
    """Verify exact real-price fills, native links/timing, partial resizing and independent cash/fees."""
    if actual["failures"] or actual["real_orders_enabled"] is not False:
        raise ValueError("Native contingent simulation failed or enabled real routing")
    expected = oracle(task, frame)
    fills = [event for event in actual["events"] if event["type"] == "fill"]
    if len(fills) != len(expected["fills"]):
        raise ValueError(f"Native contingent fill count differs: {fills} != {expected['fills']}")
    cash, holding, notionals, fees = Decimal(100000), 0, {}, {}
    for observed, wanted in zip(fills, expected["fills"]):
        for key in ("id", "time", "shares", "price"):
            left = Decimal(str(observed[key])) if key == "price" else observed[key]
            if left != wanted[key]:
                raise ValueError(f"Native contingent fill differs: {observed} != {wanted}")
        identity, size = wanted["id"], abs(wanted["shares"])
        notional = notionals.get(identity, Decimal(0)) + wanted["price"] * size
        total = (max(Decimal(18), notional * Decimal(".0005")) + notional * Decimal(".000127")
                 + (notional * Decimal(".001")).to_integral_value(rounding=ROUND_CEILING)).quantize(
                     Decimal(".01"), rounding=ROUND_HALF_UP)
        fee = total - fees.get(identity, Decimal(0))
        if Decimal(str(observed["fee"])) != fee:
            raise ValueError("Contingent incremental native fee differs from independent Decimal oracle")
        cash -= wanted["shares"] * wanted["price"] + fee
        holding += wanted["shares"]
        if (Decimal(str(observed["cash"])) != cash or observed["holding"] != holding or cash < 0 or holding < 0):
            raise ValueError("Contingent native fill-time account differs from independent cash/share ledger")
        notionals[identity], fees[identity] = notional, total
    native_orders = {order["id"]: order for order in actual["orders"]}
    if native_orders.keys() != expected["orders"].keys():
        raise ValueError("Native contingent membership differs")
    for identity, wanted in expected["orders"].items():
        observed = native_orders[identity]
        sign = 1 if wanted["side"] == "buy" else -1
        if (observed["status"] != wanted["status"] or observed["filled"] != sign * wanted["filled"]
                or observed["quantity"] != sign * wanted["quantity"]):
            raise ValueError(f"Native contingent final quantity/status differs: {observed} != {wanted}")
        if identity in expected["cancel_times"]:
            cancels = [event["time"] for event in actual["events"]
                       if event["id"] == identity and event.get("status") == "Canceled"]
            if cancels != [expected["cancel_times"][identity]]:
                raise ValueError("Native contingent cancellation timing differs")
        if identity in ("TP", "SL") and observed["triggered"] != wanted["triggered"]:
            raise ValueError("Native OTO release must wait for complete parent fill")
    if Decimal(str(actual["cash"])) != cash or actual["holding"] != holding:
        raise ValueError("Native contingent final account differs")
    for checkpoint in actual["states"]:
        for order in checkpoint["orders"]:
            if (order["id"] in ("TP", "SL") and not order["held"]
                    and order["status"] not in ("Filled", "Canceled")):
                remaining = abs(order["quantity"]) - abs(order["filled"])
                if remaining > checkpoint["holding"]:
                    raise ValueError("Native active contingent child exceeds funded remaining inventory")
            if (task["case"].startswith("oto_") and order["id"] in ("TP", "SL")
                    and order["status"] not in ("Filled", "Canceled")
                    and (expected["activation"] is None or checkpoint["time"] < expected["activation"])):
                if not order["held"] or order["filled"] != 0:
                    raise ValueError("Native OTO child became active before full parent completion")
    if task["case"].startswith("reject_"):
        reasons = {"reject_single_oco": "at least two orders", "reject_duplicate_ouo": "more than once",
                   "reject_combo_bracket": "not supported for combo"}
        if len(actual["rejections"]) != 1 or reasons[task["case"]] not in actual["rejections"][0]["message"]:
            raise ValueError("Unsupported native group lacks the expected explicit upstream rejection")
    elif actual["rejections"]:
        raise ValueError("Supported native group unexpectedly rejected")
    else:
        grouped = [order for order in native_orders.values() if order["id"] != "SEED"]
        if len({order["set"] for order in grouped}) != 1 or any(order["count"] != len(grouped) for order in grouped):
            raise ValueError("Native contingent set identity/count differs")
        for order in grouped:
            links = order["links"]
            if task["case"].startswith("oto_"):
                role = "Parent" if order["id"] == "ENTRY" else "Child"
                if not any(link["type"] == "OneTriggersOther" and link["role"] == role for link in links):
                    raise ValueError("Native OTO link/role missing")
            if order["id"] != "ENTRY":
                relation = "OneCancelsOther" if task["case"].startswith("oco_") else "OneUpdatesOther"
                if not any(link["type"] == relation and link["role"] is None for link in links):
                    raise ValueError("Native sibling relation missing")
    return {"passed": True, "fills": len(fills), "cash": str(cash), "holding": holding,
            "unprotected_remaining_holding": holding, "activation": expected["activation"],
            "native_rejections": actual["rejections"]}


def run(response: Path, output: Path, repo: Path, *, days: list[str] | None = None) -> dict[str, object]:
    """Run native grouping acceptance from immutable real inputs in a fully isolated native runtime copy."""
    response, output, repo = response.resolve(), output.resolve(), repo.resolve()
    data = capture(response)
    selected = days or data.partitions["test"]
    if not selected or set(selected) - set(data.partitions["test"]):
        raise ValueError("Native contingent acceptance days must belong to the captured holdout")
    output.mkdir(parents=True, exist_ok=False)
    runtime, bindings = build(repo, output)
    config_path = export(data, output / "inputs", repo / "tmp/lean-src", selected)
    (output / "inputs/commands.json").write_text("[]", encoding="utf-8")
    template = json.loads(config_path.read_bytes())
    bindings[str(response)] = file_hash(response)
    bindings.update({str(path): file_hash(path) for path in (output / "inputs").rglob("*") if path.is_file()})
    results = {}
    env = {**os.environ, **{name: "1" for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS")}}
    for day in selected:
        for case in CASES:
            name = day + "_" + case
            directory = output / name
            directory.mkdir()
            task = task_for(data.minutes.loc[day], case)
            task_path = directory / "task.json"
            write_report(task_path, task)
            config = json.loads(json.dumps(template))
            config.update({"algorithm-type-name": "LocalHKContingentAlgorithm",
                           "algorithm-location": str(runtime / "LocalHKContingent.dll"),
                           "results-destination-folder": str(directory / "results"),
                           "object-store-root": str(directory / "storage")})
            config["parameters"].update({"capture-start": day, "capture-end": day, "task": str(task_path),
                                         "verification-output": str(directory / "native.json")})
            native_config = directory / "config.json"
            write_report(native_config, config)
            bindings[str(task_path)] = file_hash(task_path)
            bindings[str(native_config)] = file_hash(native_config)
            with (directory / "engine.log").open("w", encoding="utf-8") as log:
                subprocess.run([str(repo / "tmp/dotnet/dotnet.exe"),
                                str(runtime / "QuantConnect.Lean.Launcher.dll"), "--config", str(native_config)],
                               cwd=repo, env=env, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=120)
            actual = json.loads((directory / "native.json").read_bytes())
            results[name] = audit(task, actual, data.minutes.loc[day])
            write_report(output / "progress.json", {"passed_cases": len(results), "last": name})
    verify_bindings(bindings)
    report = {"passed": True, "scenarios": len(results), "days": selected, "cases": results,
              "source_bindings": bindings, "lean_commit": LEAN_COMMIT, "shared_native_dlls_modified": False,
              "real_orders_enabled": False, "providers_modified": False,
              "limits": ["OTO releases only after a complete parent fill; partial-parent activation is not native",
                         "Pinned native OCO partial-fill cancellation propagates back to cancel the winner remainder",
                         "Finite liquidity is 100 shares per side/observation; BBO derives from actual minute opens",
                         "No multi-leg matching, option exercise, auction or live brokerage behavior is certified",
                         "Canceled groups may leave explicit unprotected long inventory"]}
    write_report(output / "report.json", report)
    return report


def main() -> None:
    """Expose the bounded native contingent acceptance runner without a live execution mode."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--response", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--day", action="append")
    args = parser.parse_args()
    result = run(args.response, args.output, Path.cwd(), days=args.day)
    print(json.dumps({"passed": result["passed"], "scenarios": result["scenarios"]}))


if __name__ == "__main__":
    main()
