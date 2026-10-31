# Futu desktop market-data library

`futu-desktop-bridge` is a Python library and optional local HTTP API around a signed-in Futu desktop session. Version 0.2.3 downloads quotes, historical candles, option chains, order books, public trade prints, stock news, earnings schedules, and daily short-selling volume. It also computes technical indicators and resolves historical earnings pages, call recordings, AI transcript text, and observed original report PDFs. Intraday US stock/option candles support explicit trading-session selection. Streaming uses native desktop subscriptions by default; an explicit polling mode is retained. It has no account or order-entry functions.

The native adapter is validated against **Windows x64, Futu 16.33.17808, Python 3.10**. It checks the on-disk QotBase.dll and CoreBiz.dll hashes before attachment, then verifies the loaded module paths. News additionally checks FTNews.dll; calendar/report/short-volume requests check FTQuote.dll. Other builds are rejected. Futu must remain running and signed in. No OpenD, OpenAPI key, Alpaca account, or new data purchase is used. Existing app/server permissions still determine feed availability.

## Python usage

From this checkout, D:\python\python.exe can use the native dependency already in coverage_audit/vendor. For another environment:

```powershell
python -m pip install -e '.[native]'
```

The core library and HTTP server use the standard library. The desktop adapter additionally requires Frida; the native extra includes tzdata on Windows for market-local calendar defaults. A wheel is available under dist/; it includes agent scripts and the instrument universe, and excludes local databases, evidence, and vendored dependencies.

```python
from futu_bridge import FutuClient, ProviderError

with FutuClient.desktop() as client:
    quotes = client.quotes(["US.AAPL", "US.ZNmain", "US.BTCmain"])
    daily = client.bars("US.ZNmain", start="2026-10-01", end="2026-10-23")
    minute = client.bars("US.AAPL", timeframe="1m",
                         start="2026-10-23", end="2026-10-23")
    book = client.orderbook("US.AAPL", depth=5)
    prints = client.trades("US.AAPL", limit=100)
    tencent = client.quotes("HK.00700")
    news = client.stock_news("HK.00700", limit=20)

    chain = client.option_chain("US.AAPL")  # Nearest current expiry.
    calls = [row for row in chain.data["contracts"]
             if row.get("symbol") and row.get("side") == "call"]
    last = quotes.data["quotes"][0]["last"]
    option = min(calls, key=lambda row: abs(row["strike"] - last))["symbol"]
    option_quote = client.quotes(option)
    option_bars = client.bars(option)
    option_book = client.orderbook(option)
    option_prints = client.trades(option, limit=20)

    print(daily.data["bars"])
    print(daily.source, daily.warnings)
```

All data calls return MarketDataResponse with data, source, warnings, request_id, and served_at; .to_dict() returns a serializable envelope. Use exchange/bar timestamps to assess freshness, not local served_at or observed_at. ProviderError exposes code, message, status, and details. Unknown symbols, permission denials, unavailable feeds, and empty results remain explicit. A partial quote batch contains per-row availability; an unresolved symbol rejects the batch before requesting data.

FutuClient.desktop(pid=..., catalog_path=..., stream_mode="push", push_queue_size=1024) supports optional local configuration. Use stream_mode="poll", poll_interval=1 for the older polling adapter. FutuClient() starts unconnected; importing the library does not attach. Context managers close streams and the provider. Run the repository example with `python -m examples.read_market_data`.

A specific current expiry can be selected with client.option_chain("US.AAPL", expiry="2026-10-30"). Results include available_expiries, native IDs, symbols, side, strike, expiry, and original metadata. Contracts are registered within that client/provider session. Discover the chain first in a new session: option symbols may be absent from the ordinary stock catalog. Expired-contract discovery is not implemented. US option labels are verified; Hong Kong routing remains experimental.

## Discover instruments

```python
with FutuClient.desktop() as client:
    matches = client.search_symbols("ZN", limit=20)
    identity = client.resolve_symbol("US.ZNmain")
    bitcoin = client.search_symbols("BTC", limit=30)
```

Search reads the public security master with SQLite mode=ro and the live WAL. Results include unambiguous ID.<stock_id> identifiers and aliases such as US.AAPL, HK.00700, FX.EURUSD, or US.ZNmain. Different venues can share a code, especially bonds and crypto pairs; use IDs to disambiguate. Catalog presence establishes identity, not price-feed coverage.

market_universe.json contains **126 catalog-verified instruments**: 55 continuous futures, 55 actually linked dated contracts, 12 spot instruments, one Bitcoin reference series, and three cash bonds. It covers Treasury/interest-rate futures, major and micro equity indices, volatility, energy, metals, grains/oilseeds, livestock, currency futures, Bitcoin/Ether futures, Hong Kong index futures, forex spot, and Bitcoin spot venues. Requested roots absent from the catalog are recorded explicitly.

| Group | Examples |
|---|---|
| Treasury futures | US.ZTmain, US.ZFmain, US.ZNmain, US.ZBmain, US.UBmain |
| Equity index futures | US.ESmain, US.NQmain, US.YMmain, US.RTYmain |
| Energy | US.CLmain, US.NGmain |
| Metals | US.GCmain, US.SImain, US.HGmain |
| Grains/oilseeds | US.ZCmain, US.ZWmain, US.ZSmain |
| Currency futures | US.6Emain, US.6Jmain |
| Bitcoin futures | US.BTCmain, US.MBTmain |
| Bitcoin spot aggregate | ID.12001145 (BTC/USD), ID.12001151 (BTC/USDT) |
| Bitcoin venues | ID.12002025 (Coinbase BTC/USD), ID.12001954 (HashKey BTC/USD) |
| Forex | FX.EURUSD, FX.USDJPY, FX.USDCNH |

ZNmain is a continuous 10-year Treasury **futures** series. Cash bonds, continuous series, and dated contracts are distinct instruments. Continuous-futures roll rules, historical constituent mappings, and back-adjustment methodology are not established. Futures prices retain quote units; the library does not compute contract P&L or infer multipliers from price alone.

Actual coverage is recorded in coverage_audit/full_test/report.json and http_results.json, with a readable per-instrument table in RESULTS.md. Retrieval success does not certify complete history or real-time latency.

The October 25, 2026 audit covers **135 instruments and 542 HTTP feed/chain/stream checks**. Final outcomes: 494 data checks passed; 26 feeds were unavailable; 18 were blocked by native permissions; two history windows were empty; one cash-index trade request was explicitly unsupported; the stream delivered three initial snapshots without a subsequent market update. All 16 actual Python history checks and all 100 offline/local-HTTP regression tests passed. There were no invalid-data results or decoder HTTP 502 failures in the broad run. Four targeted checks were repeated after correcting index semantics and the audit reader's size bound; original observations are preserved.

## Qlib preparation

The bridge includes a read-only exporter for Qlib's documented CSV source
format. It writes one CSV per instrument plus a JSON provenance sidecar; use
Qlib's own `scripts/dump_bin.py` for the subsequent calendar, instrument, and
binary-feature conversion. The exporter never calls an order or account API.

Keep the live bridge interpreter separate from the Qlib research environment:

```powershell
& D:\python\python.exe -m venv .venv-qlib
& .\.venv-qlib\Scripts\python.exe -m pip install -e '.[qlib]'
```

Use the existing bridge interpreter for the signed-in desktop export:

```powershell
& D:\python\python.exe -m futu_bridge.qlib_export `
  --symbol US.AAPL --symbol HK.00700 `
  --timeframe 1d --start 2020-01-01 --end 2026-10-23 `
  --adjustment forward --output-dir output\qlib_source --overwrite
```

For forward or backward adjusted prices, the exporter makes a second,
unadjusted Futu request and derives a per-date Qlib restoration factor from
`adjusted_close / unadjusted_close`. If the two responses do not cover the
same calendar values, export fails instead of silently writing an incomplete
factor series. Futu's adjustment methodology and point-in-time revisions are
still provider-defined; inspect the JSON sidecar before using the dataset for
research. Use `--adjustment none` only when you intentionally want raw prices;
raw prices across splits are unsuitable for ordinary return features unless
you apply the corporate actions separately. For adjusted exports, volume is
derived from the matching raw Futu volume divided by the paired price factor,
which follows Qlib's adjusted-volume convention.

The PyPI wheel does not include Qlib's `scripts` directory. Clone the matching
Qlib source tag once, then run its official converter (the repository's Python
package and converter should use the same release):

```powershell
git clone --depth 1 --branch v0.9.7 https://github.com/microsoft/qlib.git $env:LOCALAPPDATA\qlib-src-v0.9.7
& .\.venv-qlib\Scripts\python.exe $env:LOCALAPPDATA\qlib-src-v0.9.7\scripts\dump_bin.py dump_all `
  --data_path output\qlib_source `
  --qlib_dir output\qlib_data\futu_day `
  --freq day --date_field_name date `
  --include_fields open,close,high,low,volume,factor `
  --max_workers 1
```

Load the resulting local provider with `qlib.init(provider_uri=..., region=REG_US)`
and request features with `qlib.data.D.features(...)`. The provider URI only
contains the instruments exported by this process; Qlib does not add an
exchange calendar or delisted securities automatically. The included
`examples/read_qlib_futu.py` prints the loaded feature frame.

The exporter maps `US.AAPL` to the Qlib instrument filename `AAPL` and
`HK.00700` to `HK_00700`; the original Futu symbols and all response
provenance remain in `futu_qlib.metadata.json`. Continuous-futures roll rules,
survivorship-free universe membership, and calendar completeness are not
inferred by this adapter.

## Unstructured and option features

The bridge now includes a provenance-first research layer in
`futu_bridge.research_features` and `futu_bridge.option_features`.

The raw event representation is JSONL-friendly and keeps:

- `instrument`, `source_type`, `source_id`, `event_time`, and optional `available_at`
- original text and SHA-256 hash
- publisher/language
- `issuer_verified`, `ai_generated`, and `point_in_time_safe`
- source-specific metadata such as transcript cue offsets, PDF page numbers,
  report fiscal period, Futu response provenance, and warnings

Supported source types are `news_headline`, `earnings_transcript`,
`report_pdf_page`, and `futu_ai_summary`. News remains headline-only because
the current native news decoder does not retrieve article bodies. Earnings-call
page transcripts and subtitles are retained as AI-generated or machine-
translated content and are not issuer-verified.

Futu's current desktop also exposes a `牛牛AI` panel and static evidence shows
financial-report brief/detail-card services and highlight-content types. The
native request/reply schema for those cards is not decoded in this bridge yet.
`futu_ai_summary_event(...)` is therefore an explicit integration seam for a
future verified response; it marks the result AI-generated and unverified by
default instead of pretending that a UI summary is already a stable API.
The read-only status and evidence requirements are recorded by
`coverage_audit/probe_report_ai_summary.py`; a protocol identifier alone is not
treated as a usable summary feed.

Daily numeric features can be materialized with:

```python
from futu_bridge import aggregate_daily_features, news_events, write_feature_csv

events = news_events(news_response, available_at=None)
rows, manifest = aggregate_daily_features(events, date_basis="event_time")
write_feature_csv(rows, "output/qlib_source/AAPL_text_features.csv")
```

To add the feature columns to the OHLCV CSV that Qlib will convert, use the
strict merge helper after exporting prices:

```python
from futu_bridge import aggregate_daily_features, merge_features_into_qlib_source

rows, manifest = aggregate_daily_features(events, date_basis="available_at")
merge_features_into_qlib_source("output/qlib_source", rows)
```

The merge rejects missing dates, duplicate feature dates, and attempts to
overwrite `open`, `high`, `low`, `close`, `volume`, or `factor`. Qlib's
`dump_bin.py` can then include the added columns alongside the price fields.

### DeepSeek extraction

All event text can optionally be sent through DeepSeek's OpenAI-compatible Chat
Completions API. The extractor uses `https://api.deepseek.com/chat/completions`,
the `deepseek-flash` model by default, and JSON mode. Set the key only in the
process environment:

```powershell
$env:DEEPSEEK_API_KEY = "<your key>"
```

Never put the key in source files, `.env` files committed to the repository,
fixtures, or provenance JSON. The extractor does not log request headers or
source text. It sends bounded batches and returns validated JSON claims with
evidence quotes that must occur in the submitted source chunk. Unsupported or
unquoted claims are dropped rather than promoted to features.

```python
from futu_bridge import DeepSeekExtractor, news_events

events = news_events(news_response, available_at=None)
extractor = DeepSeekExtractor()
claims = extractor.extract_events(events)
```

Each result retains the source event ID, source hash, chunk index, model,
prompt version, response ID, usage metadata, extraction timestamp, and the
original `source_available_at` value. Model extraction time is never treated as
the time at which the information became available to a historical strategy;
results are marked `point_in_time_safe=False` until the source itself carries a
verified availability timestamp and the downstream feature policy explicitly
allows it.

DeepSeek JSON mode requires `response_format={"type":"json_object"}` and a
prompt that explicitly requests JSON. The current implementation follows that
contract and validates the returned envelope before accepting any claims.

For point-in-time-safe research, pass an explicit `available_at` from a
verified ingestion observation and aggregate on `date_basis="available_at"`.
Without that field, the manifest explicitly reports that the feature set is
not point-in-time safe. The generated numeric fields include headline,
transcript, report-page, and AI-summary counts, text volume, publisher
diversity, issuer/AI flags, and transparent lexical counts for uncertainty,
guidance, catalysts, and pressure terms. These are research features rather
than sentiment labels.

Option-chain snapshots are handled separately because Futu's current chain is
not a historical option-universe archive. `option_snapshot_features(...)`
counts only contracts with verified native symbols and call/put labels, keeps
server-ID-only rows separate, combines matching quote snapshots when supplied,
and marks the result `option_pit_safe=0` unless an explicit historical
availability timestamp is provided. This lets Qlib consume option surface
snapshot features without overstating historical coverage.

## Historical data and backtesting

Timeframes: 1m, 3m, 5m, 15m, 30m, 1h, 1d, 1w, 1mo; 1Day aliases 1d. Dates are inclusive market dates. Timestamp bounds require a timezone, e.g. 2026-10-23T13:30:00Z. Candles retain UTC timestamps, market dates, native integers, price scale, and volume precision. Native timestamps are preserved; do not assume every provider labels intervals identically.

Mixed date/timestamp ranges preserve those distinct meanings. A timestamp start and date-only end on the same market day are accepted; routing does not turn the end date into UTC midnight. Clearly reversed ranges are rejected, and the provider filters native market dates and exact timestamp endpoints.

### Trading sessions

`bars(..., session=...)` and `technical_indicators(..., session=...)` accept these selections. The HTTP bar and indicator routes accept the same `session` parameter.

| Selection | Returned sections |
|---|---|
| `provider_default` | Preserves Futu's existing default request, `KLINE_PROB_NONE=0`; stock and option defaults can differ |
| `regular` | Observed regular section only |
| `premarket` | Observed premarket section only |
| `postmarket` | Observed postmarket section only |
| `extended` | Premarket, regular, and postmarket sections |
| `overnight` | Observed overnight section only |
| `all` | Union of the extended and overnight native feeds, with overlapping regular candles deduplicated |

```python
with FutuClient.desktop() as client:
    regular = client.bars("US.MSFT", timeframe="1m",
                          start="2026-10-23", end="2026-10-23", session="regular")
    all_sections = client.bars("US.MSFT", timeframe="1m",
                               start="2026-10-23", end="2026-10-23", session="all")
    premarket = client.technical_indicators(
        "US.MSFT", timeframe="1m", start="2026-10-23", end="2026-10-23",
        session="premarket", indicators=["MA", "RSI"])
```

Explicit selections currently support intraday US stocks and US equity options whose identities were verified by a chain lookup in the same client. Native ID aliases work too. Other instruments and daily/weekly/monthly candles retain `provider_default`; explicit session selection fails for those scopes instead of imposing US stock hours on them. A successfully accepted timeframe or empty response does not establish data coverage.

The installed descriptor confirms `KLINE_PROB_NONE=0`, `KLINE_PROB_US_WITH_BA=1`, and `KLINE_PROB_WITH_OVERNIGHT=2`. Actual MSFT/AAPL stock requests with enum 1 return premarket + regular + postmarket; enum 2 returns overnight + regular. Consequently, `all` requests both feeds. Actual MSFT option responses retain their pre/post sections even with enum 0, so requesting enum 0 alone does not implement regular-only options.

Selection uses the provider's native `trade_section` values, without a clock-hour filter. The observed stock mapping is 12=regular, 5=premarket, 6=postmarket, 11=overnight; the option mapping is 21 or 22=regular, 23=premarket, 24=postmarket. Different US option regimes use different regular-section IDs: the tested SPCX call/put use 21 for their regular-only default response, while the tested AAPL/MSFT extended-session options use 22. The older bundled enum descriptor does not name all these newer IDs, so semantic labels remain explicitly marked `session_mapping_basis="observed_provider_sections"`, `section_enum_labels_verified=False`, and `session_hours_verified=False`. `session_filter_verified=True` describes the tested filtering operation. Unknown section IDs cause an explicit error. Returned candles retain raw IDs, native timestamps, `point_type`, and zero-volume/replenished bars; an option premarket chart candle does not establish that a trade occurred then.

October 23 one-minute downloads returned MSFT stock candles in these counts: 390 regular, 330 premarket, 240 postmarket, 480 overnight, 960 extended, and 1,440 all-section candles. The $525 call and put each returned 390 regular, 115 premarket, 15 postmarket, 520 extended/all, and no selected overnight candles. These are observed counts, without a continuity or exchange-calendar completeness guarantee. The option premarket section stops at 13:25 UTC and regular starts at 13:31 UTC; the adapter preserves that gap. Evidence: `coverage_audit/session_debug/public_audit.json` and the raw mode comparisons in `summary.json`.

`all` compares every retained field when native feeds overlap. A conflict in OHLCV, turnover, volume precision, point type, or other native fields raises `session_feed_conflict`. The narrow exception is a paired difference in `previous_close` and its raw field 11 with all other fields identical: the preceding section can differ between feeds. The merged `previous_close` becomes null, canonical raw field 11 is omitted and marked ambiguous, and both complete original bars remain under `native_session_feed_variants` with their enum provenance. Source/data metadata report `ambiguous_previous_close_count` and the merge policy; single-feed values remain unchanged. The recorded first MSFT regular candle demonstrates this context difference: 517.56 in the extended feed versus 516.27 in the overnight feed. Offline integrity evidence: `coverage_audit/session_debug/overlap_integrity_audit.json`.

Additional actual checks covered stock 3m all (480 candles, one previous-close ambiguity), 15m extended (64), 30m regular (13), option 15m premarket (8), and option 30m postmarket (1). The tested October 26 $525 call/put returned empty 3m histories, including provider-default follow-up requests; 3m option coverage is therefore unverified. Earlier actual checks covered 1m, 5m, and 1h. Evidence: `remaining_intervals_live.json` and `followup_option_3m.json` in the same audit directory.

SPCX October 26 $147 call/put default responses each contained 390 native-section-21 candles on October 23, with exactly the same timestamps as the observed MSFT regular section: 13:31–20:00 UTC. The adapter retains raw section 21 rather than rewriting it to 22. After adding this observed mapping, actual Python requests returned the same 390 unmodified native bars for `regular`, `extended`, and `all`; `premarket`, `postmarket`, and `overnight` each returned an explicit empty selection. An HTTP `regular` request also returned 390 bars. All 15 checks completed: nine actual-data passes and six empty selections. Saved-response regressions cover this additional option regime and still reject unknown section IDs. Initial failures remain in the original SPCX audit; fixed responses and the summary are separate under `coverage_audit/spcx/fixed_sessions_live.json`.

```python
adjusted = client.bars("US.AAPL", start="2020-08-27", end="2020-09-02",
                       adjustment="forward")
```

The default adjustment is none. Forward/backward select verified native enum values, and each response page must echo the requested mode. All three modes were downloaded across AAPL's 2020 and NVDA's 2024 split windows. These are provider-applied adjustments, not an independently reconstructed total-return series or a futures-roll adjustment. There is no standalone corporate-action event endpoint.

With start supplied, the adapter paginates up to 100 pages / 100,000 candles. Without it, the latest 500 candles are requested. Results include requests, completion_reason, older_history_available, truncated, and coverage_continuity_verified=False. lower_bound_reached means pagination crossed the requested start; provider_history_exhausted means the server stopped earlier. Neither certifies every expected trading date. Empty results never count as coverage.

Version 0.2.3 fixes a start-day pagination defect: reaching a row on the starting date no longer stops an intraday request partway through that day. The reader continues to a prior market date or explicit provider exhaustion. Conflicting duplicate timestamps within or between pages fail with `native_result_invalid`, while exact duplicates deduplicate without discarding raw fields. Clearly future-only ranges make no chart request, return no bars, and retain unknown `older_history_available=None`; ranges extending into the future cannot claim `complete_requested_range=True`. Inspect `requested_range_extends_into_future` and `completion_reason`.

The actual long-history download returned **11,075 AAPL daily candles from 1982-11-12 through 2026-10-23**, across 12 pages. The requested start was 1980, so this is provider history exhaustion. October 23 returned **390 one-minute candles**.

The public catalog is not a point-in-time universe. Delisted tickers can be absent, and old tickers can refer to new issuers. Historical identity and membership data are still needed for survivorship-bias-free backtests.

## Technical indicators

```python
with FutuClient.desktop() as client:
    result = client.technical_indicators(
        "HK.00700", timeframe="1d", start="2025-10-01", end="2026-10-23",
        adjustment="forward", indicators=["MA", "MACD", "RSI", "BOLL"])
    print(result.data["rows"][-1]["indicators"])
    print(result.data["parameters"], result.data["warmup"])
```

Eight indicator families are computed locally from one actual Futu candle response: SMA (5/10/20/60), EMA (12/26), MACD (12/26/9), RSI (6/12/24), KDJ (9/3/3), Bollinger Bands (20, 2 standard deviations), ATR (14), and OBV. Omit indicators to calculate all eight. Names are case-insensitive; MA and BOLL are accepted aliases. `futu_bridge.indicators.compute_indicators(bars, selection=None)` calculates the same values from an existing chronological OHLCV list without connecting to Futu.

Each response states formulas, parameters, seed rules, warm-up requirements, and the original bar provenance. EMA is seeded from the first full-period mean; RSI and ATR use Wilder smoothing; Bollinger uses population standard deviation; the MACD histogram is twice DIF minus signal. KDJ starts K and D at 50 before the first nine-bar RSV. OBV starts at zero; missing volume makes it unavailable from that bar onward. Values remain null until their seed window exists.

Periods count supplied bars. No extra prehistory is silently fetched and no missing sessions are filled, so supply an earlier start when the strategy needs a warm-up window. Recursive values depend on the first input bar. Calculations use only current and preceding bars, with prefix-invariance regression tests. These explicit formulas have not been checked for exact equality with every configurable Futu chart indicator. The result is marked locally derived, and retains the candle response's adjustment, coverage, and incomplete-latest-bar limitations.

Actual May 1–October 23, 2026 downloads passed all eight families on AAPL (122 daily bars), Tencent (120), ZNmain (122), USD/JPY (126), a Bitcoin reference (176), BTC/USD spot (176), and the server-discovered AAPL June 17, 2027 $350 call (122). Each invocation fetched candles exactly once and retained bar fields and provenance. Evidence: coverage_audit/push_test/indicator_live_cross_asset.json. This checks computation on real downloaded data, not native chart-value parity.

## Daily short-selling volume

```python
with FutuClient.desktop() as client:
    page = client.daily_short_volume("HK.00700", limit=50)
    print(page.data)
    if page.data["has_more"] and page.data["next_cursor"]:
        older = client.daily_short_volume("HK.00700", limit=50,
                                          cursor=page.data["next_cursor"])
```

US stocks use native protocol 6699 and Hong Kong stocks use 6379. The verified page limit is 1–50. The response preserves exact share counts and computes the fraction and percentage as short-selling shares divided by total shares. This is daily trading flow, not outstanding short interest. Futu describes US coverage as NYSE/Nasdaq groups and HK coverage as HKEX; US results are not claimed to cover every venue. See [Futu's definitions](https://www.futuhk.com/en/support/topic1_535).

Optional start/end are inclusive YYYY-MM-DD dates that filter the current downloaded page. They do not cause automatic historical traversal. Keep following next_cursor when has_more is true, even if a locally filtered page is empty. Native missing-value sentinels remain null; no publication-time availability or complete-history claim is made.

Actual October 23 records: AAPL 5,619,806 short shares / 39,539,690 total shares = 14.2130755%; Tencent 3,218,500 / 33,161,473 = 9.7055399%. Both stocks returned two 20-record pages with no overlapping dates. Fifty-record pages reached August 14 and August 13 respectively, with older records still indicated. Python and HTTP pagination/date/error checks passed. Evidence: coverage_audit/short_volume/short_public_audit.json.

## Stock news

```python
with FutuClient.desktop() as client:
    page = client.stock_news("US.AAPL", limit=20)
    print(page.data["items"])
    if page.data["has_more"] and page.data["next_cursor"]:
        next_page = client.stock_news("US.AAPL", limit=20,
                                      cursor=page.data["next_cursor"])
    dated = client.stock_news("HK.00700", limit=100,
                              start="2026-10-22", end="2026-10-23")
```

This uses the desktop's native stock-news service, protocol 7912. It returns headline IDs, titles, publication times, links, and available publisher/language metadata. It does not download article bodies. FTNews.dll is checked separately against the verified build; unsupported or unloaded news modules leave this capability unavailable.

The limit is 1–100 items per request. Follow the opaque next_cursor only when has_more is true; a terminal response may still contain a cursor. Dates use UTC and include the whole end date; timestamps require a timezone. Local date filtering can leave an empty page with has_more still true. returned_count and filtered_out_count explain this case. News publication timestamps do not establish when the information first became available, and the feed is not a complete historical archive.

The actual AAPL test downloaded two pages containing 40 distinct headlines with no overlap, a separate 100-item page, and 39 items within the October 22–23 UTC window. An empty future window was also verified. Tencent returned 20 items. Evidence is saved in coverage_audit/push_test/library_news_audit.json, coverage_audit/evidence/news_limits_dates.json, and the Hong Kong audit below.

## Earnings calendar

```python
with FutuClient.desktop() as client:
    schedule = client.earnings_calendar("HK.00700", start="2026-11-01", end="2026-11-30")
    print(schedule.data["events"])
    print(schedule.data["complete_for_native_selection"], schedule.data["coverage"])
```

This reads Futu's public reporting schedule for US and Hong Kong stocks through native protocol 20024. Bounds are inclusive YYYY-MM-DD provider calendar dates, up to 93 dates per call. Timestamps are rejected explicitly. The default start is today in the instrument's market timezone and the default end is 62 days later. The adapter uses seven-day windows, 1,000 records per page, and at most 50 pages, then filters exactly by the resolved stock ID.

Per-day totals are reconciled with distinct downloaded records. `complete_for_native_selection` refers only to the native list's event selection; it is not a complete historical or point-in-time reporting archive. Count changes, overlapping pages, missing days, or the request cap leave coverage incomplete. An absent event on an incomplete date means unknown. Fiscal labels and dates are provider values; confirmed/estimated status and the meaning of a separate native timestamp remain unverified and are not inferred. This function does not create or change personal reminders.

## Historical earnings pages and calls

```python
with FutuClient.desktop() as client:
    page = client.earnings_report_link("HK.00700", "2026Q2")
    call = client.earnings_call("HK.00700", "2026Q2",
                                include_transcript=True, language="page")
    print(page.data["report"])
    print(call.data["call"]["recordings"])
    print(call.data["transcript"])
```

Report lookup uses native protocol 20846 and accepts the provider fiscal label YYYYQ1 through YYYYQ4. It returns a public earnings-hub webpage, not an original filing PDF. Only public id, period, and stock_id query parameters are retained. AAPL native fiscal 2026Q3 returned URL period 2026Q2; native 2025Q4 returned URL period 2025Q3. Both labels are preserved without inventing a conversion.

The call helper follows that verified link to anonymous public Futu report/call pages. It returns the meeting identity, recording URLs, language metadata, and subtitle links. include_transcript=False is the default. With language="page", optional transcript text is the public page's explicitly marked AI-generated section, without timing. It is not issuer-verified; dates or wording may be inconsistent. Other accepted selections are original, en, zh-Hans, zh-Hant and request the corresponding SRT. Actual subtitle requests returned HTTP 403; the function preserves transcript_error and does not silently substitute another language or page text. Recording links may expire, and no full audio/video download or playback availability is claimed.

Actual AAPL 2026Q3 returned two recording variants and four subtitle tracks; Tencent 2026Q2 returned three recording variants and four tracks. Their public AI transcript sections contained 217 and 155 paragraphs respectively. No comments or viewer/account data are exposed. Evidence: coverage_audit/news_earnings/public_reports/calls_live_audit.json and the release-wheel smoke audit.

## Original report PDFs

```python
from futu_bridge import issuer_reports, announcement_documents, download_report_pdf

# Explicit issuer source; currently implemented for Tencent only.
archive = issuer_reports("HK.00700", year=2025)
print(archive.data["documents"])

# Use an attachment URL actually returned by an archive or observed in Futu.
url = archive.data["documents"][0]["url"]
download = download_report_pdf(url, "tencent_report.pdf")
print(download.data["sha256"], download.data["bytes"])

# A public announcement may deny access or lack attachment links.
# This returns an explicit error in that case, never a fabricated URL.
# documents = announcement_documents("306888691")
```

The PDF downloader accepts verified public document hosts and routes, bounds response size, checks PDF header/EOF, records SHA-256, and atomically writes a new local file without overwriting. It does not alter the source PDF or automatically extract financial facts. The default limit is 50 MiB, configurable up to 100 MiB. PDF download is a Python utility, not a remote filesystem-write HTTP endpoint. Version 0.2.2 accepts strictly validated public SEC EDGAR attachment paths under `/Archives/edgar/data/<CIK>/<accession>/<filename>.pdf`; an accepted route does not establish remote access.

Report access errors distinguish transport context from document discovery. `public_http_error` retains the upstream HTTP status; HTTP 401, 403, and 429 map to service-unavailable status 503. A recognized announcement browser challenge produces `desktop_context_required`, while a parsed page with no verified attachment anchors produces `document_links_unavailable`. The app can open a report in its browser context while a separate anonymous request is rejected. The anonymous utility reports that outcome explicitly; no working desktop-context PDF extraction is claimed here.

The signed-in app's Tencent announcement viewer exposed original attachments for 2025 Annual Report (notice 306888691) and 2026 Interim Report (notice 307975385). Both downloaded anonymously: 4,428,270 bytes / 282 pages and 5,451,089 bytes / 122 pages. Covers were rendered and visually verified. Original Chinese PDF text has imperfect font mappings, so plain text extraction is not assumed reliable.

Anonymous announcement-page resolution did not expose usable attachment links in the actual test; automatic Futu historical filing discovery remains incomplete. `issuer_reports` separately reads Tencent's official archive and found eight annual/interim report links. This explicit issuer adapter currently supports HK.00700 only; it is not silently substituted for Futu and does not establish complete filing history. Generic Apple original-PDF discovery and all-company Futu announcement pagination remain unimplemented. Evidence: coverage_audit/news_earnings/public_reports/documents_live_audit.json; downloaded originals: output/pdf/.

Separately observed links from SpaceX's official investor-relations event pages downloaded through the current `download_report_pdf` utility: the Q2 2026 Form 10-Q (150 pages), Q3 results (13 pages), Q3 Form 10-Q (64 pages), and Q3 edited webcast transcript (13 pages). These are explicit issuer-public attachments on `s21.q4cdn.com`, restricted to the observed tenant `184289198` and its public financial/content directories. They are separate from Futu's report/call data and do not establish a complete SpaceX archive or resolve the Microsoft SEC/desktop-context access case. The SpaceX Q3 issuer transcript PDF also does not imply that the Futu call helper returned a transcript. Exact URLs, original bytes, SHA-256 hashes, page counts, and rendered covers are recorded in `coverage_audit/spcx/documents.json`; local originals are under `output/pdf/`.

## Books, trades, and native push

Quotes, books, and recent trades normally request fresh responses through the desktop session. Native auth checks, book selectors, and selected crypto broker are preserved. Delayed/BMP quote mode currently reads the existing delayed cache; its separate request protocol is not implemented. Some instruments therefore have candles but lack quote/book/trade access.

Book depth is a maximum request, not a guarantee. At most 50 levels are decoded, and many instruments return best bid/ask only. Inspect array lengths, book_kind, and warnings. Unknown market timestamps remain null with raw times retained. Volume is normalized only when its precision is known.

trades() returns actual public prints, up to 1,000 in a bounded recent window. It does not return brokerage orders or complete historical tape. Trade IDs remain exact strings; high-precision sizes retain decimal strings. Rows sort by exchange timestamp and exact native ID. Futu's tick-direction classification is not a verified aggressor flag.

```python
with FutuClient.desktop(stream_mode="push") as client:
    chain = client.option_chain("US.AAPL")  # Registers real current option symbols.
    with client.stream("US.AAPL", types=["quotes", "orderbook", "trades"]) as stream:
        event = stream.read(timeout=10)
        if event:
            print(event.type, event.result.data, event.result.source)
        print(stream.status())
        # Explicit reconnect after a capture interruption, when needed:
        # stream.reconnect()
```

Native streams allocate dedicated Futu subscription handles and observe protocol 6212 callbacks. Quote bit 0, book bits 3/4, and trade bit 35 are filtered to the requested instruments before leaving the native observer. There are no repeated price, book, or recent-trade requests. First observations of each instrument/bit/probability/broker are conservatively labeled initial_snapshot=True; Futu's recovered schema has no verified bootstrap flag. Inspect exchange timestamps before calling an observation new market activity.

Book messages may replace only one side or patch individual best-bid/ask fields. The decoder follows the native side flags and reports push.updated_sides and push.state_complete. The latter means both sides have been observed since decoder reset; it does not certify every venue, price level, order, or exchange event. A known capture gap resets this state so an old side cannot be presented as complete alongside a new side. Trade IDs remain exact strings, with bounded per-symbol deduplication and explicit revised observations when a previously seen ID's fields change.

Gap and lifecycle records use event.type="heartbeat" and event.result.data.event="gap" or "stream_status". Read these records: they report missing local capture messages, queue overflow, decoding failures, native resubscription notices, disconnects, and reconnects. Queue overflow has a separate coalesced notice that cannot itself disappear when the data queue fills. status() exposes counters and the current capture epoch. Capture sequence numbers are generated by our observer; they are not exchange sequence numbers. No arithmetic on trade IDs is used to infer exchange loss.

stream.reconnect() re-establishes the subscription and can reattach to the same running Futu process after an instrumentation detach. PID and process-creation identity are checked before recovering retained handles. Other active streams are restored too. A restarted Futu process requires a new client. Native unsubscribe batching is allowed to settle before resubscription; these bounded checks inspect subscription bookkeeping only. Shared subscriptions owned by the app remain untouched. Futu does not promise a new bootstrap for an already shared subscription. There is no exchange replay or Last-Event-ID resumption, and lossless=False remains explicit.

Closing a stream wakes its readers before cleanup. Unsubscribe cleanup has a bounded wait so an unresponsive native call cannot prevent the provider from attempting detach; timed-out cleanup retains its handle provenance in the provider. A frozen desktop/instrumentation runtime can still require restarting the bridge. Native queue reads do not invoke upstream requests; the explicit polling mode retains its older upstream-call timeout limitations.

The October 25, 2026 actual audit captured AAPL stock and October 26 $347.50 call/put native quote/book images, then verified resubscription, instrumentation detach/reattach, intentional local callback loss, and bounded queue overflow. All eight available delivery/lifecycle checks passed with no polling calls and no remaining test-owned handles. The Sunday session produced no genuine trade-bit-35 pushes. Trade decoding, deduplication, and gap handling pass fixture tests, but open-market trade delivery and latency remain unverified. Evidence: coverage_audit/push_test/library_push_audit_settled.json.

## Hong Kong verification

The October 25 Tencent audit used the catalog-resolved HK.00700 identity and the same public Python API as US stocks. Actual downloads returned a quote with an October 23 exchange timestamp, four daily candles in the October 19–23 request window, 331 one-minute candles for October 23, ten bid levels and ten ask levels, 100 recent public prints, and 20 stock-news headlines. These are observed record counts, not a claim of complete session coverage.

Native quote and book callbacks arrived on the initial subscription, after resubscription, and after an actual instrumentation detach/reattach. All test-owned subscription handles were released. The Sunday observation window contained no new trade pushes; recent-trade downloads and newly arriving trades are separate checks. Reproduce with `python coverage_audit/audit_hk_native.py`; full responses and lifecycle evidence are in coverage_audit/push_test/hk_00700_audit.json. Hong Kong options remain experimental and are not covered by this Tencent stock audit.

## Optional HTTP API

```powershell
& D:\python\python.exe -m futu_bridge --provider futu_bridge.native:create_provider --port 8765
```

Only loopback binding is allowed. Leave the terminal open; Ctrl+C stops it. FUTU_BRIDGE_PID, FUTU_BRIDGE_CATALOG, FUTU_BRIDGE_STREAM_MODE (push/poll), FUTU_BRIDGE_PUSH_QUEUE_SIZE, and FUTU_BRIDGE_POLL_INTERVAL configure the provider. Optional --token-env FUTU_BRIDGE_TOKEN reads a local bearer token without printing it. Browser cross-origin and non-loopback Host requests are refused.

| GET route | Parameters |
|---|---|
| /health | Service and separate provider connection state |
| /v1/capabilities | Capabilities and limitations |
| /v1/instruments | query, limit=20 (maximum 100) |
| /v1/instruments/resolve | symbol |
| /v1/quotes | symbols=US.AAPL,US.ZNmain,US.BTCmain |
| /v1/bars | symbol, timeframe=1d, optional start, end, adjustment=none, session=provider_default |
| /v1/indicators | symbol, timeframe=1d, optional start, end, adjustment=none, indicators=MA,MACD,RSI, session=provider_default |
| /v1/options/chain | underlying=US.AAPL, optional expiry |
| /v1/orderbook | symbol, depth=10 |
| /v1/trades | symbol, limit=100 |
| /v1/news | symbol, limit=20, optional cursor, start, end |
| /v1/short-selling/daily | symbol, limit=20 (maximum 50), optional cursor, inclusive date start, end |
| /v1/earnings/calendar | symbol, optional inclusive YYYY-MM-DD start, end; maximum 93 dates |
| /v1/earnings/report | symbol, fiscal_period=2026Q2 |
| /v1/earnings/call | symbol, fiscal_period, include_transcript=false, language=page |
| /v1/reports/issuer | symbol=HK.00700, optional year; explicit issuer source |
| /v1/reports/announcement | notice_id; public-page access may be unavailable |
| /v1/stream | symbols, types=quotes,trades,orderbook (SSE) |
| /v1/samples/bars | Explicit offline sample, separate from actual feeds |

```powershell
Invoke-RestMethod 'http://127.0.0.1:8765/v1/quotes?symbols=US.AAPL,US.ZNmain,US.BTCmain'
Invoke-RestMethod 'http://127.0.0.1:8765/v1/bars?symbol=US.ZNmain&start=2026-10-01&end=2026-10-23'
```

## Repeat verification

The current 0.2.3 Python regression result is recorded in `coverage_audit/unit_suite_0.2.3.log`. Session tests include saved actual-response partitions, Python/HTTP routing, unknown-section rejection, overlap field conflicts, and explicit preservation of per-feed previous-close variants. Actual session downloads are recorded separately in `coverage_audit/session_debug/`; saved-response replays and synthetic negative cases are labeled as such.

The expanded hardening audit is in `coverage_audit/hardening/live/report.json`, with its runner `coverage_audit/audit_hardening_live.py`. It completed 278 actual desktop/HTTP and invariant checks across 18 base instruments and 14 options: 232 passed, 40 returned explicitly empty selections, and six were unavailable/unsupported. It covered US/Hong Kong stocks, ETFs, a cash index, Treasury/equity/metals futures, forex, Bitcoin futures/spot, a cash bond, seven option underlyings, news/short-volume pagination, inclusive bounds, weekends, future ranges, and quote/book delivery after reconnect and detach/reattach. No invalid decoded data or unexpected exceptions occurred. All test subscriptions were released. The Sunday observation still does not verify newly arriving market trade pushes.

The preserved 0.2.2 wheel returned only 170 SPCX minute bars for October 22 through date-only bounds, while an independent timestamp query returned 1,440. The corrected working source returned 1,440 for that date and 2,880 for October 22–23, with equivalent results for AAPL and MSFT. The initial comparison of two date-only queries falsely agreed because both shared the same defect; the new test uses independent transport bounds. See `coverage_audit/hardening/baseline_022.json`. This broad audit ran the corrected working source before the 0.2.3 version bump and final future-end metadata correction; its captured source hashes identify that state. Final regressions and extracted-wheel tests check the release itself.

SPY and QQQ options returned 405 native regular-section minute bars through 20:15 UTC, while the tested individual-stock options returned 390 regular bars. Saved-response regressions retain that distinction without imposing stock hours on options. Decoder tests include 300 deterministic malformed inputs, conflicting critical fields, int32 signing, precision/date/timestamp limits, and retained unknown wire extensions. Unknown fields 24/25 remain raw data with unverified semantics. Fault tests also cover truncated/oversized HTTP documents, atomic PDF publication races, partial native subscriptions, malformed HTTP origins, and cleanup failures.

The earlier 0.2.1 release recorded **311 Python tests** and **50 JavaScript VM checks** across native push, news, calendar, report-link and short-volume observers. Those counts describe that earlier release. VM stubs verify filtering and failure paths; they are separate from actual desktop/API/download evidence. `coverage_audit/verify_wheel.py` compares packaged sources/resources with the workspace and imports the wheel in isolation. The release-wheel smoke runner checks quotes, news, short volume, calendars, historical report/call lookups, indicators, temporary HTTP routes, and native callbacks with cleanup; inspect its versioned evidence for the release actually tested.

The independent third-stock audit uses US.MSFT (stock ID 201345), a dynamically discovered October 26 $525 call and put, and both direct Python and HTTP APIs. Downloads returned 7,500 daily stock bars from January 2, 1997 through October 23, 2026; 390 stock minute bars; 520 minute bars for each option; 100 recent prints per instrument; two news pages without duplicate IDs; and two 50-record short-volume pages without overlapping dates. July/October calendar lookups returned matching MSFT events. Fiscal label 2026Q4 resolved to URL period 2026Q2; the call page returned two recordings, four subtitle links, and 181 AI transcript paragraphs.

Native quotes and books arrived for all three instruments on initial subscription, resubscription, and capture detach/reattach. Polling and HTTP SSE delivery were tested separately. Queue overflow was reported; dropping callback 1 and receiving callback 2 produced a missing-message notice. The original test also preserved an unobservable trailing-loss case: discarding the final callback produces no sequence gap until a later message arrives. New trade pushes remain pending in the Sunday session. The earlier MSFT audit observed an annual-report attachment on SEC that returned HTTP 403 to an anonymous request. The current helper accepts that strict public SEC route and classifies remote rejection separately from invalid URLs; a successful MSFT PDF download is not established by that earlier audit. See coverage_audit/third_stock_msft/report.json, followup.json, and validation.json, with audit runners coverage_audit/audit_third_stock.py and audit_third_stock_followup.py.

The additional SPCX audit downloaded 93 daily stock candles from June 12 through October 23, 2026 and 1,440 one-minute stock candles with `session="all"`. Its October 26 $147 call and put each returned 390 regular-section candles, preserving raw section 21. Actual explicit option `premarket`, `postmarket`, and `overnight` selections were empty, while `regular`, `extended`, and `all` retained the same 390 native bars. These counts describe the tested windows and contracts, without a history-completeness guarantee. The original mapping errors are preserved separately from the fixed checks in `coverage_audit/spcx/initial_report.json` and `fixed_sessions_live.json`. Historical/upcoming calendars each returned one event; a fresh Q3 Futu call lookup returned `available=False` and no recordings, so the separately downloaded issuer transcript PDF is not counted as a successful Futu call result. Evidence: `coverage_audit/spcx/followup_information.json`.

```powershell
# Captured-payload regressions, synthetic edge cases, Python/local-HTTP contracts.
& D:\python\python.exe -m unittest discover -s futu_bridge/tests -v

# Actual desktop/HTTP audit; Futu must be running.
& D:\python\python.exe -m futu_bridge.full_audit --start 2026-10-19 --end 2026-10-23

# Actual US stock/option session selections; retains empty responses explicitly.
& D:\python\python.exe coverage_audit/audit_session_public.py
& D:\python\python.exe coverage_audit/audit_session_remaining.py

# Actual push, reconnect, callback-drop, and overflow audit.
& D:\python\python.exe -m futu_bridge.push_audit --seconds 20

# During a regular US market session: require new native trade pushes for
# AAPL and the dynamically selected nearby call and put; absence exits nonzero.
& D:\python\python.exe -m futu_bridge.push_audit --seconds 60 --require-trades

# During a Hong Kong market session: require newly timestamped Tencent trades.
& D:\python\python.exe coverage_audit/audit_hk_native.py --seconds 60 --require-trades --output coverage_audit/push_test/hk_00700_market_hours.json
```

The full audit starts a temporary loopback server, requests four feeds for every universe instrument plus equities/ETF/index/delisted examples, discovers actual AAPL options and tests a nearby call and put, observes SSE, checks catalog/ticker identities, and downloads Python history/timeframe/adjustment cases. It saves actual responses, then closes the server and provider. No mock or bundled sample substitutes for a successful data download.

The separate offline sample requires the decoded sample file from this checkout or a supplied sample_path. The wheel does not bundle it; no actual feed falls back to it.

