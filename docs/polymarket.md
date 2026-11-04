# Polymarket data resource

The bridge provides one Python client, one CLI, and namespaced HTTP routes for
Polymarket's public prediction-market data. REST responses preserve the complete
upstream JSON: unknown fields, non-binary markets, token identifiers, decimal
strings, timestamps, and pagination envelopes. No signing key or trading client
is constructed.

Version **0.4.0** uses exactly two market-data providers for this resource:

1. **Polymarket official APIs — primary.** Gamma supplies discovery, CLOB supplies
   live prices/books, and Data API v2 supplies indexed history and wallet data.
   These are complementary endpoints of one provider, not interchangeable feeds.
2. **dRPC public Polygon endpoint — chain backup.** `https://polygon.drpc.org`
   supports bounded raw logs, receipts and balances when chain data is needed.
   It is contacted only for explicit `/onchain/*` requests. It cannot replace
   Polymarket's off-chain order books or metadata.

**Truly free requirement:** all configured upstream market-data reads work without API keys,
paid plans, trial credits, credit cards or gas payments. No paid fallback exists.
Free endpoints still impose rate, retention, traffic and availability limits.
The catalog exposes this selection as `source_policy` with `provider_count: 2`.

The optional [US-equity event filter](equity-filter.md) adds a DeepSeek relevance
assessment after popularity and category filtering. This classifier is a
separately billed use of your DeepSeek key, not another market-data provider.

For live market data, the preferred transport is Polymarket WebSocket. If it
cannot connect or disconnects, the same stream automatically falls back to
Polymarket's public REST book snapshots while reconnecting. This is a transport
fallback within the primary provider, not an independent provider. dRPC remains
a limited chain backup; no universal free backup for all Polymarket data is claimed.

## Quick start

```powershell
.\.venv-qlib\Scripts\python.exe -m pip install -e ".[polymarket]"

# Futu + Polymarket together:
.\.venv-qlib\Scripts\python.exe -m futu_bridge --provider futu_bridge.native:create_provider --port 8765

# Alternatively, Polymarket alone (no desktop connection required):
python -m futu_bridge --port 8765
```

The optional dependency is `websocket-client`. REST uses
Python's standard library. System proxy settings and `HTTP_PROXY`/`HTTPS_PROXY`
are honored. `--disable-polymarket` disables these routes. The existing loopback,
Host, Origin, and optional bearer-token restrictions apply to both sources.

`/health.resources.polymarket` means the resource is enabled, not that every
upstream is reachable. REST responses record their actual source, endpoint, request filters, observation
time, and payload hash. Stream and chain responses also identify their source. The HTTP response
uses the existing `{request_id, served_at, data, source, warnings}` envelope;
Data API v2's own `{data, pagination}` envelope remains inside `data`.

## Resources

`GET /v1/polymarket/catalog` is the complete reference, including upstream
parameters, aliases, sources, and the free-only policy. It currently
contains **98 public Polymarket REST operations**, plus the Polygon read catalog
and stream options. The catalog is bundled with the package; startup does not
download schemas or connect to upstreams.

| Resource | Local route | Contents |
| --- | --- | --- |
| Discovery | `/v1/polymarket/markets`, `/events`, `/search` | Questions, descriptions, slugs, categories, volume, liquidity, outcomes and asset IDs |
| Discovery metadata | `/tags`, `/sports`, `/teams`, `/series`, `/comments`, `/profile` | Taxonomy, sports, series, comments and public profiles |
| Current prices | `/orderbook`, `/price`, `/midpoint`, `/spread`, `/last-trade-price` | Raw L2 snapshots and price observations for a token |
| Batch prices | `/books`, `/prices`, `/midpoints`, `/spreads`, `/last-trades-prices` | Up to 500 tokens per read |
| History | `/prices-history` | Data API v2 price observations, their resolution and settlement point where available |
| Public activity | `/trades`, `/positions`, `/activity`, `/holders` | Market/wallet trades, position lifecycle, splits, merges, redeems and holders |
| Analytics | `/oi`, `/resolutions`, `/tokens`, `/value`, `/user-pnl`, `/user-stats`, `/user-volume`, `/leaderboard`, `/live-volume`, `/status` | Interest, settlement, token mapping, wallet and aggregate analytics, freshness |
| Composite snapshot | `/snapshot?slug=...` | Market metadata, books, midpoints, spreads, recent trade prices, history, trades, OI and resolutions |
| US-equity event filter | `/equity-events`, `/equity-events/status` | Saved selections, excluded/review events, probabilities, evidence and refresh progress; POST `/equity-events/refresh` starts discovery and optional classification |
| Streaming | `/stream` | CLOB snapshots/deltas/trade prices, RTDS fills and sports |
| Raw on-chain | `/onchain/status`, `/onchain/logs`, `/onchain/balance`, `/onchain/receipt` | Free public Polygon RPC for current contracts; exact raw logs and ERC-1155 balances |

Routes in the table are relative to `/v1/polymarket`. Raw endpoint access is
also available through `/gamma/<upstream-path>`, `/clob/<upstream-path>`, and
`/data/<v2-path>`. For example:

```text
GET /v1/polymarket/gamma/markets/slug/<market-slug>
GET /v1/polymarket/data/positions/combos?user=<public-wallet>&limit=20
GET /v1/polymarket/data/builders/leaderboard?limit=20
GET /v1/polymarket/clob/tick-size?token_id=<outcome-asset-id>
```

Use the catalog's upstream parameter spellings. Unknown filters fail explicitly.
Array parameters can be repeated in the URL. The convenience `markets` and
`events` routes request both protocol versions (`version=v1&version=v2`);
raw Gamma routes preserve the upstream's defaults. Pass `closed=true` for closed
markets. A closed market is not proof of settled resolution; inspect settlement
metadata and `/resolutions`.

## Python

```python
import json
from futu_bridge import PolymarketClient

with PolymarketClient() as pm:
    page = pm.markets(closed=False, order="volume24hr", ascending=False, limit=5)
    market = page.data["markets"][0]
    token_ids = json.loads(market["clobTokenIds"])
    condition = market["conditionId"]

    books = pm.batch("books", token_ids)
    prices = pm.batch("prices", token_ids, side="BUY")  # executable ask; SELL is bid
    history = pm.history(token_ids[0], interval="1d")
    trades = pm.get("trades", condition=condition, limit=20)
    settled = pm.get("resolutions", condition=condition)

    # Full upstream contract, including endpoints without convenience aliases:
    combos = pm.request("data", "/v2/positions/combos", params={"user": "<public-wallet>"})
```

Keep asset IDs as strings: they exceed JavaScript's safe integer range.
For V1 use `clobTokenIds`; V2 can use `positionIds`. The token lookup resource
resolves the relationship among assets, conditions and protocol versions.
Metadata observation time is not historical availability time.

Batch reads are also available as JSON POSTs, which avoid URL-size limits:

```powershell
$body = @(@{ token_id = '<outcome-asset-id>' }) | ConvertTo-Json -AsArray -Compress
Invoke-RestMethod -Method Post -ContentType 'application/json' -Body $body `
  'http://127.0.0.1:8765/v1/polymarket/clob/books'
```

`POST /v1/polymarket/request` accepts
`{"api":"data","path":"/v2/trades","params":{"limit":5}}` or a catalogued
read batch with `body`. This is an allowlisted data gateway: order placement,
cancellation, API-key creation, private user endpoints and arbitrary URLs are
not exposed. `PUT`, `PATCH` and `DELETE` remain disabled.

## Pagination and exports

Gamma keyset reads use `next_cursor` → `after_cursor`; Data API v2 uses
`pagination.next_cursor` → `cursor`. These cursors are not interchangeable.
Preserve the request filters when continuing. A null cursor ends the scan.

```python
from futu_bridge import PolymarketClient
from futu_bridge.polymarket_cli import export_pages

with PolymarketClient() as pm:
    # Inspect a bounded walk of complete upstream response pages.
    for page in pm.iter_pages("trades", params={"condition": "<condition-id>", "limit": 100}, max_pages=3):
        for trade in page.data["data"]:
            print(trade["token_id"], trade["price"])

    export_pages(pm, "output/polymarket/markets.jsonl", resource="markets",
                 params={"closed": False, "limit": 100}, max_pages=10)
```

```powershell
polymarket-data get --resource markets --params-file filters.json
polymarket-data export --resource markets --params-file filters.json --max-pages 10 --output output\polymarket\markets.jsonl
polymarket-data export --resource prices-history --params-file history.json --all --output output\polymarket\history.jsonl
polymarket-data snapshot '<market-slug>'
```

`filters.json` can contain `{"closed":false,"limit":100}`. `history.json` can
contain `{"token_id":"<outcome-asset-id>","interval":"max","limit":1000}`.
Use `python -m futu_bridge.polymarket_cli` if the console entry point is not on
PATH. `--all` (Python: `max_pages=None`) explicitly walks to the end. Files are
never overwritten unless `--overwrite` is supplied.

Each JSONL line contains one complete response page, retaining all source
fields. `<output>.manifest.json` records row/page counts, `complete`, `limited`,
and `next_params`. To resume a bounded scan into a new file, save `next_params`
as the next `--params-file`. Failure leaves `.partial` and a failed manifest;
it never promotes a partial export as complete. `complete` describes the
requested query's pagination, not completeness of the upstream dataset.

For tabular analysis, flatten `page.data["data"]` (Data API) or
`page.data["markets"]` (Gamma) into pandas. These are probability observations,
not equity OHLCV bars; this integration does not invent OHLC, volume or Qlib
adjustment factors.

## Streaming

```python
with PolymarketClient() as pm:
    with pm.stream("market", token_ids=["<outcome-asset-id>"]) as feed:
        for event in feed:
            print(event["type"], event["data"])

    # Public trade fills, optionally filtered by market_slug or event_slug:
    with pm.stream("activity", market_slug="<market-slug>") as feed:
        print(feed.read(timeout=15))
```

```text
GET /v1/polymarket/stream?channel=market&token_ids=<id1>,<id2>
GET /v1/polymarket/stream?channel=activity&market_slug=<slug>
GET /v1/polymarket/stream?channel=sports
```

HTTP streams use SSE (`ready`, `update`, `gap`, `reconnected`, `error`, `end`).
`update.data` is the complete upstream event; examine its `event_type`, `topic`,
or `channel`. `book` is a snapshot; `price_change` contains deltas. Connections
heartbeat, reconnect and resubscribe automatically. Queue overflow and reconnect
gaps are explicit. Rebuild a local order book after a gap. There is no promise
of lossless delivery or replay. Closing the iterator/context releases the socket.

```powershell
polymarket-data stream --channel market --token-id '<id>' --seconds 30 --output output\polymarket\stream.jsonl
polymarket-data stream --channel activity --seconds 10
```

The `market` stream automatically falls back to free CLOB REST `/books`
snapshots during initial WebSocket connection failures and interruptions. It
continues reconnecting and restores WebSocket delivery after recovery. Snapshot
requests are limited to at most one batch every two seconds between reconnect
attempts; connection timeouts and backoff can make the interval longer.

Fallback events retain all REST fields, including the upstream `timestamp`, and
add `event_type: "book"`. Their source explicitly reports `transport: "rest"`,
`fallback_active: true`, and `snapshot_only: true`. Ordinary events use
`transport: "websocket"`. REST snapshots do not replay missed trades or deltas;
rebuild the book after a `gap`. If both transports fail, an error is emitted and
reconnection continues. A policy/subscription rejection ends the stream rather
than being treated as a transient failure. Other stream channels reconnect but
do not claim a REST equivalent. A Polymarket-wide outage can affect both transports.

RTDS activity remains public. Legacy RTDS reference topics can still be requested
explicitly, but are deprecated and may only deliver an initial snapshot. External
`reference`, Coinbase and Binance streams have been removed and return HTTP 410.
No settlement-oracle, equity or TWAP equivalence is invented. The existing Futu
provider remains the project's equity-data source under its desktop permissions.

## On-chain data and upstream changes

Data API v1 retired on **November 2, 2026**. This integration uses v2's
`condition`/`cursor` parameters. History is available from Data API v2;
the raw CLOB `/prices-history` and `/batch-prices-history` reads are also exposed.
Price history has resolution/retention limits. Read `resolution_seconds` on
each returned point; do not assume uniform spacing. Absolute Data API history
windows are limited to 15 days; `interval=max` supports longer-range retrieval.

Trade coverage also depends on the query: the bare feed covers the current and
previous month; condition/event feeds have a fixed three-year window. Time bounds
apply to wallet queries; `user=<wallet>&start=1` requests the available full wallet
history. Cursor pagination does not extend these upstream retention windows.
Positions default to open positions; use the documented `status` filter for
closed, redeemable, losing, or mergeable positions.

The paid Goldsky adapter, legacy Goldsky subgraphs, external exchange feeds, and
additional RPC providers are removed. Old Goldsky routes and external reference
streams return HTTP 410 before any network request. There is no environment
variable or paid-plan option that re-enables them.

Use the primary provider for indexed data:

```python
pm.get("activity", user="<public-wallet>", limit=20)
pm.get("positions", user="<public-wallet>", limit=20)
```

Use the single chain backup explicitly when raw on-chain data is required:

```python
pm.onchain("status")
pm.onchain("logs", contract="ctf_exchange", blocks=5)
pm.onchain("logs", contract="ctf", from_block=80000000, to_block=80000009)
pm.onchain("balance", address="<public-wallet>", token_id="<id>", contract="ctf")
pm.onchain("receipt", transaction_hash="<transaction-hash>")
```

`GET /v1/polymarket/onchain/catalog` lists the current contracts and the single
dRPC endpoint. The connection verifies Polygon chain ID 137. If dRPC fails, the
request returns an explicit error; it does not switch to a third provider.
Only status, logs, balance and receipt reads are exposed. No transaction
submission exists, and these reads do not consume wallet funds or gas.

Logs cover at most 100 blocks per request, accept optional JSON-encoded `topics`,
and retain raw RPC payloads. Explicitly chunk longer scans. Retain block hashes
and removed flags to handle reorganizations. Public RPC archive-state availability
varies; raw logs need ABI decoding and do not form a complete indexed history API.

Balance reads require an explicit token system: `contract=ctf` for CTF tokens,
or `contract=positions` for protocol V2 PositionManager tokens. Supply the correct
token ID from market metadata. Results retain uint256 quantities as decimal
strings, pinned to the reported block; they do not enumerate all tokens a wallet
ever held. `/positions` provides the primary indexed wallet view.

```powershell
polymarket-data onchain status
polymarket-data onchain catalog
# logs.json contains {"contract":"ctf_exchange","blocks":5}
polymarket-data onchain logs --params-file logs.json
Invoke-RestMethod 'http://127.0.0.1:8765/v1/polymarket/onchain/logs?contract=ctf_exchange&blocks=5'
```

## Verification and maintenance

Offline tests cover allowlisting, HTTP authorization, opaque cursor progress,
export limits and failures, raw field preservation, batch sizing, removed-source
blocking, REST fallback/recovery, empty RTDS frames, queue overflow and cleanup.

```powershell
python -m unittest discover -s futu_bridge/tests
node --test futu_bridge/tests/native_builds.test.cjs
```

Live evidence from this workspace is written to `output/polymarket/`. Credentials
are not included. Current verification is under `output/polymarket/two_sources/`. Older reports
under `output/polymarket/free/` describe earlier configurations and are retained
only as historical evidence. The freshness probe observed approximately two-second
live CLOB updates but a three-minute-old Data API trade page; an API responding
successfully does not prove that its data is current.

To refresh the reviewed endpoint catalog, install development dependency PyYAML,
run `python scripts/update_polymarket_catalog.py`, inspect the diff, and rerun
tests. The script includes only public GETs and explicitly approved read POSTs.

Official references:

- [Polymarket market data](https://github.com/Polymarket/agent-skills/blob/main/market-data.md)
- [Data API v2 migration](https://docs.polymarket.com/migrate/data-api-v1-to-v2)
- [RTDS → PolyBolt migration](https://docs.polymarket.com/migrate/rtds-to-polybolt)
- [Goldsky current data and subgraph migration](https://docs.goldsky.com/chains/polymarket)
- [Polygon free public RPCs](https://docs.polygon.technology/pos/reference/rpc-endpoints)
- [Polymarket current contracts](https://docs.polymarket.com/resources/contracts)
