"""Run real desktop/HTTP coverage checks across the packaged market universe.

Requires the supported signed-in desktop build and the native extra. No sample
or replay data enters this audit. Results are observations, not coverage promises.
"""
import argparse
from collections import Counter, defaultdict
from contextlib import redirect_stderr, redirect_stdout
import datetime as dt
import json
from pathlib import Path
import threading

from .client import FutuClient
from .integration_audit import Audit, FEEDS, utcnow
from .provider import ProviderError
from .server import MarketDataServer


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("coverage_audit/full_test"))
    parser.add_argument("--start")
    parser.add_argument("--end")
    args = parser.parse_args(argv)
    today = dt.datetime.now(dt.timezone.utc).date()
    end = args.end or (today - dt.timedelta(days=1)).isoformat()
    start = args.start or (dt.date.fromisoformat(end) - dt.timedelta(days=6)).isoformat()
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    universe = json.loads(Path(__file__).with_name("market_universe.json").read_text(encoding="utf-8"))
    baseline = [{"symbol": s, "canonical_symbol": s, "asset_group": group, "contract_kind": "security"}
                for s, group in [("US.AAPL", "stock"), ("US.NVDA", "stock"), ("HK.00700", "stock"),
                                 ("US.SPY", "etf"), ("US..SPX", "index"),
                                 ("US.TWTR", "delisted_symbol"), ("US.SIVB", "delisted_symbol")]]
    cases = baseline + universe["instruments"]
    report = {"schema_version": 1, "started_at": utcnow(), "bar_window": {"start": start, "end": end},
              "mode": "actual_desktop_and_http_requests", "instrument_count_before_options": len(cases),
              "market_universe": universe, "policy": "A PASS means actual returned data passed field and identity checks. It does not certify feed latency, continuity, all-entitlement coverage, or continuous-futures roll methodology."}
    client = FutuClient.desktop()
    server = MarketDataServer(("127.0.0.1", 0), provider=client.provider)
    worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    worker.start()
    audit = Audit("http://127.0.0.1:" + str(server.server_port), 15, start, end)
    try:
        with (output / "requests.log").open("w", encoding="utf-8") as log:
            with redirect_stdout(log), redirect_stderr(log):
                report["health"] = audit.request("/health")
                report["capabilities"] = audit.request("/v1/capabilities")
                for index, item in enumerate(cases):
                    symbol = item["symbol"]
                    for feed in FEEDS:
                        result = audit.probe(feed, symbol, item["asset_group"])
                        result["canonical_symbol"] = item.get("canonical_symbol", symbol)
                        result["contract_kind"] = item["contract_kind"]
                    if (index + 1) % 10 == 0:
                        save(output / "progress.json", {"completed_instruments": index + 1,
                             "total_instruments": len(cases), "summary": dict(Counter(r["status"] for r in audit.results))})
                        save(output / "http_results.partial.json", audit.results)
                chain = audit.probe("option_chain", "US.AAPL", "option")
                if chain["status"] == "PASS":
                    quote = next(r for r in audit.results if r["symbol"] == "US.AAPL" and r["feed"] == "quotes")
                    quote_rows = quote.get("response", {}).get("data", {}).get("quotes", [])
                    last = quote_rows[0].get("last") if quote_rows else None
                    contracts = chain["response"]["data"]["contracts"]
                    chosen = []
                    for side in ("call", "put"):
                        matching = [r for r in contracts if r.get("side") == side and r.get("symbol")]
                        if matching:
                            selected = min(matching, key=lambda r: abs(r["strike"] - last)) if isinstance(last, (int, float)) else matching[0]
                            chosen.append(selected["symbol"])
                    report["selected_option_contracts"] = chosen
                    for symbol in chosen:
                        for feed in FEEDS:
                            audit.probe(feed, symbol, "option")
                audit.stream_probe(8)
                report["discovery_checks"] = {
                    "treasury_search": audit.request("/v1/instruments", {"query": "ZN", "limit": 20}),
                    "bitcoin_search": audit.request("/v1/instruments", {"query": "BTC", "limit": 30}),
                    "treasury_resolve": audit.request("/v1/instruments/resolve", {"symbol": "US.ZNmain"}),
                    "ticker_reuse": {s: audit.request("/v1/instruments/resolve", {"symbol": s})
                                     for s in ("US.FB", "US.META", "US.S", "US.BBBY", "US.TWTR", "US.SIVB")}}
            save(output / "http_results.json", audit.results)
            history_checks = []
            history_cases = [("aapl_long", "US.AAPL", "1d", "1980-01-01", end, "none")]
            history_cases += [("aapl_" + period, "US.AAPL", period,
                               "2026-01-01" if period in ("1w", "1mo") else end, end, "none")
                              for period in ("1m", "3m", "5m", "15m", "30m", "1h", "1d", "1w", "1mo")]
            history_cases += [("split_" + symbol + "_" + adjustment, "US." + symbol, "1d", first, last, adjustment)
                              for symbol, first, last in (("AAPL", "2020-08-27", "2020-09-02"),
                                                         ("NVDA", "2024-06-06", "2024-06-12"))
                              for adjustment in ("none", "forward", "backward")]
            for name, symbol, period, first, last, adjustment in history_cases:
                try:
                    response = client.bars(symbol, timeframe=period, start=first, end=last, adjustment=adjustment)
                    save(output / (name + ".json"), response.to_dict())
                    rows = response.data["bars"]
                    stamps = [row["timestamp_seconds"] for row in rows]
                    valid = bool(rows) and stamps == sorted(set(stamps)) and all(first <= row["date"] <= last for row in rows)
                    history_checks.append({"name": name, "status": "PASS" if valid else "INVALID_OR_EMPTY",
                                           "count": len(rows), "first": rows[0]["date"] if rows else None,
                                           "last": rows[-1]["date"] if rows else None,
                                           "pages": len(response.data["requests"]),
                                           "completion_reason": response.data["completion_reason"], "file": name + ".json"})
                except ProviderError as exc:
                    history_checks.append({"name": name, "status": "UNAVAILABLE", "code": exc.code, "details": exc.details})
            report["python_history_checks"] = history_checks
        groups = defaultdict(Counter)
        for result in audit.results:
            groups[result["asset_class"]][result["status"]] += 1
        report.update(finished_at=utcnow(), http_summary=dict(Counter(r["status"] for r in audit.results)),
                      by_asset_group={group: dict(counts) for group, counts in groups.items()},
                      http_result_count=len(audit.results), http_results_file="http_results.json")
        save(output / "report.json", report)
        save(output / "progress.json", {"completed": True, "completed_instruments": len(cases),
                                        "http_summary": report["http_summary"]})
        print(json.dumps({"output_dir": str(output), "http_summary": report["http_summary"],
                          "python_history_summary": dict(Counter(r["status"] for r in history_checks))}, indent=2))
        return 1 if any(r["status"] in ("INVALID_DATA", "UNVERIFIED_SOURCE") for r in audit.results) else 0
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
