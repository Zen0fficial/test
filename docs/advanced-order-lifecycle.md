# Stop-limits, amendments and expiry in the joint framework

The joint offline simulator now supports stop-limit orders, amendments to price,
total quantity and stop trigger, GTC orders, HK session-day expiry, and exact-time
GTD expiry. Qlib, Nautilus and MLflow continue using the same pinned Python
environment. LEAN uses its existing pinned .NET checkout. No provider, provider
format, installed Python package, dependency lock or live feed adapter changes.
All fills are simulated; there is no brokerage connection or real order routing.

## Run and inspect

```powershell
.\.venv-joint\Scripts\python.exe -m futu_bridge.joint simulate-lifecycle `
  --response output/joint/qlib_native_rl_data_20261109/provider_response.json `
  --output output/joint/my_advanced_lifecycle_run `
  --suite all
```

`JointResearch.simulate_lifecycle(output, response=..., suite="all")` exposes
the same operation. `all` includes the eight prior baseline scenarios and
18 new cases. `extended` selects just the new cases; `baseline` selects the
previous contract. Choose a fresh output directory. Failed runs retain their
inputs, logs and failed status rather than overwriting prior evidence.

Each output contains frozen source bytes, per-case task requests, native event
journals and final accounts, per-engine conformance reports, LEAN build logs,
and a combined report. The CLI runs Qlib, Nautilus and LEAN independently and
fails if any engine or independent audit fails. Tests and captured market-data
artifacts stay local, outside the published package.

## Native and converter responsibilities

| Behavior | NautilusTrader 1.231.0 | LEAN pinned revision | Qlib 0.9.7 |
|---|---|---|---|
| Stop-limit order | Native `StopLimitOrder`, trigger latch and matching | Native `StopLimitOrder` ticket; converter quote fill model owns the persistent trigger latch | Converter parent state; native Exchange/Account execute eligible children |
| Amendments | Native `Strategy.modify_order` and `OrderUpdated` | Native `OrderTicket.Update` and `UpdateSubmitted` acknowledgement | Converter updates original parent instruction while retaining native executed children |
| GTC | Native GTC | Native GoodTilCanceled | Pending parent retained |
| HK DAY | Converter maps to native GTD at 16:00 HK | Native `DayTimeInForce` with the custom HK calendar | Converter expiry at 16:00 HK |
| Exact-time GTD | Native GTD and native strategy expiry timer | Converter `TimeInForce` implementation, native brokerage expiry/cancellation | Converter deadline checked before matching |
| Accounts and fees | Native cash account and positions, cumulative parent FeeModel | Native transaction/portfolio updates, incremental fees in the fill event | Native Qlib Exchange, Account and Position with cumulative parent fee reservation |

LEAN's stock `GoodTilDateTimeInForce` expires equity orders at the close of the
expiry **date**. Its time component does not implement the intraday deadline
used here. `HKExactTimeInForce` therefore supplies the timestamp policy through
LEAN's extension interface. LEAN's native stop-limit trigger properties have
internal setters; the custom fill model retains its latch without reflection
or upstream source edits and exposes it for independent verification.

The Qlib lifecycle state is explicitly converter-owned. This does not change
Qlib's native RL training or expand the RL policy action space to these orders.
Existing nested/simple RL and market-order simulation interfaces remain intact.

## Execution contract

- Source: the unchanged Futu 16.37.27308 HK.00700 capture, 6,289 raw records,
  6,270 accepted continuous-session minutes across 19 sessions. The five held-out
  sessions are November 2–6, 2026. SHA-256:
  `7e35cdb971bfcfb1f5c6a83ae6db6ad464dda5d6f0414197411f1d15ef6a9128`.
- The existing bar-end timestamp assumption and minus-one-minute conversion
  remain. Bid/ask prices are derived from observed minute opens with five-bps
  adverse adjustment and HKD 0.20 rounding. These are not captured historical
  BBO/depth data. Capacity is assumed at 100 shares per side per observed minute.
- Each case starts with HKD 100,000, no inventory, no leverage and immediate
  settlement. Sell cases first acquire inventory through simulated fills.
  Predetermined order policies use only information available at submission or
  amendment time; dates are not selected for favorable subsequent returns.
- A stop-limit latches when the opposite quote crosses its stop. Once latched,
  it remains a limit even if the quote later retreats across the trigger.
  Filling still requires that quote to satisfy the limit. A gap can trigger an
  order without filling it. Stop-limit fills use the current opposite quote,
  bounded by the limit, rather than guaranteeing the trigger price.
- Ordinary resting limits retain the previous conservative limit-price policy.
  A newly marketable amendment can execute immediately after acknowledgement:
  Nautilus/Qlib use the current quote; LEAN's custom ordinary limit policy uses
  the amended bound. These policies and resulting balances are checked
  separately, without requiring identical results across engines.
- LEAN preserves its native next-observation activation for newly submitted
  non-market orders. This can change fills and expiry remainders. Amendments to
  already active orders can execute during the amendment observation.
- Amendments retain the parent/native order ID, cumulative executed quantity,
  trigger latch and cumulative fee notional. `shares` means **new total order
  quantity**, not extra quantity. An amendment cannot target a terminal order
  or leave a zero/negative remainder; use cancellation for the latter. Scheduled
  amendments must follow submission and precede cancellation/expiry. Limit and
  trigger prices remain on supported HK ticks; quantities remain 100-share lots.
- Matching precedes scheduled cancellation/amendment commands at an observation.
  A partial-fill callback may immediately amend the remainder. Expiry has the
  opposite boundary rule: **no fill at or after its deadline**. Unfilled and
  partially filled orders expire; completed orders do not acquire a second
  terminal state. Unfilled portions incur no extra fee.
- GTC is verified to remain open through this single-session replay. DAY
  expires at the final daily close, not the noon lunch break. GTD is scoped to
  a later observed instant or 16:00 on the same captured day. This is not
  multi-day persistence, an auction model or a general holiday calendar.

## Native behavior corrected or retained explicitly

Nautilus 1.231's default stop-limit pricing can reuse the original trigger
price when a later amendment becomes marketable. The supported
`FillModel.get_orderbook_for_fill_simulation` hook supplies the current derived
quote and remaining assumed capacity for stop-limits. Native triggers,
amendments, matching, fills and accounting remain active. The hook counts
actual executed shares, so a partial order cannot reuse the same minute's
capacity.

Nautilus can also match an equity quote before its own GTD expiration check at
the same timestamp. Clock-only control observations at every deadline run the
native strategy expiry timer first. Equity book delivery follows by one
nanosecond and quote callbacks by two, a declared deterministic ordering rule,
not a measured latency. Original market event times are retained on the book.
The fill hook additionally refuses expired orders. LEAN receives a separate
non-equity clock-only stream at 16:00; Qlib checks the close deadline directly.
None of these controls adds a fabricated closing quote or market trade.

When a Nautilus price amendment completes a previously partial order, this
pinned version can emit a redundant cancellation callback after its final
fill. Its authoritative order state remains FILLED. The raw callback is retained
as `native_notices: cancel_after_fill`, separately from effective lifecycle
events. The independent oracle requires this specific notice where expected
and rejects unexpected notices. It does not erase evidence or pretend a filled
order was effectively canceled.

## Verification cases and evidence

| Area | New scenarios |
|---|---|
| Stop-limits | `stop_limit_buy`, `stop_limit_sell`, `stop_limit_gap`, `stop_limit_partial` |
| Amendments | `amend_price`, `amend_quantity`, `amend_increase`, `amend_stop`, `amend_trigger`, `amend_stop_limit`, `amend_partial`, `amend_resume` |
| Time in force | `gtd_unfilled`, `gtd_partial`, `gtd_boundary`, `day_unfilled`, `day_partial`, `gtc_open` |

The final matrix is recorded in
`output/joint/lifecycle_extended_final_20261109/report.json`: 26 scenarios ×
five held-out days × three engines = **390 runs**, including the 120 prior
baseline cases. A case name does not imply every day triggers or fills; the
oracle follows actual prices. Source bytes are unchanged.

| Engine | Fills | Cancellations | Amendments | Expiries | GTC orders left open |
|---|---:|---:|---:|---:|---:|
| Qlib | 141 | 38 | 55 | 25 | 5 |
| Nautilus | 141 | 38 | 55 | 25 | 5 |
| LEAN | 133 | 39 | 55 | 25 | 5 |
| Total | 415 | 115 | 165 | 75 | 15 |

The five redundant Nautilus post-fill cancellation notices are retained and
verified separately; they are not counted as effective cancellations above.
All engines also verify 29 triggered stop-limit parents each, including cases
where no fill follows the trigger. LEAN's activation policy explains its fill
and cancellation differences.

The independent advanced oracle imports no execution adapter or fee calculator.
It reconstructs quotes directly from source opens, enumerates eligibility
between instruction changes and deadlines, computes cumulative fees separately,
and compares every acknowledgement/fill/expiry, latched stop-limit state, final
native status, cash and inventory. Native canceled status is accepted for an
expiry only alongside the independently required expiry event.

Eleven new local adversarial tests cover immutable inputs, zero-remainder and
terminal amendment rejection, fee continuity after partial execution, triggered
but unfilled stop-limits, finite capacity, current-quote pricing after amendment,
exact expiry boundaries for all order types, close without a quote, GTC survival,
future-data independence, and rejection of altered/missing events and source
prices. Synthetic quote mutations are contract tests, not real-data evidence.

The full local regression suite passes **217 Python tests and three JavaScript
tests**. The structural gate covers 38 joint modules. All 211 shared dependency
pins, native imports and Qlib/Nautilus/MLflow import-order checks pass. The built
wheel contains the updated Python and C# converters byte-for-byte and excludes
tests. Aggregated checks and source/code/wheel hashes are recorded in
`output/joint/lifecycle_extended_milestone_20261109.json`; shared-runtime evidence
is in `output/joint/lifecycle_extended_runtime_20261109.json`.

## Remaining scope

Trailing orders, IOC/FOK, good-after-time, cross-day persistence and fee resets,
cancel/replace network races, matching priority, multi-order/multi-instrument
contention, actual queue/depth reconstruction, auctions, settlement/corporate
actions, and broader strategy/model/workflow coverage remain open. The CLI is a
prescribed offline conformance harness, not a live order-entry system. The
capability catalogue continues to report `full_feature_coverage: false`.
