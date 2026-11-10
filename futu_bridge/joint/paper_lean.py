"""Convert captured HK opens to LEAN's native equity quote-tick files offline.

The custom market is deliberately named hkpaper: it supports this instrument,
capture window and continuous sessions, not a complete HK exchange calendar.
No downloaded market data, QuantConnect account or brokerage connection is used.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import json
import shutil
import subprocess
import zipfile

from .paper_common import CAPITAL, execution_price, schedule
from .qlib_rl_data import ExecutionData

LEAN_COMMIT = "49b4aa7022abd38e99e9c7d3ec67929fa7f6d7b8"


def build(repo: Path, source: Path, dotnet: Path, output: Path) -> None:
    """Compile only a converter-owned algorithm in the pinned separate checkout."""
    revision = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    if revision != LEAN_COMMIT:
        raise ValueError("LEAN checkout differs from the verified shared-runtime revision")
    shutil.copyfile(repo / "futu_bridge/converters/lean_paper.cs",
                    source / "Algorithm.CSharp/LocalHKPaperAlgorithm.cs")
    with (output / "lean-build.log").open("w", encoding="utf-8") as log:
        subprocess.run([str(dotnet), "build", str(source / "Launcher/QuantConnect.Lean.Launcher.csproj"),
                        "-c", "Release", "--no-restore", "--nologo", "-v", "quiet"],
                       cwd=repo, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=300)


def export(data: ExecutionData, output: Path, source: Path, days: list[str], *,
           variant: str = "split", bps: int = 5, capital: Decimal = CAPITAL) -> Path:
    """Write explicit simulated bid/ask quotes at observed minute-open instants."""
    output.mkdir(parents=True, exist_ok=False)
    data_dir = output / "data"
    for directory in ("market-hours", "symbol-properties"):
        shutil.copytree(source / "Data" / directory, data_dir / directory)
    folder = data_dir / "equity/hkpaper/tick/00700"
    folder.mkdir(parents=True)
    for market in ("hkpaper", "usa"):
        for name in ("map_files", "factor_files"):
            (data_dir / "equity" / market / name).mkdir(parents=True, exist_ok=True)
    for day in days:
        lines, trades = [], []
        for time, row in data.minutes.loc[day].iterrows():
            reference = Decimal(str(row.open))
            bid = execution_price(reference, "sell", bps) * 10000
            ask = execution_price(reference, "buy", bps) * 10000
            millis = (time.hour * 3600 + time.minute * 60) * 1000
            lines.append(f"{millis},{bid},200,{ask},200")
            # LEAN Equity.HasData requires a trade point. Quantity one is a
            # declared placeholder, never the minute's future aggregate volume.
            trades.append(f"{millis},{reference * 10000},1")
        date = day.replace("-", "")
        with zipfile.ZipFile(folder / f"{date}_quote.zip", "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(f"{date}_00700_tick_quote.csv", "\n".join(lines) + "\n")
        with zipfile.ZipFile(folder / f"{date}_trade.zip", "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(f"{date}_00700_tick_trade.csv", "\n".join(trades) + "\n")
    (output / "commands.json").write_text(json.dumps(schedule(days, variant)), encoding="utf-8")
    config = {
        "environment": "local-paper-test", "live-mode": False,
        "algorithm-type-name": "LocalHKPaperAlgorithm", "algorithm-language": "CSharp",
        "algorithm-location": str(source / "Launcher/bin/Release/QuantConnect.Algorithm.CSharp.dll"),
        "data-folder": str(data_dir), "results-destination-folder": str(output / "results"),
        "object-store-root": str(output / "storage"), "close-automatically": True,
        "log-handler": "QuantConnect.Logging.ConsoleLogHandler",
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
        "parameters": {"capture-start": days[0], "capture-end": days[-1], "capital": str(capital),
                       "commands": str(output / "commands.json"), "verification-output": str(output / "native.json")}}
    config_path = output / "config.json"
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return config_path


def run(data: ExecutionData, output: Path, source: Path, dotnet: Path, days: list[str], *,
        variant: str = "split", bps: int = 5, capital: Decimal = CAPITAL) -> dict[str, object]:
    """Execute native LEAN locally; missing output and nonzero exit fail closed."""
    output, source, dotnet = output.resolve(), source.resolve(), dotnet.resolve()
    config = export(data, output, source, days, variant=variant, bps=bps, capital=capital)
    with (output / "engine.log").open("w", encoding="utf-8") as log:
        subprocess.run([str(dotnet), str(source / "Launcher/bin/Release/QuantConnect.Lean.Launcher.dll"),
                        "--config", str(config)], stdout=log, stderr=subprocess.STDOUT, check=True, timeout=300)
    result = json.loads((output / "native.json").read_text(encoding="utf-8"))
    result.update(schedule=variant, bps=bps, days=days)
    return result
