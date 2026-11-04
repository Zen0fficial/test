# Polymarket events relevant to US equities

The filter runs in the requested order: popularity, Polymarket categories,
then DeepSeek. Polymarket and the existing dRPC chain backup remain the only
market-data providers. Those data reads are free and keyless. DeepSeek is a
separate, optional classifier using your own API key and token billing.

## Selection rules

1. Fetch open, active, non-archived events from Gamma in descending 24-hour
   volume order. Deduplicate and keep the top 500 by default. Liquidity breaks
   ties and low liquidity is flagged. No sports/category exclusion happens
   before this popularity shortlist, so excluded events are not backfilled
   with less popular events.
2. Check official tags, categories, sports primary tags and sports series.
   Structured sports contests are excluded. Narrow title checks catch missing
   league tags. Entertainment categories are also excluded; corporate earnings,
   acquisition and regulatory events can continue when business tags provide
   context. Mixed categories and commercial release/delay events (for example,
   a video-game launch tagged pop culture) continue to the LLM, as do missing
   or unfamiliar categories. A generic politics tag on celebrity trivia does
   not by itself bypass the entertainment exclusion.
3. Send only category survivors to DeepSeek in batches of four. Require an
   include/exclude/review decision, importance score, affected assets/sectors,
   conditional economic mechanism, confidence, and literal evidence from the
   supplied event text. Unknown tickers, missing events, malformed output or
   invented evidence fail validation. Failed batches and low-confidence
   inclusions remain in review.

Importance is a 0–100 relevance assessment, not a winning probability or a
forecast of stock returns. A valid quote supports the event facts; the economic
mechanism remains the model's inference. The default asset list is a small,
editable research universe, not a complete US security master. The model can
use sectors for companies outside that list.

## Configure the key

Set `DEEPSEEK_API_KEY` in the environment of the process that starts the bridge
or CLI. Restart an already running bridge after changing its environment.
For an interactive PowerShell session, a masked prompt avoids placing the key
in command history:

```powershell
$secret = Read-Host 'DeepSeek API key' -AsSecureString
$credential = [System.Net.NetworkCredential]::new('', $secret)
$env:DEEPSEEK_API_KEY = $credential.Password
Remove-Variable secret, credential
.\.venv-qlib\Scripts\python.exe -m futu_bridge --provider futu_bridge.native:create_provider --port 8765
```

The repository's `.env.example` is a placeholder template; `.env` files are
**not loaded automatically**. The key is never returned in API responses or
saved in classifier results. An absent or placeholder key produces zero model
requests and leaves survivors with reason `awaiting_api_key`. Existing valid
cached judgments can still be read and reused.

## Run and inspect

```powershell
# Explicit refresh; HTTP returns 202 with a job id. May use your DeepSeek API.
$job = Invoke-RestMethod -Method Post -ContentType 'application/json' `
  -Body '{"top_n":500,"analyze":true}' `
  'http://127.0.0.1:8765/v1/polymarket/equity-events/refresh'
Invoke-RestMethod 'http://127.0.0.1:8765/v1/polymarket/equity-events/status'

# These GETs only read the saved report; they never call the model.
Invoke-RestMethod 'http://127.0.0.1:8765/v1/polymarket/equity-events?decision=include&limit=50'
Invoke-RestMethod 'http://127.0.0.1:8765/v1/polymarket/equity-events?decision=review&limit=50'
Invoke-RestMethod 'http://127.0.0.1:8765/v1/polymarket/equity-events?decision=exclude&limit=50'

# Synchronous CLI refresh, followed by results. Use --offset to paginate.
polymarket-data equity-events --refresh --top-n 500 --decision all --limit 500

# Free discovery/category preview, explicitly disabling all model judgments.
polymarket-data equity-events --refresh --no-llm --decision review

# Read the saved report without another refresh.
polymarket-data equity-events --decision include
```

Use `python -m futu_bridge.polymarket_cli` if `polymarket-data` is not on PATH.
Before the first successful refresh, results return HTTP 503 `filter_not_ready`.
Only one refresh per filter instance can run at a time; another request returns
HTTP 409 `filter_busy`.

```python
from futu_bridge import PolymarketClient

with PolymarketClient() as pm:
    report = pm.refresh_equity_events(top_n=500)  # Explicit discovery + classifier
    selected = pm.equity_events(limit=50).data
    print(selected["counts"], selected["events"])
    token_ids = selected["selected_token_ids"]
    # Select the outcome tokens you want to follow before opening a market stream.
```

Each event retains its popularity rank, volume, liquidity, category decision,
final reason, quality flags, model judgment and constituent market outcomes.
`selected_token_ids` includes outcome IDs from all included open markets,
independent of result pagination. It is available for the existing CLOB book
and stream interfaces; the filter does not start subscriptions automatically.

## Probabilities, freshness and caching

Outcome probabilities come from Gamma metadata and remain separate from LLM
judgments. Zero prices and non-binary outcomes are preserved. These values are
indicative metadata, not executable winning odds. Use CLOB bid/ask, books or
streams for current trading prices. Event and market update timestamps are
retained alongside the report observation time.

Refreshes are explicit, not scheduled. Reports or discovery observations older
than 15 minutes are marked `stale`, including when classification itself takes
that long. The discovery manifest records page sources, deduplication and whether
the requested shortlist was filled. Paginated discovery is not an atomic
market-wide snapshot. A failed refresh preserves the previous report and
exposes the failure through the status endpoint.

Validated judgments are cached for 24 hours using event text, taxonomy, config
and prompt version. Price/volume-only changes reuse them; content changes
invalidate them. `--no-llm` ignores even cached judgments for that run. Sports
taxonomy has a separate 24-hour fallback cache. If taxonomy cannot be verified,
ambiguous events cannot become unqualified inclusions.

Results live under `output/polymarket/equity_filter` relative to the working
directory. Use one writer process per cache directory. CLI and HTTP can share
saved results, but their in-memory job locks are independent.

## Tune the filter

Copy `futu_bridge/equity_filter_config.json` and edit the desired fields. Point
`POLYMARKET_EQUITY_CONFIG` at the copy before starting the bridge, or pass
`--config <path>` to the CLI. Overrides merge with the packaged defaults.
Unknown fields and invalid bounds fail explicitly.

| Setting | Default | Purpose |
| --- | --- | --- |
| `top_n` | 500 | Popularity shortlist; allowed 1–2000 |
| `max_pages` | 10 | Discovery bound, 100 events per page |
| `min_volume_24h` | 0 | Optional minimum volume before taking the shortlist |
| `low_liquidity_threshold` | 1000 | Quality flag only |
| `model` | `deepseek-v4-pro` | DeepSeek Pro classifier |
| `reasoning_effort` | `max` | Highest supported reasoning setting; thinking explicitly enabled |
| `max_output_tokens` | 65536 | Output allowance including reasoning; never reduced during retries |
| `request_timeout_seconds` | 300 | Time allowed for a model response |
| `retry_attempts` | 3 | Total attempts per failed batch, including the initial call |
| `retry_base_delay_seconds` | 2 | Exponential retry backoff: 2 seconds, then 4 seconds |
| `batch_size` | 4 | Events per request; allowed 1–8 |
| `max_llm_events` | 500 | Bound on uncached events sent per refresh |
| `cache_ttl_hours` | 24 | Reuse period for validated judgments |
| `candidate_categories`, `excluded_categories` | Packaged lists | Polymarket label/slug rules |
| `asset_universe` | Packaged symbol-to-name map | Allowed symbols in model results |

`POLYMARKET_EQUITY_CACHE_DIR` (CLI: `--cache-dir`) selects a persistent result
directory. Reload the filter process after environment/config changes, then
refresh to apply them to saved results. The HTTP refresh body accepts only
`top_n` and `analyze`; it cannot change credentials, model or arbitrary paths.

Connection failures, HTTP 408/409/425/429/5xx, invalid JSON and invalid evidence
are retried up to the configured attempt limit. Backoff honors `Retry-After`
up to 300 seconds; a longer cooldown stops retries for that batch. Authentication,
insufficient balance and other permanent HTTP 4xx errors are not retried.
An exhausted batch remains in review. Three consecutive exhausted transport
batches, or an exhausted HTTP 4xx response, stop further batches for the run.

Retries can incur additional DeepSeek charges. The report records attempted
calls, retries, sanitized attempt errors, successful batches, cache hits and
reported token usage. Failed/unparseable responses may still be billed by the
provider even when their usage cannot be recovered. No fixed cost is promised.

The classifier uses `deepseek-v4-pro`, thinking enabled, and `max` effort.
Retries retain the same model and reasoning settings. Flash/lower-effort caches
cannot supply Pro/max selections. After configuration changes, reading an old
report returns HTTP 409 `filter_config_changed` until a new refresh completes.
The existing research-text extractor keeps its previous model default.
See [DeepSeek's model catalog](https://api-docs.deepseek.com/quick_start/pricing/)
and [effort controls](https://api-docs.deepseek.com/guides/thinking_mode/).
