"""Configure the local LEAN live data queue without QuantConnect services."""

import argparse
from pathlib import Path
import shutil

from .capture import write_json
from .live_source import hk_symbol, local_url


def configure(output, lean_source, base_url="http://127.0.0.1:8765", symbol="00700", seconds=0, disconnect_after=0):
    symbol = hk_symbol(symbol)
    if symbol != "HK.00700": raise ValueError("The LEAN live converter currently targets HK.00700")
    if seconds < 0 or disconnect_after < 0: raise ValueError("Durations must be nonnegative")
    root, source = Path(output).resolve(), Path(lean_source).resolve()
    root.mkdir(parents=True, exist_ok=False)
    data_dir = root / "data"
    for name in ("market-hours", "symbol-properties"):
        shutil.copytree(source / "Data" / name, data_dir / name)
    for name in ("map_files", "factor_files"):
        (data_dir / "equity" / "hk-futu" / name).mkdir(parents=True)
    config = {
        "environment": "live-futu", "live-mode": True, "live-mode-brokerage": "PaperBrokerage",
        "algorithm-type-name": "LocalFutuLiveAlgorithm", "algorithm-language": "CSharp",
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
        "data-queue-handler": ["QuantConnect.Algorithm.CSharp.LocalFutuDataQueue"],
        "setup-handler": "QuantConnect.Lean.Engine.Setup.BrokerageSetupHandler",
        "result-handler": "QuantConnect.Lean.Engine.Results.LiveTradingResultHandler",
        "data-feed-handler": "QuantConnect.Lean.Engine.DataFeeds.LiveTradingDataFeed",
        "real-time-handler": "QuantConnect.Lean.Engine.RealTime.LiveTradingRealTimeHandler",
        "history-provider": ["QuantConnect.Lean.Engine.HistoricalData.SubscriptionDataReaderHistoryProvider"],
        "transaction-handler": "QuantConnect.Lean.Engine.TransactionHandlers.BacktestingTransactionHandler",
        "live-cash-balance": '[{"currency":"HKD","amount":100000}]', "live-holdings": "[]",
        "futu-live-url": local_url(base_url), "futu-live-symbol": symbol, "futu-live-output": str(root),
        "futu-live-seconds": seconds, "futu-live-disconnect-after": disconnect_after,
    }
    write_json(root / "config.json", config)
    return root / "config.json"


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", required=True)
    p.add_argument("--lean-source", required=True)
    p.add_argument("--base-url", default="http://127.0.0.1:8765")
    p.add_argument("--symbol", default="00700")
    p.add_argument("--seconds", type=float, default=0)
    p.add_argument("--disconnect-after", type=float, default=0)
    print(configure(**vars(p.parse_args())))


if __name__ == "__main__":
    main()
