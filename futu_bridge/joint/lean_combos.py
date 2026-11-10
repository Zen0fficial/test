"""Native complete-group LEAN equity combos from aligned canonical real daily observations.

One actual daily open per date becomes an explicitly assumed 09:30 zero-spread
quote, not a reconstructed intraday path. Native market/net-limit/leg-limit
group matching and native date-end expiry remain distinct. Partial group ratios
are explicitly rejected until a separately verified finite-liquidity model exists.
"""

from __future__ import annotations

import argparse
from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP
import json
import os
from pathlib import Path
import shutil
import subprocess
import zipfile

from .lean_contingent import build, verify_bindings
from .paper_lean import LEAN_COMMIT
from .session import write_report
from .storage.snapshots import SnapshotStore, file_hash

SYMBOLS = ("HK.00700", "HK.00388")
CASES = ("market_roundtrip", "limit_roundtrip", "leg_roundtrip", "limit_amend", "leg_amend", "limit_cancel",
         "limit_day", "leg_day", "limit_gtd", "leg_gtd")


def read_panel(panel: Path) -> tuple[dict[str, dict[str, Decimal]], dict[str, str]]:
    """Require the canonical Parquet panel to agree exactly with both original provider responses."""
    report_path = panel / "report.json"
    report = json.loads(report_path.read_bytes())
    if report.get("passed") is not True:
        raise ValueError("Real panel capture did not pass ingestion validation")
    table = SnapshotStore(panel / "store").read(report["snapshot"])
    rows = [row for row in table.to_pylist() if row["symbol"] in SYMBOLS]
    bindings = {str(report_path): file_hash(report_path)}
    raw_by_key = {}
    for symbol in SYMBOLS:
        meta = report["sources"][symbol]
        path = panel / meta["file"]
        if file_hash(path) != meta["sha256"]:
            raise ValueError("Panel raw capture hash changed")
        bindings[str(path)] = meta["sha256"]
        source = json.loads(path.read_bytes())["data"]
        if source["symbol"] != symbol or source["adjustment"] != "none" or source["timeframe"] != "1d":
            raise ValueError("Combo input requires exact unadjusted daily equity observations")
        for row in source["bars"]:
            raw_by_key[symbol, row["date"]] = row
    observations = {}
    for row in rows:
        key = row["symbol"], row["trading_date"]
        raw = raw_by_key[key]
        for field in ("open", "high", "low", "close", "volume"):
            if row[field] != Decimal(str(raw[field])):
                raise ValueError("Canonical panel differs from original provider values")
        if row["available_ns"] is not None:
            raise ValueError("Historical panel must not invent observed point-in-time availability")
        price = row["open"]
        if not 200 < price <= 500 or price % Decimal(".2"):
            continue
        observations.setdefault(row["trading_date"], {})[row["symbol"]] = price
    aligned = {day: values for day, values in sorted(observations.items()) if set(values) == set(SYMBOLS)}
    if len(aligned) < 12:
        raise ValueError("Need twelve aligned real sessions in the declared HK tick band")
    bindings.update({str(path): file_hash(path) for path in (panel / "store/parquet").rglob("*.parquet")})
    return aligned, bindings


def task_for(prices: dict[str, dict[str, Decimal]], days: list[str], case: str,
             completion: str = "all_legs_complete") -> dict[str, object]:
    """Specify complete-group matching and reject unsupported partial-ratio requests before native execution."""
    if case not in CASES or completion != "all_legs_complete":
        raise ValueError("Only declared complete-group native cases are supported; partial ratios are unsupported")
    if len(days) != 4 or len(set(days)) != 4 or days != sorted(days) or any(day not in prices for day in days):
        raise ValueError("A combo scenario requires four ordered aligned real dates")
    if any(set(prices[day]) != set(SYMBOLS) for day in days):
        raise ValueError("Missing actual leg observations")
    if sum(prices[days[0]].values()) * 100 >= 100000:
        raise ValueError("Declared whole group exceeds unleveraged starting cash")
    return {"case": case, "days": days, "buy": days[0] + " 09:30:00", "action": days[1] + " 09:30:00",
            "sell": days[2] + " 09:30:00", "expiry": days[0] + " 09:31:00",
            "group_completion": completion, "symbols": list(SYMBOLS), "quantity_per_leg": 100,
            "data_assumption": "one real daily open at assumed HK 09:30; zero spread; no invented path"}


def export_inputs(prices: dict[str, dict[str, Decimal]], days: list[str], directory: Path, repo: Path) -> None:
    """Export only actual daily opens and separate clock controls into native local tick files."""
    data = directory / "data"
    for name in ("market-hours", "symbol-properties"):
        shutil.copytree(repo / "tmp/lean-src/Data" / name, data / name)
    for market in ("hkpaper", "usa"):
        for name in ("map_files", "factor_files"):
            (data / "equity" / market / name).mkdir(parents=True, exist_ok=True)
    for symbol in SYMBOLS:
        ticker = symbol.split(".")[1]
        folder = data / "equity/hkpaper/tick" / ticker
        folder.mkdir(parents=True)
        for day in days:
            scaled = prices[day][symbol] * 10000
            for kind, values in (("quote", f"34200000,{scaled},100,{scaled},100\n"),
                                 ("trade", f"34200000,{scaled},1\n")):
                with zipfile.ZipFile(folder / (day.replace("-", "") + "_" + kind + ".zip"), "w",
                                     zipfile.ZIP_DEFLATED) as archive:
                    archive.writestr(day.replace("-", "") + "_" + ticker + "_tick_" + kind + ".csv", values)
    controls = [day + " " + instant for day in days for instant in ("09:31:00", "12:00:00", "16:00:00")]
    (data / "combo-clock.csv").write_text("\n".join(controls) + "\n", encoding="utf-8")
    (directory / "commands.json").write_text("[]", encoding="utf-8")


def configuration(task: dict[str, object], directory: Path, inputs: Path, runtime: Path) -> dict[str, object]:
    """Build an explicitly offline native launcher configuration for the separate extension assembly."""
    return {
        "environment": "local-paper-test", "live-mode": False,
        "algorithm-type-name": "LocalHKCombosAlgorithm", "algorithm-language": "CSharp",
        "algorithm-location": str(runtime / "LocalHKCombos.dll"), "data-folder": str(inputs / "data"),
        "results-destination-folder": str(directory / "results"), "object-store-root": str(directory / "storage"),
        "close-automatically": True, "log-handler": "QuantConnect.Logging.ConsoleLogHandler",
        "messaging-handler": "QuantConnect.Messaging.Messaging", "job-queue-handler": "QuantConnect.Queues.JobQueue",
        "api-handler": "QuantConnect.Api.Api",
        "map-file-provider": "QuantConnect.Data.Auxiliary.LocalDiskMapFileProvider",
        "factor-file-provider": "QuantConnect.Data.Auxiliary.LocalDiskFactorFileProvider",
        "data-provider": "QuantConnect.Lean.Engine.DataFeeds.DefaultDataProvider",
        "object-store": "QuantConnect.Lean.Engine.Storage.LocalObjectStore",
        "data-channel-provider": "DataChannelProvider",
        "setup-handler": "QuantConnect.Lean.Engine.Setup.BacktestingSetupHandler",
        "result-handler": "QuantConnect.Lean.Engine.Results.BacktestingResultHandler",
        "data-feed-handler": "QuantConnect.Lean.Engine.DataFeeds.FileSystemDataFeed",
        "real-time-handler": "QuantConnect.Lean.Engine.RealTime.BacktestingRealTimeHandler",
        "history-provider": ["QuantConnect.Lean.Engine.HistoricalData.SubscriptionDataReaderHistoryProvider"],
        "transaction-handler": "QuantConnect.Lean.Engine.TransactionHandlers.BacktestingTransactionHandler",
        "parameters": {"capture-start": task["days"][0], "capture-end": task["days"][-1], "capital": "100000",
                       "commands": str(inputs / "commands.json"), "task": str(directory / "task.json"),
                       "verification-output": str(directory / "native.json")}}


def expected(task: dict[str, object], prices: dict[str, dict[str, Decimal]]) -> list[dict[str, object]]:
    """Apply pinned complete-group candidate predicates to actual daily observations without native journals."""
    name, days = task["case"], task["days"]
    if name.endswith(("_cancel", "_day", "_gtd")):
        return []
    result = []
    held = False
    created, limits, net = None, {}, None
    direction = 1
    for index, day in enumerate(days):
        current = prices[day]
        if index == 0 or (index == 2 and held):
            direction = 1 if index == 0 else -1
            created = day
            resting = index == 0 and name.endswith("_amend")
            limits = {symbol: current[symbol] + direction * 20 for symbol in SYMBOLS}
            if resting:
                limits[SYMBOLS[0]] -= 120
            net = sum(current.values()) / 100 + direction - (3 if resting else 0)
        if index == 1 and name.endswith("_amend"):
            limits[SYMBOLS[0]] = current[SYMBOLS[0]] + 20
            net = sum(current.values()) / 100 + 1
        if created is None:
            continue
        if name.startswith("market_"):
            eligible = True
        elif name.startswith("leg_"):
            eligible = day > created and all((current[symbol] < limits[symbol] if direction > 0
                                             else current[symbol] > limits[symbol]) for symbol in SYMBOLS)
        else:
            eligible = day > created and (sum(current.values()) / 100 < net if direction > 0
                                          else sum(current.values()) / 100 > net)
        if eligible:
            for symbol in SYMBOLS:
                result.append({"id": ("BUY_" if direction > 0 else "SELL_") + symbol.split(".")[1],
                               "time": day + " 09:30:00", "shares": direction * 100, "price": current[symbol]})
            held = direction > 0
            created = None
    if held or len(result) != 4:
        raise ValueError("Real aligned input did not complete the prescribed funded round trip")
    return result


def audit(task: dict[str, object], native: dict[str, object],
          prices: dict[str, dict[str, Decimal]]) -> dict[str, object]:
    """Verify full native groups, actual prices, per-leg fees, exposure and date-end expiry independently."""
    if native["failures"] or native["real_orders_enabled"] or native["partial_ratio_supported"]:
        raise ValueError("Native combo run failed or violated declared offline complete-group scope")
    wanted = expected(task, prices)
    fills = [event for event in native["events"] if event["shares"] != 0]
    if len(fills) != len(wanted):
        raise ValueError(f"Native complete-group fill count differs: {fills} != {wanted}")
    cash, holdings = Decimal(100000), {symbol.split(".")[1]: 0 for symbol in SYMBOLS}
    for index, (actual, target) in enumerate(zip(fills, wanted)):
        if any((Decimal(str(actual[key])) if key == "price" else actual[key]) != value
               for key, value in target.items()):
            raise ValueError(f"Native combo leg differs from actual-data oracle: {actual} != {target}")
        notional = abs(target["shares"]) * target["price"]
        fee = (max(Decimal(18), notional * Decimal(".0005")) + notional * Decimal(".000127")
               + (notional * Decimal(".001")).to_integral_value(rounding=ROUND_CEILING)).quantize(
                   Decimal(".01"), rounding=ROUND_HALF_UP)
        if Decimal(str(actual["fee"])) != fee:
            raise ValueError("Native combo fee differs from independent per-leg HK tiered calculation")
        cash -= target["shares"] * target["price"] + fee
        holdings[target["id"].split("_")[1]] += target["shares"]
        if cash < 0 or any(value < 0 for value in holdings.values()):
            raise ValueError("Native combo oracle violated fully funded long-only exposure")
        if index % 2 == 1:
            if (actual["group"] != fills[index - 1]["group"] or actual["time"] != fills[index - 1]["time"]
                    or Decimal(str(actual["cash"])) != cash or actual["holdings"] != holdings):
                raise ValueError("Native complete-group atomicity or group-boundary accounts differ")
    if Decimal(str(native["cash"])) != cash or native["holdings"] != holdings:
        raise ValueError("Native combo final cash/positions differ")
    name = task["case"]
    expected_type = "ComboMarket" if name.startswith("market_") else (
        "ComboLegLimit" if name.startswith("leg_") else "ComboLimit")
    expected_tif = "DayTimeInForce" if name.endswith("_day") else (
        "GoodTilDateTimeInForce" if name.endswith("_gtd") else "GoodTilCanceledTimeInForce")
    status = "Filled" if wanted else "Canceled"
    if len(native["orders"]) != (4 if wanted else 2):
        raise ValueError("Native combo did not preserve both legs per submitted group")
    groups = {}
    for order in native["orders"]:
        groups.setdefault(order["group"], []).append(order["id"])
        if (order["type"] != expected_type or order["members"] != 2 or order["status"] != status
                or order["tif"] != expected_tif or abs(order["quantity"]) != 100
                or abs(order["filled"]) != (100 if wanted else 0)):
            raise ValueError("Native combo type, TIF, group membership or completion differs")
    if (len(groups) != len(native["orders"]) // 2
            or any(len(members) != 2 or len({member.split("_")[0] for member in members}) != 1
                   for members in groups.values())):
        raise ValueError("Native combo legs lost their distinct buy/exit group identities")
    if not wanted:
        deadline = task["action"] if name.endswith("_cancel") else task["days"][0] + " 16:00:00"
        cancels = [event for event in native["events"] if event["status"] == "Canceled"]
        if len(cancels) != 2 or any(event["time"] != deadline for event in cancels):
            raise ValueError("Native combo cancellation/expiry timing differs")
        if name.endswith(("_day", "_gtd")):
            midday = next(state for state in native["states"] if state["time"] == task["days"][0] + " 12:00:00")
            if any(order["status"] == "Canceled" for order in midday["orders"]):
                raise ValueError("Native equity GTD/Day must survive until its date's market close")
    return {"passed": True, "fills": len(fills), "native_groups": len(native["orders"]) // 2,
            "cash": str(cash), "holdings": holdings, "native_type": expected_type, "native_tif": expected_tif}


def run(panel: Path, output: Path, repo: Path, *, windows: int = 3) -> dict[str, object]:
    """Run all three native combo types and native expiry/amend/cancel cases on disjoint actual date windows."""
    panel, output, repo = panel.resolve(), output.resolve(), repo.resolve()
    if type(windows) is not int or windows not in (1, 2, 3):
        raise ValueError("Choose one to three actual four-session acceptance windows")
    prices, bindings = read_panel(panel)
    days = list(prices)[-12:]
    output.mkdir(parents=True, exist_ok=False)
    runtime, native_bindings = build(repo, output, converter="lean_combos.cs", assembly="LocalHKCombos")
    bindings.update(native_bindings)
    bindings[str(Path(__file__).resolve())] = file_hash(Path(__file__).resolve())
    inputs = output / "inputs"
    export_inputs(prices, days, inputs, repo)
    bindings.update({str(path): file_hash(path) for path in inputs.rglob("*") if path.is_file()})
    results = {}
    env = {**os.environ, **{name: "1" for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS")}}
    for number in range(windows):
        window = days[number * 4:(number + 1) * 4]
        for case in CASES:
            name = window[0] + "_" + case
            directory = output / name
            directory.mkdir()
            task = task_for(prices, window, case)
            write_report(directory / "task.json", task)
            config_path = directory / "config.json"
            write_report(config_path, configuration(task, directory, inputs, runtime))
            for path in (config_path, directory / "task.json"):
                bindings[str(path)] = file_hash(path)
            with (directory / "engine.log").open("w", encoding="utf-8") as log:
                subprocess.run([str(repo / "tmp/dotnet/dotnet.exe"),
                                str(runtime / "QuantConnect.Lean.Launcher.dll"), "--config", str(config_path)],
                               cwd=repo, env=env, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=120)
            native = json.loads((directory / "native.json").read_bytes())
            results[name] = audit(task, native, prices)
            write_report(output / "progress.json", {"passed_cases": len(results), "last": name})
    verify_bindings(bindings)
    report = {"passed": True, "scenarios": len(results), "cases": results, "symbols": list(SYMBOLS),
              "days": days[:windows * 4], "source_bindings": bindings, "lean_commit": LEAN_COMMIT,
              "real_orders_enabled": False, "shared_native_dlls_modified": False,
              "limitations": ["Native complete groups only; finite partial ratios explicitly unsupported",
                              "Actual daily opens placed at assumed 09:30 with zero spread; not reconstructed BBO",
                              "100-share simulation lots and immediate settlement; historical lot metadata unverified",
                              "Native net-limit equity ratio scale /100 and per-leg stale-data behavior retained",
                              "Daily historical PIT availability, option legs and auction data remain unavailable"]}
    write_report(output / "report.json", report)
    return report


def main() -> None:
    """Expose isolated offline native combo acceptance from the existing canonical daily panel."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--panel", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--windows", type=int, default=3)
    args = parser.parse_args()
    result = run(args.panel, args.output, Path.cwd(), windows=args.windows)
    print(json.dumps({"passed": result["passed"], "scenarios": result["scenarios"]}))


if __name__ == "__main__":
    main()
