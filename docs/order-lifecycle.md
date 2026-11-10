# Offline order lifecycle: limit, stop, cancel and partial fills

The [subsequent advanced lifecycle milestone](advanced-order-lifecycle.md) adds
stop-limits, amendments and GTC/DAY/GTD expiry. The eight scenarios and results
below describe the original baseline, retained as regression coverage. The CLI
now defaults to `--suite all`; use `--suite baseline` to reproduce these cases.

This milestone adds **limit and stop-market orders, cancellation of unfilled
orders, cancellation of a partially filled remainder, and completed limit
partial fills** to the joint framework. Providers, captured-data formats,
the original Qlib exporter, and the shared Python dependency lock are unchanged.
All orders run inside offline simulations; live feed observers remain data-only.

```powershell
.\.venv-joint\Scripts\python.exe -m futu_bridge.joint simulate-lifecycle `
  --response output/joint/qlib_native_rl_data_20261109/provider_response.json `
  --output output/joint/my_lifecycle_run
```

The Python API is `JointResearch.simulate_lifecycle(output, response=..., suite="all")`.
Choose a new output directory. LEAN builds only converter-owned additions in
the existing pinned source checkout. Qlib and Nautilus workers use the same
`.venv-joint` executable; no packages are installed or upgraded by this command.
The runner freezes the source, retains failed artifacts, audits every expected
event and rejects any missing engine, mismatched account or failed process.

## What each framework does

| Framework | Order lifecycle | Execution and accounting |
|---|---|---|
| NautilusTrader 1.231.0 | Native limit/stop-market orders, native cancel commands and acknowledgements, native partial states | Native `SimulatedExchange`, L2 snapshot matching with liquidity consumption, fee model, cash account and positions |
| LEAN pinned local revision | Native `LimitOrder`/`StopMarketOrder`, native `OrderTicket.Cancel`, native transaction/portfolio processing | Converter-owned quote fill model emits finite partial events; native engine applies fees, fills and holdings |
| Qlib 0.9.7 | Explicit converter-owned pending/trigger/cancellation state; upstream `Order` has no persistent limit/stop/status contract | Qlib's native `Exchange`, lot/cash/inventory clipping, `Account` and `Position` process every executed child |

Qlib lifecycle support is an implementation in the converter, not a claim of
upstream native limit/stop functionality. It complements the previously tested
full nested RL simulator; it does not replace that simulator or change its RL
action space. LEAN uses its documented [custom fill-model extension](https://www.quantconnect.com/docs/v2/writing-algorithms/reality-modeling/trade-fills/key-concepts)
because its standard equity fill policy does not provide this finite-liquidity
quote-only partial-fill contract. Installed framework source remains unchanged.

## Explicit simulation contract

- Real input: existing Futu HK.00700 capture, 19 sessions and 6,270 accepted
  continuous-session minutes. The holdout is November 2–6, 2026. Source hash:
  `7e35cdb971bfcfb1f5c6a83ae6db6ad464dda5d6f0414197411f1d15ef6a9128`.
- The previous bar-end timestamp assumption remains: subtract one minute and
  retain the actual HK morning/afternoon calendar, excluding lunch.
- Quote prices are derived from observed minute opens with five-bps adverse
  slippage rounded to HKD 0.20. They are **not historical BBO observations**.
- Liquidity replenishes at each observed minute: **100 shares per side**, shared
  by fills at that instant. Nautilus receives explicit L2 clear/add snapshots;
  LEAN's fill model applies the same cap to the existing exported quote stream.
  This is an assumed capacity, not evidence of historical depth or queue priority.
- Start each scenario with HKD 100,000, no holdings, no leverage and immediate
  settlement. Sell tests first acquire inventory through simulated native fills.
  Scenarios retain their ending inventory; they are order-conformance tests,
  not return comparisons or investment strategies.
- A limit fills only when the opposite quote meets its bound. At initial
  submission Qlib/Nautilus may execute at that quote; resting limits use the
  limit price, matching Nautilus's conservative native convention. Stops latch
  when the opposite quote crosses the trigger and execute at the current side
  quote; the trigger price is not a guaranteed fill price.
- Current-observation matching precedes scheduled cancellations. A fill at a
  cancellation instant can therefore happen before its cancellation request.
  A partial-cancel request cancels immediately after the first acknowledged fill.
- Native LEAN `BacktestingBrokerage.Scan` delays all non-market orders until
  **the next observation**. This behavior is preserved. Its expected fill times,
  amounts and balances can differ from Nautilus/Qlib; the independent oracle
  checks those native rules instead of forcing false cross-engine equality.

## Partial-fill fees

The existing IBKR Tier I rate scenario remains in use. The new intraday
convention computes cumulative charges on each parent order's **actual filled
notional**, debiting only the increase at each fill. Thus the minimum commission
and rounded stamp charge are not restarted for every partial. Canceling a
remainder charges nothing further; completely unfilled orders charge nothing.
Parent IDs isolate fee state. LEAN cancellation tags can change, so accounting
and journal identity are tied to native numeric order IDs rather than mutable
tags. This is an explicit fee aggregation assumption, not a reconstruction of
every broker contract note, cross-day minimum or rounding practice.

## Executed evidence

Eight scenarios across five holdout days and three engines pass **120 native
execution/converter runs**. The independent oracle enumerates eligible instants
from the original captured opens and checks event count, chronology, quantity,
limit/trigger behavior, partial totals, cancellation finality, cumulative fees,
native final order status, cash and inventory.

| Engine | Verified fills | Verified cancellations |
|---|---:|---:|
| Qlib native accounting + lifecycle converter | 55 | 20 |
| Nautilus native lifecycle | 55 | 20 |
| LEAN native lifecycle + fill adapter | 52 | 21 |
| Total | 162 | 61 |

The difference is expected from LEAN's delayed activation. All engines have
triggered buy-stop and sell-stop evidence, resting limits that fill and those
that remain unfilled, complete partial fills, and canceled partial remainders.
Cases are named `limit_partial`, `partial_cancel`, `limit_cancel`,
`limit_resting`, `stop_buy`, `stop_cancel`, `limit_sell`, and `stop_sell`.
A case name does not guarantee a fill on every day; eligibility follows the
actual observed prices, and unfilled residuals are explicitly canceled.

Evidence is under `output/joint/lifecycle_final_20261109/`: source bytes,
per-engine logs, each task and native event journal, and independent reports.
The local-only `test_order_lifecycle.py` adds ten adversarial tests: parent fee
minimums/idempotent previews, invalid inputs, finite capacity, partial
cancellation, unfilled cancellation, sell inventory, stop quote prices,
future-data independence, and rejection of missing/extra/tampered events.
Synthetic quote mutations in boundary tests are not counted as real-data runs.

The full local regression suite passes 206 Python tests and three JavaScript
tests. The structural gate covers 36 joint modules; all 211 shared dependency
pins and native import checks pass. Existing market-order conformance is rerun
separately under `output/joint/lifecycle_market_regression_20261109/`.

## Still open

This is scoped continuous-session HK.00700 support. The advanced extension now
covers stop-limits, amendments and GTC/DAY/GTD. Trailing orders, IOC/FOK,
cancel/reject races with network latency, multi-instrument contention,
real queue/depth reconstruction, cross-day
orders/fee resets, and general portfolio/strategy-framework coverage remain
open. The supported CLI deliberately uses prescribed scenarios; it is not a
live brokerage interface or a claim that every upstream order type now works.
