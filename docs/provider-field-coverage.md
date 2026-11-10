# Fixed-provider field coverage and Parquet contracts

The common store now accepts ten additional typed observation families through
`futu_bridge/joint/storage/provider_records.py`. All local providers, their endpoints,
the shared `.venv-joint`, and the existing keyless Polymarket source selection remain unchanged.
This module performs no network calls and has no order-routing code. The acceptance acquisition
used read-only requests against the already running `http://127.0.0.1:8765` adapter.

Typed coverage means named Arrow columns with validated identities, units and time semantics.
Raw coverage means complete supplied JSON can be archived unchanged; it does **not** mean every
nested field has an analytical contract or that every upstream endpoint has been tested live.
Fields not returned by a provider are not synthesized. A provider's advertised route is not a
guarantee of entitlement, availability, accuracy, or complete history.

## API and storage semantics

```python
from pathlib import Path
from futu_bridge.joint.storage import SnapshotStore
from futu_bridge.joint.storage.incremental import as_of
from futu_bridge.joint.storage.provider_records import FAMILIES, ingest_provider, provider_table

store = SnapshotStore(Path("output/common_provider_store"))
snapshot = ingest_provider(
    store,
    Path("output/joint/provider_fields_capture_20261110/news_hk.json"),
    "news",
    "news",
    expected_head=store.head("news"),
)
known_news = as_of(store, snapshot.identity, cutoff_ns=1794292521296441000)
```

`provider_table(payload: bytes, family: str)` is a pure converter.
`ingest_provider(store, source: Path, dataset, family, *, expected_head=None)` archives the exact
input bytes, then merges immutable Parquet revisions using the common store's compare-and-swap head.
`FAMILIES` is the public family list for CLI integration. `inventory(repo: Path)` enumerates the
pinned provider methods and all 98 catalogued Polymarket REST operations; it records source hashes.
`run(captures: Path, output: Path, repo: Path)` performs the real-capture acceptance and writes a report.
The output directory must be new so prior evidence is never overwritten.

Every row contains `record_id`, `provider`, `symbol`, `source_observed_ns`, `response_served_ns`,
`observed_ns`, `source_sha256`, `payload_json`, and `quality_flags`. All three time columns are UTC
nanoseconds. `observed_ns = max(source_observed_ns, response_served_ns)`: a multi-request public
earnings response cannot become available before the final response completed. The captured call
took more than seven seconds after its initial source timestamp. This is observed receipt time,
**not** a claim about when an older document or data point first became publicly available.

Publication times, scheduled dates, meeting times and option expiries have separate fields.
A future earnings schedule or future-expiry option can therefore be known today without being
hidden by an event-time cutoff. News and historical documents are never made available in a
backtest merely because their publication or fiscal date precedes the capture.

Exact numeric identifiers must be strings; numeric IDs are rejected. JSON fractional numbers
are parsed as `Decimal`, and normalized payloads retain every delivered decimal digit. Strikes use
`decimal128(38,9)` with the explicitly validated native `1e9` scale. Ratios and indicative probabilities
use `decimal256(76,38)` and reject values that cannot fit exactly. Shares are checked int64 values.
These preserve delivered precision; they cannot repair precision already lost upstream.

Raw archives preserve original bytes, including envelope extensions, unknown fields and expiring
public media links. Normalized `payload_json` is canonical semantic JSON, so whitespace/key-order
changes do not create conflicting versions. It includes the source record and mapped typed values.
Different values for the same business identity at the same observation time fail closed; a later
observation creates another version. Empty responses may be stored but do not certify data coverage.

These are histories of observations, not authoritative membership or deletion feeds. A record absent
from a later limited page is not a cancellation, delisting, withdrawn article or removed paragraph.
For a complete document version, pin its response archive or select paragraphs belonging to that
capture's `source_sha256`, then order by `ordinal`. Generic `as_of` chooses the latest known version
per record; it does not invent deletions for absent records. Physical Parquet order is by identity,
not the original feed order; paragraph `ordinal` preserves the original sequence.

## Real data accepted

Evidence is in `output/joint/provider_fields_verified_20261110/report.json`; the local ignored tests
are `futu_bridge/tests/test_parquet_provider_records.py`. The report verifies 528 typed rows,
identical raw archive bytes, Parquet type/value round trips, duplicate import idempotence, and no
rows visible one nanosecond before their actual availability. Seventeen local tests additionally
exercise malformed identity/scale rejection, unknown fields, revisions, stale publishers, nullable
status, arbitrary outcome counts and precision beyond Python's default Decimal context.

| Family | Actual capture | Rows | Observed coverage |
|---|---|---:|---|
| `news` | `news_hk.json`, HK.00700 | 5 | Recent Chinese headlines, publisher, language, links and publication time |
| `option_contracts` | `options_us.json`, US.AAPL | 96 | Current 2026-11-11 expiry, native contract identity and strikes |
| `earnings_calendar` | `calendar_hk.json`, HK.00700 | 1 | Native 2026Q3 schedule dated 2026-11-18 |
| `earnings_report_links` | `report_hk.json`, HK.00700 | 1 | Requested 2026Q2 earnings-hub page metadata |
| `short_volume` | `short_hk.json`, HK.00700 | 5 | Daily share volumes and ratios, 2026-11-04 through 2026-11-10 |
| `instruments` | `instrument_hk.json` and `search_hk.json` | 1 + 5 | Tencent equity resolution plus native catalog search matches |
| `issuer_documents` | `issuer_hk.json`, HK.00700 | 2 | Tencent 2025 annual/interim PDF link metadata |
| `earnings_calls` | `call_hk.json`, HK.00700 | 1 | Meeting and public-page metadata labeled 2026Q2 |
| `transcript_paragraphs` | Same call capture | 155 | Public-page paragraph text, original order and source quality flags |
| `polymarket_outcomes` | `event.json` | 256 | 128 markets in “Presidential Election Winner 2028” |

Futu/document captures were acquired on **2026-11-10, 06:35 UTC**, using the fixed local Futu
16.37.27308 adapter. `event.json` is an unchanged copy of the genuine existing
`output/joint/verified_runtime_20261109/input/event.json`, observed on **2026-11-05, 04:08:59 UTC**.
Its outcome prices are observations at that time, not current executable prices. The report pins
every capture hash, every published snapshot ID and the converter implementation hash.

The public earnings page is explicitly unverified: its metadata is labeled 2026Q2, while the
first substantive transcript paragraph mentions 2024. The converter preserves the exact text,
`issuer_verified=False`, and `content_period_identity_unverified`. It does not certify that the
text belongs to the requested fiscal period, is an issuer transcript, or is complete. This content
must not be treated as verified financial actuals or used as a historical point-in-time feature.

## Complete fixed-provider method inventory

| Provider method / route | Typed analytical coverage | Archived-only fields and limits |
|---|---|---|
| `quotes` / `/v1/quotes` | Existing stream observation identity/receipt metadata | Quote body retained in stream JSON; dedicated quote/fundamental columns are not implemented |
| `bars` / `/v1/bars` | Existing complete, unadjusted daily/minute OHLCV and source labels | Turnover is raw; adjusted/other-timeframe importer contracts are not implemented; historical release time unknown |
| `option_chain` / `/v1/options/chain` | Current native option contract identity, side, strike, expiry date | Expiry discovery and native integer fields retained; multiplier/share scales unverified; Greeks, IV, prices and expired chains not supplied by this endpoint |
| `orderbook` / `/v1/orderbook` | Existing SSE converter types observed price levels, size and optional order count | Direct REST envelope converter not implemented; may be top-of-book; no exchange order identities |
| `trades` / `/v1/trades` | Existing SSE converter types exact trade IDs, prices, sizes and exchange timestamps | Direct REST envelope converter not implemented; native recent page is bounded and incomplete historically |
| `stream` / `/v1/stream` | Existing durable journal plus typed books/trades | Quotes/status/extensions retained; native queue is bounded, no exchange sequence or losslessness guarantee |
| `search_symbols` / `/v1/instruments` | Native instrument IDs, symbols, exchange, name and untranslated type/currency enums | Catalog presence does not establish quote entitlement, tradability or point-in-time historical membership |
| `resolve_symbol` / `/v1/instruments/resolve` | Same instrument schema | No guessed mapping from numeric currency/type enums; no historical security-master availability |
| `stock_news` / `/v1/news` | News ID, stock ID, headline, publisher, language, URL and UTC publication ns | Opaque pagination cursor/coverage retained in archive; bounded headlines, no article-body guarantee or push feed |
| `earnings_calendar` / `/v1/earnings/calendar` | Provider-local scheduled date, fiscal label, raw period/year, raw timestamp, nullable confirmed/estimated | Native bounded date/page selection; timestamp meaning unverified; this is not an exchange trading calendar |
| `earnings_report_link` / `/v1/earnings/report` | Exact report/stock ID, fiscal period, URL/content type, nullable PDF availability | Earnings-hub page metadata; no numerical actuals/estimates or verified original PDF content |
| `earnings_call` / `/v1/earnings/call` | Meeting/feed/live IDs, times, page flags, recording/subtitle counts, paragraph text | Media/subtitle URLs retained; may expire; source transcript authenticity, period identity and completeness unverified |
| `technical_indicators` / `/v1/indicators` | No additional indicator-column contract in this module | Fixed implementation computes from returned bars; JSON may be archived; prehistory loading and native-chart parity are not guaranteed |
| `daily_short_volume` / `/v1/short-selling/daily` | Share quantities, exact delivered ratios and percentages, source date/timestamp | Bounded page; today's record may be partial; not outstanding short interest, lending balances or borrow availability |

`capabilities()` and `close()` are control methods, not missing analytical data families.
Additional fixed public document helpers expose `/v1/reports/issuer` (typed link metadata here)
and `/v1/reports/announcement` (raw-only; no genuine announcement capture accepted in this run).
Neither helper establishes an archived binary PDF body. A separate verified document download
must be archived and linked by content hash before claiming PDF-content coverage.

## Field units and unresolved interpretations

| Family | Typed fields beyond shared provenance | Unit and interpretation limits |
|---|---|---|
| News | `news_id`, `stock_id`, `headline`, `published_ns`, `url`, `publisher`, `language` | Exact string IDs; UTF-8 text; source Unix seconds checked against timezone-explicit timestamp |
| Instruments | `stock_id`, `code`, `futu_symbol`, `name`, `exchange`, `listed_exchange`, numeric `currency_code`, `market_code`, `instrument_type_v2`, `sub_instrument_type_v2`, `price_accuracy`, `delisting_flag` | Original numeric enums only; `price_accuracy` is not an independently verified minimum tick |
| Option contracts | `stock_id`, `underlying`, `underlying_stock_id`, `side`, `strike`, `strike_raw`, `strike_scale`, `expiry_date`, `identity_status`, `multiplier_raw`, `contract_share_size_raw`, `timezone`, `exchange`, `is_delisted` | Strike = native integer / 1e9; price currency is not supplied explicitly; unknown multiplier/share scales remain strings; expiry is an ISO date, not a verified exercise instant |
| Earnings calendar | `stock_id`, `scheduled_date`, `fiscal_period`, `fiscal_year`, `fiscal_period_raw`, `provider_timestamp_ns`, `timestamp_meaning`, `market_timezone`, `confirmed`, `estimated` | Date-only provider schedule; nullable booleans retained; raw timestamp is not silently treated as earnings release time |
| Report links | `stock_id`, `report_id`, `fiscal_period`, `url`, `content_type`, `pdf_available` | Large report IDs remain strings; no inferred statement metrics |
| Issuer documents | `title`, `url`, `year`, `period_type`, `format`, `verified_download` | Actual capture has `verified_download=False`; link format is not a verified downloaded MIME type |
| Earnings calls | `stock_id`, `fiscal_period`, `feed_id`, `live_id`, `title`, `url`, `meeting_start_ns`, `page_published_ns`, `status_code`, `transcript_available`, `issuer_verified`, `ai_content_unverified`, `recording_count`, `subtitle_count` | Source timestamp units converted exactly; numeric status untranslated; fiscal label does not verify content identity |
| Transcript paragraphs | `feed_id`, `fiscal_period`, `ordinal`, `text`, `language`, `format`, `issuer_verified`, `machine_generated_or_translated`, `timestamped`, `completeness_verified` | Zero-based original paragraph order; no invented paragraph time or speaker labels |
| Short volume | `stock_id`, `trading_date`, `provider_timestamp_ns`, `short_volume`, `total_volume`, `short_volume_ratio`, `short_volume_ratio_percent`, `ratio_source`, `quantity_unit` | Share counts; fraction vs percentage points remain distinct; source ratio is already computed, exact numerator/denominator retained |
| Polymarket outcomes | `event_id`, `market_id`, `condition_id`, `token_id`, `outcome_index`, `outcome`, `question`, `indicative_probability`, `active`, `closed`, `resolved`, `resolution_status`, `resolution_source` | Token IDs stay exact decimal strings; arbitrary outcome counts supported; missing prices stay null; closed/proposed status or 0/1 prices do not imply final resolution or a winner |

Polymarket event categories, volume, liquidity, popularity and complete resolution metadata remain
inside the observed JSON, available only at the capture time. They are not backdated from event
creation, scheduled end time or a subsequently observed resolution. CLOB best bid/ask and indicative
Gamma outcome prices remain distinct data concepts.

## Concrete remaining gaps and blockers

- **Engineering gaps with existing data:** dedicated typed REST quotes/indicator records, REST trade/book
  envelopes, most Polymarket wallet/activity/on-chain fields, CLOB/RTDS deltas, and typed categorical/popularity
  metrics still need separately validated contracts. Their public availability does not make them typed here.
- **Provider limitations:** no authoritative exchange trading-calendar endpoint, historical security-master
  membership, verified original availability for retrospective data, expired-option discovery, or native
  option Greeks/IV/prices in the captured contract endpoint. These cannot be supplied faithfully by guessing.
- **Units needing verification:** native numeric currency/type enums and option multiplier/contract-share-size
  integer scales. Raw values are available, but are deliberately not passed as resolved units to a simulator.
- **Document quality:** no original issuer transcript, independently verified earnings financial actuals,
  downloaded PDF body or period-authentic transcript was established by these captures.
- **History/completeness:** bounded pages and snapshots are not complete historical archives. Repeated
  collection supplies future observations; it cannot recreate receipt times or recover uncaptured deltas.
- **Real acceptance scope:** only the listed actual captures are accepted. The inventory below does not
  claim live testing of all 98 upstream operations; endpoint availability can change independently.

## Pinned Polymarket REST inventory

Generated from the repository's fixed `polymarket_endpoints.json`, SHA-256
`1a089d6fde5f59941a886f05db9d7655fe7b64fcac0d9cc60d2e6294eafedc8a`.
The fixed provider declaration SHA-256 is
`a18a9fe5bd9c70b53a1bda75fba848af783f809301a6502a61303d39b2007d1a`.
The catalog's POST operations below are public batch **reads**; no order writes are exposed.

Coverage codes: **E** = event adapter identity/text plus nested typed outcome observations, with
remaining event metadata in JSON; **H** = existing historical token/time/probability adapter,
historical availability unknown; **R** = raw JSON only, no typed field contract in the common store.
The E converter accepts the fixed provider's `{events: [...]}` envelope; endpoint shapes differing
from that envelope require an explicit shape adapter. All route entries are inventory, not
independent live-availability certifications.

<!-- BEGIN GENERATED ENDPOINT INVENTORY -->

| API | Method | Path | Coverage |
|---|---|---|---|
| gamma | GET | `/status` | R |
| gamma | GET | `/teams` | R |
| gamma | GET | `/teams/{id}` | R |
| gamma | GET | `/tags` | R |
| gamma | GET | `/tags/{id}` | R |
| gamma | GET | `/tags/slug/{slug}` | R |
| gamma | GET | `/tags/{id}/related-tags` | R |
| gamma | GET | `/tags/slug/{slug}/related-tags` | R |
| gamma | GET | `/tags/{id}/related-tags/tags` | R |
| gamma | GET | `/tags/slug/{slug}/related-tags/tags` | R |
| gamma | GET | `/events` | E |
| gamma | GET | `/events/pagination` | E |
| gamma | GET | `/events/results` | R |
| gamma | GET | `/events/{id}` | R |
| gamma | GET | `/events/{id}/tweet-count` | R |
| gamma | GET | `/events/{id}/comments/count` | R |
| gamma | GET | `/events/{id}/tags` | R |
| gamma | GET | `/events/slug/{slug}` | R |
| gamma | GET | `/events/creators` | R |
| gamma | GET | `/events/creators/{id}` | R |
| gamma | GET | `/markets` | R |
| gamma | GET | `/markets/{id}` | R |
| gamma | GET | `/markets/{id}/description` | R |
| gamma | GET | `/markets/{id}/tags` | R |
| gamma | GET | `/markets/slug/{slug}` | R |
| gamma | POST | `/markets/information` | R |
| gamma | POST | `/markets/abridged` | R |
| gamma | GET | `/markets/keyset` | R |
| gamma | GET | `/events/keyset` | E |
| gamma | GET | `/series` | R |
| gamma | GET | `/series/{id}` | R |
| gamma | GET | `/series/{id}/comments/count` | R |
| gamma | GET | `/series-summary/{id}` | R |
| gamma | GET | `/series-summary/slug/{slug}` | R |
| gamma | GET | `/comments` | R |
| gamma | GET | `/comments/{id}` | R |
| gamma | GET | `/comments/user_address/{user_address}` | R |
| gamma | GET | `/public-profile` | R |
| gamma | GET | `/profiles/user_address/{user_address}` | R |
| gamma | GET | `/sports` | R |
| gamma | GET | `/sports/market-types` | R |
| gamma | GET | `/public-search` | R |
| clob | GET | `/time` | R |
| clob | GET | `/midpoint` | R |
| clob | GET | `/midpoints` | R |
| clob | POST | `/midpoints` | R |
| clob | GET | `/spread` | R |
| clob | POST | `/spreads` | R |
| clob | GET | `/last-trade-price` | R |
| clob | GET | `/last-trades-prices` | R |
| clob | POST | `/last-trades-prices` | R |
| clob | GET | `/fee-rate` | R |
| clob | GET | `/fee-rate/{token_id}` | R |
| clob | GET | `/tick-size` | R |
| clob | GET | `/tick-size/{token_id}` | R |
| clob | GET | `/neg-risk` | R |
| clob | GET | `/neg-risk/{token_id}` | R |
| clob | GET | `/price` | R |
| clob | GET | `/prices` | R |
| clob | POST | `/prices` | R |
| clob | GET | `/book` | R |
| clob | GET | `/books` | R |
| clob | POST | `/books` | R |
| clob | GET | `/simplified-markets` | R |
| clob | GET | `/sampling-markets` | R |
| clob | GET | `/sampling-simplified-markets` | R |
| clob | GET | `/clob-markets/{condition_id}` | R |
| clob | GET | `/markets-by-token/{token_id}` | R |
| clob | POST | `/markets/live-activity` | R |
| clob | GET | `/markets/live-activity/{condition_id}` | R |
| clob | GET | `/prices-history` | R |
| clob | POST | `/batch-prices-history` | R |
| clob | GET | `/rewards/markets/current` | R |
| clob | GET | `/rewards/markets/{condition_id}` | R |
| clob | GET | `/rewards/markets/multi` | R |
| clob | GET | `/rebates/current` | R |
| clob | GET | `/builder/trades` | R |
| data | GET | `/v2/activity` | R |
| data | GET | `/v2/activity/combos` | R |
| data | GET | `/v2/approvals` | R |
| data | GET | `/v2/biggest-winners` | R |
| data | GET | `/v2/builders/leaderboard` | R |
| data | GET | `/v2/builders/volume` | R |
| data | GET | `/v2/holders` | R |
| data | GET | `/v2/leaderboard` | R |
| data | GET | `/v2/live-volume` | R |
| data | GET | `/v2/oi` | R |
| data | GET | `/v2/positions` | R |
| data | GET | `/v2/positions/combos` | R |
| data | GET | `/v2/prices-history` | H |
| data | GET | `/v2/resolutions` | R |
| data | GET | `/v2/status` | R |
| data | GET | `/v2/tokens` | R |
| data | GET | `/v2/trades` | R |
| data | GET | `/v2/user-pnl` | R |
| data | GET | `/v2/user-stats` | R |
| data | GET | `/v2/user-volume` | R |
| data | GET | `/v2/value` | R |

<!-- END GENERATED ENDPOINT INVENTORY -->

WebSocket market, RTDS and sports channels remain raw-only. The existing US-equity relevance filter excludes sports from equity selection; it does not remove the public sports API. Polygon public RPC status/logs/balance/receipt remain raw-only, preserving exact integer strings and block context. Goldsky remains removed; no key-required or paid replacement source was introduced.
