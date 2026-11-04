# Futu + Polymarket Research Bridge

Read-only research integration between the validated Futu desktop market-data
bridge and Qlib. The project exports adjusted OHLCV data into Qlib's CSV/bin
workflow, preserves source provenance, materializes option-chain snapshots, and
supports DeepSeek JSON extraction for news, earnings transcripts, and report
PDF pages.

Polymarket is a second independent data resource: Gamma discovery, CLOB books
and prices, Data API v2 history and analytics, live WebSockets, and on-chain
data access. Both sources share one local service. See [Polymarket usage](docs/polymarket.md).

The Futu desktop application itself is not part of this repository. Run the
bridge against a locally installed, signed-in Futu desktop session. Version
0.4.0 supports Windows x64 desktop builds **16.33.17808** and **16.37.27308**,
selected by exact quote/transport DLL hashes. Unknown builds and mixed DLL
versions are rejected.

## Install

```powershell
python -m venv .venv-qlib
.\.venv-qlib\Scripts\python.exe -m pip install -e ".[native,qlib,polymarket]"
```

## Local live API

Keep Futu signed in, then start the read-only loopback service:

```powershell
.\.venv-qlib\Scripts\python.exe -m futu_bridge --provider futu_bridge.native:create_provider --port 8765
Invoke-RestMethod 'http://127.0.0.1:8765/health'
Invoke-RestMethod 'http://127.0.0.1:8765/v1/quotes?symbols=US.AAPL,HK.00700'
```

Check `provider_connected` in health and the exchange timestamps in responses.
The bridge preserves the desktop session's market-data permissions.

## Polymarket

Polymarket REST routes are enabled automatically and also work without Futu.
The `polymarket` extra adds WebSockets; REST has no extra dependencies.

```powershell
Invoke-RestMethod 'http://127.0.0.1:8765/v1/polymarket/markets?closed=false&limit=5&order=volume24hr&ascending=false'
Invoke-RestMethod 'http://127.0.0.1:8765/v1/polymarket/trades?limit=5'
Invoke-RestMethod 'http://127.0.0.1:8765/v1/polymarket/catalog'
Invoke-RestMethod 'http://127.0.0.1:8765/v1/polymarket/onchain/logs?contract=ctf_exchange&blocks=5'

# Live Polymarket books, with automatic REST snapshot fallback.
polymarket-data stream --channel market --token-id '<outcome-asset-id>' --seconds 10

# Lossless paginated export; the manifest records whether more pages remain.
polymarket-data export --resource markets --max-pages 2 --output output\polymarket\markets.jsonl
```

```python
from futu_bridge import PolymarketClient

with PolymarketClient() as pm:
    page = pm.markets(closed=False, limit=5)
    market = page.data["markets"][0]
    snapshot = pm.snapshot(market["slug"])
```

The Polymarket resource has exactly two configured providers: **Polymarket
primary**, and **dRPC as the chain-data backup**. Both use public endpoints with
no API key, paid plan, trial credits, credit card, or transaction fees for reads.
Gamma, CLOB and Data API are complementary APIs of the same primary provider.

Market streams prefer Polymarket WebSocket and automatically use official CLOB
REST book snapshots during connection failures, returning to WebSocket after
recovery. The dRPC backup is invoked only for explicit `/onchain/*` reads; it
cannot reproduce off-chain books, market metadata, or full indexed history.
External exchange feeds, Goldsky, and additional RPC providers have been removed.
The [usage guide](docs/polymarket.md) documents fallback limits and source labels.

The [US-equity event filter](docs/equity-filter.md) selects popular events first,
uses Polymarket's categories to exclude sports and entertainment, then asks
DeepSeek to assess their relevance to US stocks. Market data remains free and
keyless; the optional classifier uses your separately billed DeepSeek API.
Missing credentials leave candidates in review. Reading saved results never
calls the model.

## Export prices

```powershell
python -m futu_bridge.qlib_export `
  --symbol US.AAPL --symbol HK.00700 `
  --timeframe 1d --start 2020-01-01 --end 2026-10-23 `
  --adjustment forward --output-dir output\qlib_source --overwrite
```

Use the matching Qlib source checkout's `scripts/dump_bin.py` to convert the
CSV files to Qlib binary data. See `futu_bridge/README.md` for the full
workflow and point-in-time limitations.

## DeepSeek extraction

Set `DEEPSEEK_API_KEY` in the process environment. Never commit the key.

```powershell
$env:DEEPSEEK_API_KEY = "<your key>"
futu-deepseek-extract `
  --input-jsonl output\research\events.jsonl `
  --output-jsonl output\research\claims.jsonl
```

DeepSeek results retain source IDs, source hashes, evidence quotes, model
metadata, and extraction time. Model extraction time is not treated as a
historical availability timestamp.

## Tests

```powershell
python -m unittest discover -s futu_bridge/tests
node --test futu_bridge/tests/native_builds.test.cjs
```
