# Native order coverage and process recovery

Scope: NautilusTrader 1.231.0 and LEAN commit
`49b4aa7022abd38e99e9c7d3ec67929fa7f6d7b8`, shared `.venv-joint`, 2026-11-10.
These are bounded capability-family checks, not certification of every possible
order-type/TIF/trigger/latency combination or live brokerage behavior.

## Implemented additions

`nautilus_order_extensions.run(response, output, repo)` exercises 15 native
PARTIAL OTO scenarios on the five real HK.00700 holdout sessions. Native children
are accepted after the first 100-share fill, grow after the second parent fill,
and remain native reduce-only OUO siblings. Tests cover immediate full-bracket
cancellation and delayed take-profit amendments as well as ordinary growth.
Every fill, effective time, commission and cash balance is checked against an
independent Decimal/source-price oracle.

The public native bracket factory initializes each child at the full parent
quantity. The pinned cash engine checks that quantity before its native resize
pass, rejecting a 200-share child when only 100 shares have filled. The converter
uses public initialization-event serialization to initialize children at the
declared minimum fill lot. Native OTO activation, quantity updates, sibling
cancellation and matching remain unchanged. This is faithful for the explicit
100-share integral-lot liquidity model; it is not a universal fractional-fill
fix. Whole-bracket cancellation cancels protection too: the report explicitly
shows any residual unprotected holding. It never calls that residual protected.

`nautilus_order_matrix.run(response, output, repo)` adds 50 real-data cases:
market-to-limit, market-if-touched, limit-if-touched, trailing-stop-limit,
post-only rejection and unsupported native auction-TIF rejection. Native fills
and passive trigger times must equal a source-derived oracle before fee/account
reconciliation can pass. In this matrix the touched orders and trailing-limit
orders exercise both buy and funded sell directions.

`lean_order_extensions.run(response, output, repo)` adds 60 native trailing-stop
and limit-if-touched tickets, including latched triggers, unfilled cancellation
and 200-share touched-limit partial fills, percentage offsets and partially filled
trailing market remainders. The new C# converter subclasses the
existing offline account setup without changing the legacy lifecycle cases.
It retains native `FillModel.TrailingStopFill` and `LimitIfTouchedFill`, their
mutable trailing stop and touched latch, and native ticket/account processing.
The converter selects actual simulated BBO through the public `GetPrices` hook
because LEAN's generic last-tick cache can otherwise select the required
one-share placeholder trade. A finite-capacity wrapper enforces 100 shares per
side/observation, cumulative actual-fill costs and executable-side limit checks.
Native trailing first-trigger behavior is retained. Because upstream assumes its
candidate fills completely, a converter latch keeps a triggered, partially
filled trailing order's remainder executable as a market order even if price
reverses through the old stop. This faithful finite-liquidity adaptation is
checked on real reversal cases and does not retrigger the remaining quantity.

`nautilus_trigger_variants.run(raw, output, repo)` exercises 36 source-selective
conditions using **95 real BBO observations and 21 real, non-bootstrap trades**
from the existing live capture. All 36 produce independently verified native
simulated executions. Sources are LAST_PRICE, LAST_OR_BID_ASK, BID_ASK,
DOUBLE_LAST, DOUBLE_BID_ASK and MID_POINT; offsets are price, basis points and
ticks, in both funded directions. The trigger controller is explicitly
joint-owned; native matching, commissions and cash accounting execute its
resulting market tickets. Double confirmation means two consecutive fresh
observations of the relevant source, never repeated stale last prices on quote
callbacks. Bootstrap/backfill trades are excluded and ambiguous revisions fail.

An additional 18 cases compare the upstream native trailing calculator's
LAST_PRICE, LAST_OR_BID_ASK and BID_ASK stop updates with independent offset
arithmetic. Distant stops avoid executions in this arithmetic check. Native
`trade_execution=False` preserves actual BBO, updates last on a trade, and runs
native trailing maintenance at the next quote. This is separate from the joint
controller's explicitly event-driven trigger policy. The genuine capture has
constant BBO; quote-only confirmation cases use initially eligible causal
thresholds, rather than inventing a price excursion. These checks do not claim
a lossless market feed, exchange sequence, or a historical individual-order queue.

The source is the frozen real Futu minute capture, SHA256
`7e35cdb971bfcfb1f5c6a83ae6db6ad464dda5d6f0414197411f1d15ef6a9128`.
Continuous-session opens generate explicitly assumed adverse five-basis-point,
tick-rounded quotes. The 100-share displayed quantity is an assumption, not
reconstructed historical order-book liquidity. No provider, installed package,
shared lock or real trading connection is changed.

## Deterministic process recovery

Run the recovery acceptance with the shared interpreter:

```powershell
.venv-joint/Scripts/python.exe -m futu_bridge.joint.nautilus_recovery `
  --response output/joint/qlib_native_rl_data_20261109/provider_response.json `
  --output output/joint/new_recovery_verification
```

The coordinator freezes separate market-observation and prescribed-command
JSON files, hashes both, and binds them to converter code, the shared dependency
lock, native version and Python version. It starts a worker with fixed
`PYTHONHASHSEED=0`, necessary because native Python set iteration can otherwise
reorder simultaneous sibling quantity updates across processes.

The first worker processes a prefix using native streaming backtest mode,
flushes an immutable checkpoint, then waits. The coordinator **kills that actual
process**. A fresh worker verifies inputs, replays the prefix into a new native
engine and compares the complete exported economic boundary state before
continuing. A third, independent process executes the uninterrupted reference.
Every suffix event and final account/order/position/fee/book state must agree;
prefix/suffix concatenation also proves no duplicated or missing journal events.

The 31 verified cases cover five days each of a partial fill with delayed
cancellation, a fill-before-cancel race, queued insertion, a partially filled
parent with active children, queued child amendment, and a scheduled native
timer, plus working GTC children carried across two genuine sessions. The
checkpoint includes native order status/size/price/trigger, contingent links,
cash total/free/locked, positions and realized PnL, current cached BBO, cumulative
fee state, callback journals and active timer names. Matching-engine liquidity
consumption, private latency queues and timers are **reconstructed by native
replay**; they are not falsely described as directly serialized state.

This is replay-from-genesis recovery, with time proportional to prefix length.
It is not a constant-time checkpoint restore, `Trader.save` substitute for all
engine memory, or live brokerage reconciliation. Every acceptance process is an
offline `BacktestEngine`. A changed code binding, altered input, changed seed or
mismatching reconstructed boundary rejects suffix execution.

## Pinned Nautilus order/TIF/trigger inventory

The native enums are read from the installed pinned package. Matching behavior
is checked against `backtest/engine.pyx` and `execution/trailing.pyx`.

| Family | Joint status | Scope or remaining work |
|---|---|---|
| MARKET, LIMIT, STOP_MARKET, STOP_LIMIT | Implemented and verified | Existing 390 cross-engine lifecycle scenarios; finite fills, cancellation, amendments and expiry |
| MARKET_TO_LIMIT | Implemented and verified | New 200-share buy conversion/remainder cases; funded sell and more conversion/cancel races are available but not yet integrated |
| MARKET_IF_TOUCHED, LIMIT_IF_TOUCHED | Implemented and verified | New buy/sell bid/ask trigger cases; this does not certify every trigger/TIF combination |
| TRAILING_STOP_MARKET | Implemented and verified | Existing native additional-order acceptance, fixed price offsets |
| TRAILING_STOP_LIMIT | Implemented and verified | New buy/sell fixed-price-offset cases with independent trigger/fill oracle |
| GTC | Implemented and verified | Intraday and overnight working-child recovery |
| IOC, FOK | Implemented and verified | Native additional-order full/partial/unfilled cases; not an exhaustive cross product with all types |
| GTD | Implemented and verified | Native GTD with exclusive cutoff guard in the converter |
| DAY | Implemented as HK adapter policy | Joint DAY converts to an exact native GTD close; direct exchange-native DAY semantics are not claimed |
| AT_THE_OPEN, AT_THE_CLOSE | Explicitly unsupported by pinned native market/limit matching | Actual rejection cases pass. A faithful auction simulator additionally needs auction observations/rules; neither is implied by a continuous-minute open |
| DEFAULT, BID_ASK | Implemented and verified for exercised types | Quote-driven cases |
| LAST_PRICE, LAST_OR_BID_ASK | Joint trigger policy implemented and verified; native offset arithmetic verified separately | Receipt-ordered real trades/BBO and independent causal oracle; the joint controller submits native market tickets only after its declared condition |
| DOUBLE_LAST, DOUBLE_BID_ASK, MID_POINT | Explicit joint policy implemented and verified | Native trailing calculator rejects these sources. Joint double confirmation uses two fresh relevant observations, not a claim about a particular brokerage's definition |
| MARK_PRICE, INDEX_PRICE | Product/input semantics not supplied for this HK equity | Dedicated mark/index fields and product-specific interpretation are needed; do not substitute ordinary last price as verified mark/index data |
| NO_TRIGGER | Nonconditional/emulation-disabled configuration | Not a market-trigger implementation |
| Price/bps/tick trailing offsets | Implemented and verified | Native arithmetic plus joint-trigger executions; basis points express percentages. PRICE_TIER is explicitly unsupported by the pinned upstream calculator |
| Native trailing activation prices | Available, joint case pending | Existing real observations suffice; this is implementation/verification work |
| PARTIAL OTO / OUO | Implemented for declared integral-lot cash simulation | New sizing adapter and independent exposure checks; arbitrary sub-lot fills remain unsupported by the declared simulator |
| Full native/live recovery | Out of current certified scope | Offline deterministic reconstruction is implemented; live broker reconciliation is intentionally outside the manual-trading task |

## Pinned LEAN order/TIF inventory

The native order family list comes from pinned `Common/Orders/OrderTypes.cs`;
TIF classes come from `Common/Orders/TimeInForces/`. Native support alone does not
prove our HK converter supports the corresponding behavior.

| Family | Joint status | Scope or remaining work |
|---|---|---|
| Market, Limit, StopMarket, StopLimit | Implemented and verified | Existing paper/lifecycle workflows, amendments, cancellations and finite partial fills |
| TrailingStop | Implemented and verified | Native fixed-amount/percentage trailing semantics and funded buy/sell cases; faithful converter latch preserves post-trigger finite partial remainders |
| LimitIfTouched | Newly implemented | Native touched latch plus executable-side guard; buy/sell, delayed marketability, partial fills and cancellation |
| MarketOnOpen, MarketOnClose | Native implementation available, HK converter not implemented | Both integration and verified HK auction inputs/calendar are needed; do not call native availability joint coverage |
| OptionExercise | Native implementation available, integration/input gap | Requires dated option contracts, multipliers, expiry/exercise/settlement semantics and real observations absent from this HK equity test |
| ComboMarket, ComboLimit, ComboLegLimit | Native implementation available, integration pending | Real multi-equity captures exist; suitable aligned legs/portfolio contract and joint adapter are not implemented. Option combos additionally need option data |
| GoodTilCanceled, Day | Implemented and verified | Existing joint lifecycle policy and native ticket/account processing |
| GoodTilDate | Native date-end behavior available | Joint exact timestamp GTD uses `HKExactTimeInForce`, because the native class expires at date end; these distinct meanings must remain explicit |
| IOC/FOK | No corresponding pinned native TIF class | A faithful joint fill/cancel policy would need implementation; not a missing-data excuse |
| OCO/OTO/OUO grouping | Joint LEAN integration pending | Native ticket primitives exist, but Nautilus contingent evidence does not certify LEAN groups |
| Full process recovery of LEAN simulation | Joint integration pending | Nautilus reconstruction evidence must not be attributed to LEAN |

Consequently these additions close concrete P5 gaps, but do **not** justify a
"full feature coverage" flag. Available-but-unimplemented cases above remain
engineering work; genuine input dependencies are listed separately.

## Local test cases and evidence

Executable tests remain private/local. `test_nautilus_recovery.py` covers:
native partial children and independent fees; fee drift; early-trigger drift;
supported native types versus explicit unsupported auction outcomes; corrupted
market bytes; source/runtime drift; manifest path membership; real process kill,
native timer reconstruction and rejection of a changed boundary checkpoint.

Verified local evidence:

- `output/joint/nautilus_partial_verified_20261110/report.json`: 15 cases with independent oracle.
- `output/joint/nautilus_matrix_oracle_20261110/report.json`: 50 native type/rejection cases.
- `output/joint/nautilus_recovery_verified_20261110/report.json`: 31 killed-process recovery cases.
- `output/joint/lean_order_extensions_final_20261110/report.json`: 60 native trailing/touched cases.
- `output/joint/nautilus_trigger_variants_verified_20261110/report.json`: 36 joint-policy/native-execution cases and 18 native-offset cases on genuine live-captured observations.

`test_lean_order_extensions.py` independently re-audits all 60 native artifacts
and rejects changed latch state, duplicate partial-fill commission, placeholder
trade substitution, false final cancellation status, wrong percentage-derived
stop state and loss of the market remainder after a trailing-stop price reversal. These tests do not
rebuild or download LEAN, and the executable files stay outside published files.

`test_nautilus_trigger_variants.py` rechecks all 36 real-capture executions and
18 native offset sequences. It rejects premature double confirmation, changed
raw/decoded source values, modified stop levels/fees and unsupported mark/index/
price-tier substitutions. The three new local test modules contain 21 passing
test methods, including an actual killed-process recovery case. The final log
is `output/joint/native_recovery_extensions_tests_final_20261110.log`.

Reports pin exact capture identity; recovery additionally embeds source/runtime
bindings. Preserve failed probe directories as diagnostic evidence. Re-run with
a new output directory after changing any bound source or dependency.
