"""Convert fixed provider captures to local LEAN equity files and custom data."""

import argparse
from collections import defaultdict
import datetime as dt
from pathlib import Path
import shutil
import zipfile
from zoneinfo import ZoneInfo

from .capture import load_capture, read_json, sha256, write_json
from .validation import FIELDS, bar_end, bars, compare_rows, expected_rows, number, probabilities, ticker


def export(capture_dir, output, lean_source):
    manifest, responses = load_capture(capture_dir)
    equity_inputs, probability_inputs = list(bars(manifest, responses)), list(probabilities(manifest, responses))
    # LEAN clamps a backtest's last date to yesterday. A final history bucket
    # can be delivered at midnight the next day; do not silently drop it.
    if dt.date.fromisoformat(manifest["end"]) + dt.timedelta(days=1) >= dt.datetime.now(dt.timezone.utc).date():
        raise ValueError("LEAN needs the day after capture end to be in the past, to replay the final midnight bucket")
    root = Path(output).resolve()
    root.mkdir(parents=True, exist_ok=False)
    data_dir = root / "data"
    for name in ("map_files", "factor_files"):
        (data_dir / "equity" / "usa" / name).mkdir(parents=True)
    source = Path(lean_source).resolve()
    # Engine reference calendars/symbol properties are infrastructure, not a
    # replacement market-data provider. No QuantConnect data download is used.
    reference_hashes = {}
    for directory in ("market-hours", "symbol-properties"):
        shutil.copytree(source / "Data" / directory, data_dir / directory)
        for file in (data_dir / directory).rglob("*"):
            if file.is_file():
                reference_hashes[str(file.relative_to(data_dir))] = sha256(file)
    ny = ZoneInfo("America/New_York")
    for symbol, response in equity_inputs:
        code = ticker(symbol).lower()
        grouped = defaultdict(list)
        for row in response["data"]["bars"]:
            # Futu label -> LEAN bar START in the exchange timezone (DST-aware).
            start = (bar_end(row, manifest["bar_timestamp"]) - dt.timedelta(minutes=1)).astimezone(ny)
            if start.second or start.microsecond:
                raise ValueError("LEAN minute bars require whole-minute boundaries")
            ms = (start.hour * 3600 + start.minute * 60) * 1000
            # LEAN's decimal parser accepts fractional scaled values. Preserve
            # Futu sub-0.0001 prices instead of rounding them to integer ticks.
            values = [str(ms)] + [format(number(row[key]) * 10000, "f") for key in FIELDS[:4]]
            values.append(format(number(row["volume"]), "f"))
            grouped[start.strftime("%Y%m%d")].append(",".join(values))
        folder = data_dir / "equity" / "usa" / "minute" / code
        folder.mkdir(parents=True)
        for day, lines in grouped.items():
            name = f"{day}_{code}_minute_trade.csv"
            with zipfile.ZipFile(folder / f"{day}_trade.zip", "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr(name, "\n".join(lines) + "\n")
    custom = data_dir / "custom" / "polymarket"
    custom.mkdir(parents=True)
    for index, (outcome, rows) in enumerate(probability_inputs):
        grouped = defaultdict(list)
        for row, available in rows:
            # Files partition by delivery date so midnight bucket ends are not lost.
            grouped[available.strftime("%Y%m%d")].append(",".join([
                str(int(number(row["timestamp"]))), str(int(number(row["resolution_seconds"]))),
                format(number(row["price"]), "f"), outcome["token_id"]]))
        for day, lines in grouped.items():
            (custom / f"pm{index}_{day}.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")

    config = {
        "environment": "local-converter-test", "live-mode": False,
        "algorithm-type-name": "LocalProviderCompatibilityAlgorithm", "algorithm-language": "CSharp",
        "algorithm-location": str(source / "Launcher/bin/Release/QuantConnect.Algorithm.CSharp.dll"),
        "data-folder": str(data_dir), "results-destination-folder": str(root / "results"),
        "object-store-root": str(root / "storage"), "close-automatically": True,
        "log-handler": "QuantConnect.Logging.ConsoleLogHandler", "messaging-handler": "QuantConnect.Messaging.Messaging",
        "job-queue-handler": "QuantConnect.Queues.JobQueue", "api-handler": "QuantConnect.Api.Api",
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
        "parameters": {"capture-start": manifest["start"], "capture-end": manifest["end"],
                       "tickers": ",".join(ticker(s) for s in manifest["symbols"]),
                       "outcome-count": str(len(probability_inputs)), "verification-output": str(root / "engine_rows.json")},
    }
    write_json(root / "config.json", config)
    write_json(root / "conversion.json", {"platform": "LEAN", "capture_sha256": sha256(Path(capture_dir) / "manifest.json"),
               "reference_files": reference_hashes, "normalization": "Raw; corporate-action factors are not fabricated",
               "bar_timestamp": "start in America/New_York; delivery at end", "probability_delivery": "bucket end in UTC",
               "outcomes": manifest["outcomes"], "provider_files_unchanged": True})
    return root


def verify(capture_dir, output):
    manifest, responses = load_capture(capture_dir)
    result = read_json(Path(output) / "engine_rows.json")
    report = compare_rows(result["rows"], expected_rows(manifest, responses))
    if result.get("submitted_orders") != 0:
        raise ValueError("The data-only observer must report zero submitted orders")
    report.update(platform="LEAN", submitted_orders=result["submitted_orders"], live_orders=0,
                  capture_sha256=sha256(Path(capture_dir) / "manifest.json"),
                  purpose="Data compatibility only; trading is manual")
    write_json(Path(output) / "verification.json", report)
    return report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=("export", "verify"))
    p.add_argument("--capture", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--lean-source")
    args = p.parse_args()
    if args.command == "export":
        if not args.lean_source:
            p.error("export requires --lean-source")
        print(export(args.capture, args.output, args.lean_source))
    else:
        print(verify(args.capture, args.output))


if __name__ == "__main__":
    main()
