"""Run from the repository root with the supported Futu desktop signed in."""
import json
from futu_bridge import FutuClient, ProviderError


def main():
    with FutuClient.desktop() as client:
        print(json.dumps(client.capabilities(), indent=2))
        print(json.dumps(client.quotes(["US.AAPL", "US.ZNmain", "US.BTCmain"]).data, indent=2))
        print(json.dumps(client.bars("US.ZNmain", timeframe="1d").data, indent=2))
        print(json.dumps(client.orderbook("US.AAPL", depth=5).data, indent=2))
        print(json.dumps(client.trades("US.AAPL", limit=10).data, indent=2))
        print(json.dumps(client.stock_news("HK.00700", limit=5).data, ensure_ascii=False, indent=2))
        print(json.dumps(client.daily_short_volume("HK.00700", limit=5).data, indent=2))
        indicators = client.technical_indicators("US.ZNmain", indicators=["MA", "MACD", "RSI"])
        print(json.dumps(indicators.data["rows"][-1], indent=2))
        print(json.dumps(client.earnings_report_link("HK.00700", "2026Q2").data, indent=2))
        replay = client.earnings_call("HK.00700", "2026Q2")
        print(json.dumps(replay.data, ensure_ascii=False, indent=2))
        chain = client.option_chain("US.AAPL").data
        last = client.quotes("US.AAPL").data["quotes"][0]["last"]
        contracts = [row for row in chain["contracts"] if row.get("symbol") and row.get("side") == "call"]
        if contracts:
            option = min(contracts, key=lambda row: abs(row["strike"] - last))["symbol"]
            print("Nearest-strike call from the returned chain:", option)
            for operation in (lambda: client.quotes(option), lambda: client.bars(option),
                              lambda: client.orderbook(option), lambda: client.trades(option, limit=10)):
                try:
                    print(json.dumps(operation().to_dict(), indent=2))
                except ProviderError as exc:
                    print(json.dumps({"code": exc.code, "message": exc.message, "details": exc.details}))


if __name__ == "__main__":
    main()
