# Futu + Polymarket Research Bridge

Read-only research integration between the validated Futu desktop market-data
bridge and Qlib. The project exports adjusted OHLCV data into Qlib's CSV/bin
workflow, preserves source provenance, materializes option-chain snapshots, and
supports DeepSeek JSON extraction for news, earnings transcripts, and report
PDF pages.

Polymarket is a second independent data resource: Gamma discovery, CLOB books
and prices, Data API v2 history and analytics, live WebSockets, and on-chain
data access. Both sources share one local service. See [Polymarket usage](docs/polymarket.md).

Optional [platform-specific converters](docs/platform-converters.md) let
**Qlib, NautilusTrader and locally built LEAN** consume the same fixed provider
responses through framework-specific adapters. The existing providers and Qlib exporter remain
unchanged. Actual local-data replay checks cover US equity minute bars and
Polymarket probability history, including timestamps and precision. Trading
remains manual; live adapters and replay observers place no orders. The optional
local portfolio simulator records artificial fills only.

[Continuous HK.00700 feeds](docs/live-platform-feeds.md) connect the same local
native push stream to NautilusTrader's live data client and LEAN's live data
queue. Both receive native trade and best bid/ask ticks, retry disconnected
streams, and retain source-to-callback evidence. Prepare the
[shared Python environment](docs/shared-runtime.md), then run both:

```powershell
& .\scripts\run_live_platforms.ps1
```

The optional [HK.00700 ML baseline](docs/ml-baseline.md) uses daily Futu data to
estimate next-observed-day closing-price direction. It saves inspectable
predictions and JSON model parameters for manual review, with chronological
holdout evaluation and no order placement. The first model did not demonstrate
a useful improvement over its constant probability baseline.

The [joint research framework](docs/joint-framework.md) provides one API for
native replay, continuous data feeds, Qlib features, causal Futu/Polymarket
alignment, and [costed HK.00700 model comparisons](docs/ml-comparison.md). It preserves
the fixed providers, with explicit backend selection and auditable results.
Qlib, NautilusTrader, and MLflow now use [one pinned Python environment](docs/shared-runtime.md)
with shared NumPy, pandas, and PyArrow versions. MLflow records Qlib experiments
in local SQLite. LEAN retains its .NET runtime.

[Native Qlib reinforcement learning](docs/native-qlib-rl.md) now trains Qlib's
PPO and DQN policies through its own trainer, recurrent network, interpreters,
and simple order-execution simulator. The separate [full native execution path](docs/native-execution.md)
now supports Qlib's nested day/30-minute/1-minute HK simulator and native
simulated execution in NautilusTrader and LEAN. Both Qlib simulators support
native PPO/DQN training. These workflows use real captured HK.00700 minute
data, costed artificial trades, and independent ledger verification. The
existing SB3 allocation experiment remains a separate backend. Neither sends
brokerage orders.

[Order lifecycle simulation](docs/order-lifecycle.md) supports limit/stop-market
orders, cancellations and partial fills. The [advanced lifecycle extension](docs/advanced-order-lifecycle.md)
adds stop-limits, price/quantity/trigger amendments, and GTC, HK DAY and exact-time
GTD policies. Native Nautilus/LEAN order state and a Qlib lifecycle converter
are checked against real HK.00700 minute data, including parent fees and account
balances. Liquidity is a declared simulation assumption; all trades are offline.

Full upstream feature coverage is still in progress. Inspect the explicit
feature-family inventory and available local verification evidence with:

```powershell
.\.venv-joint\Scripts\python.exe -m futu_bridge.joint capabilities
```

See the [original coverage audit](docs/framework-capability-audit.md) and the
[native RL milestone](docs/native-qlib-rl.md) and
[native execution milestone](docs/native-execution.md) for the remaining gaps.

The [Parquet operator guide](docs/parquet-operations.md) covers immutable storage,
incremental observations, durable live journals, compaction, backup and rollback.
[Native Qlib adapters](docs/qlib-parquet.md) read Parquet directly and preserve
feature/model/RL behavior; [local task workers](docs/qlib-parquet-tasks.md) replace
MongoDB coordination with fenced SQLite claims and Parquet history. `compare-models`
defaults to Parquet; `--storage legacy_bin` retains the legacy reproduction path.
Native joint RL commands default to Parquet too, with `--storage legacy_json`
preserving their original input path and unchanged learning budgets.
`research-parquet` runs a pinned daily/minute bundle through model comparison and
the three native simulated execution engines. The fixed providers and shared
environment remain unchanged.

MongoDB is no longer an executable task backend: the former task entrypoint
delegates to the local replacement, and `--mongo-uri` is rejected. The pinned
`pymongo` package remains only for upstream Qlib import compatibility. The old
MongoDB installer script is retired and cannot recreate the unused downloads.

The expanded adapters include [29 native Qlib learner classes and MetaModelDS](docs/qlib-native-models.md),
[durable online scheduling](docs/qlib-online-parquet.md), [37 native Nautilus indicators](docs/native-indicators.md),
[activation-price orders](docs/native-activation-orders.md), and
[LEAN process recovery](docs/lean-recovery.md). The [provider field inventory](docs/provider-field-coverage.md)
documents ten additional typed data families and every pinned public route,
including fields retained only as original JSON.

See the [Parquet release status](docs/parquet-release.md) for implementation
choices, real-data verification, native execution differences and the remaining
engineering/data/dependency gaps. [Panel signal analysis](docs/panel-signal-analysis.md)
checks saved learner outputs without refitting or executing trades.

The November 11 local retest passed 484 Python regressions, three JavaScript
tests, and the offline training, simulation, recovery, storage and packaging
checks. Live converters connected after the Hong Kong close but received no
fresh updates, so live freshness remains unverified. See the release status
for the exact scope and remaining gaps; tests and private captures stay local.

The [LEAN feature workflow](docs/lean-feature-coverage.md) includes native
optimization and shared-Python QuantBook verification. See
[native order coverage](docs/native-order-coverage.md) for partial brackets,
process recovery and extended order cases, and the
[development plan](docs/parquet-development-plan.md) for remaining acceptance gates.
These scoped implementations do not imply every upstream feature is integrated.

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
