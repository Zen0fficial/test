# Native Nautilus feature coverage with fixed providers

The joint layer now connects NautilusTrader 1.231.0's Parquet catalogue, native
indicators and aggregation, Actor/Strategy composition through BacktestNode,
native performance reports, actual captured L2 snapshots, and additional
simulated order lifecycles. These are executed integrations with independent
checks, not an inventory of importable classes.

The providers and `.venv-joint` remain fixed. All order execution is offline in
BacktestEngine. The new native configuration factory imports only the existing
paper strategy and converter-owned Actor/fee configuration; no broker or live
order client is connected.

## Reproduce

```powershell
.\.venv-joint\Scripts\python.exe -m futu_bridge.joint.nautilus_features `
  --response output/joint/qlib_native_rl_data_20261109/provider_response.json `
  --output output/joint/my_nautilus_feature_check
```

The Python interface is
`run(response: Path, output: Path, repo: Path, *, depth_source: Path | None = None)`
in `futu_bridge.joint.nautilus_features`. The default depth source is
`output/joint/shared_live_20261109/nautilus/raw.jsonl` beneath `repo`.
Pass `--depth-source` to select a different captured stream under the same
validated schema. Missing or invalid depth fails the run; it is never silently
replaced with assumed depth. Use a new output directory to preserve earlier
successful and failed evidence.

## Executed feature families

| Family | Native implementation and independent verification | Scope |
|---|---|---|
| Parquet catalogue | Native instrument/bar/quote persistence, query and native BacktestNode loading; every OHLCV value and timestamp compared with captured data | One captured HK equity; strict new-catalog output avoids native overwrite hazards |
| Indicators | Native SMA, EMA and explicitly simple-average RSI; automatic Actor indicator registration and callback outputs checked against independent pandas formulas | Three indicators, configurable standalone period; not the complete indicator catalogue |
| Aggregation | Native composite `5-MINUTE-INTERNAL@1-MINUTE-EXTERNAL` aggregation; all five-minute OHLCV checked independently | Completed real minute bars; no empty lunch/overnight bars |
| Actor/Strategy/batch composition | Native importable configurations, BacktestNode with two runs, Actor indicators and existing paper Strategy in each run | Two prescribed, single-stock schedules; not arbitrary execution-algorithm composition |
| Native performance reports | Native order, fill, position and account reports plus BacktestResult performance statistics | Counts, realized PnL and final account verified; undefined native statistics serialized as null |
| Real depth | Full ten-level provider snapshots become native L2 CLEAR/ADD snapshots; Parquet read-back and managed-book Actor replay check every level and timestamp | Observed snapshot reconstruction, not exchange-level order events or lossless queue reconstruction |
| Further order lifecycles | Native IOC/FOK limits, native trailing-stop market orders, GTC overnight remainders | Simulated quotes/capacity, fixed price trailing offset, uninterrupted process; no restart recovery |
| Contingent orders and latency | Native OTO/OCO/OUO brackets and native LatencyModel insertion/cancellation races | FULL parent activation, one instrument, deterministic prescribed latency; see the separate evidence below |

The base catalogue stores **6,270 real one-minute OHLCV bars and 6,270 derived
quote ticks**, plus the native equity instrument. Completed bars are delivered
at their close, preserving the existing Futu bar-end interpretation. Quotes
use observed minute opens, five-bps adverse rounding and a declared size of
200 shares for the two batch runs. Bar execution is disabled, so completed
OHLCV bars cannot independently generate fills or leak their future high/low
into open-time execution.

BacktestNode queries the held-out November 2–6 sessions. Each of the two native
Actor/Strategy runs receives **1,650 bars and 330 native five-minute aggregates**.
The data engine has `time_bars_build_with_no_updates=False`, preserving lunch
and overnight gaps. Each source minute and each aggregate is compared directly
against the captured frame.

## Indicator contract and implementation comparison

`native_indicators(close: np.ndarray, period: int = 20)` returns an `N × 3`
array in SMA, EMA, RSI order. It creates fresh native state on every call.

- SMA uses the available history until the full window is present.
- EMA is seeded with the first close, uses `alpha=2/(period+1)`, and corresponds
  to pandas `ewm(span=period, adjust=False)`.
- RSI uses **simple** rolling average gains/losses, a zero first delta, and a
  fractional output from zero to one. A zero average loss yields one. This is
  not silently treated as Wilder RSI or rescaled to 0–100.
- Partial warmup values are emitted from the first observation; native
  readiness begins at `period` observations. The Actor path uses period 20.
- Inputs must be finite one-dimensional arrays and an integer period at least
  two. Empty arrays return shape `(0, 3)`. Future changes cannot affect a
  previously emitted prefix.

`reference_indicators` is a separate vectorized implementation of that exact
contract. All 6,270 rows agree within relative tolerance `1e-12` and absolute
tolerance `1e-10`, with further tests at periods 2 and 60. These interfaces let
the joint benchmark choose an implementation only after matching semantics.
The catalogue report records workload-specific write and read/verification
times; concurrent development load means those values are not a claim that
one platform is universally fastest. The root joint benchmark performs its
separate controlled comparisons.

## Actual market depth versus simulated liquidity

The existing observed stream contains **96 complete ten-bid/ten-ask depth
snapshots**, plus separate top-of-book events. Only full depth snapshots are
converted here. They contain 1,920 verified price levels in total. Raw scaled
price and volume fields must agree with decoded fields; levels must be unique,
ordered, uncrossed, on supported HK ticks, and complete. Receipt timestamps
must increase and cannot precede the provider's observation timestamp.

Each snapshot becomes a native CLEAR followed by ADD levels, with native
snapshot/end flags. The source does not supply verified exchange sequences,
so sequence zero explicitly means unavailable. Price-level order counts stay
in the preserved provider journal; L2 level volume is not misrepresented as
individual order ownership or placement/cancellation events. Top-of-book
records are not used to invent the nine missing levels.

Both the source and native event/availability timestamps are retained. Replay
uses provider observation time for `ts_event` and local receipt time for
`ts_init`, not unverified raw exchange-time fields. Parquet can normalize the
unused CLEAR zero-price precision from `0` to `0.0`; every numeric value,
action, identity, side, flag, sequence and timestamp is still checked exactly.
The replay consumes the **read-back catalogue**, not the pre-write objects.

Depth-source SHA-256:
`e202b6664098468682e1141409d150c3564697b42798f61d58d99590ff19d2c5`.
The capture is gap-possible and not lossless. This supports real observed L2
snapshot replay; it does not certify exchange order flow, queue priority,
trades hidden between snapshots, or achievable depth-based execution.

## IOC/FOK, trailing and overnight simulation

`nautilus_features_orders` adds 48 independently audited cases across the five
held-out sessions and four overnight boundaries:

- IOC and FOK limit orders, each with fully fillable, partially available and
  unmarketable cases. A 200-share IOC may execute 100 shares then cancel the
  remainder. The corresponding FOK cancels without a partial fill or fee.
- Native trailing-stop buys and sells with a fixed HKD 0.40 PRICE offset. Buy
  triggers ratchet downward with the ask; sell triggers ratchet upward with
  the bid. Execution uses the actual current derived quote when triggered.
  Sell cases first acquire inventory through a funded simulated native fill.
- GTC orders submitted at 15:59 survive into the next captured session. The
  partial case may fill 100 shares before the close and the remainder on the
  following day, retaining its parent identity and cumulative fee state. The
  unfilled case is canceled only at the next day's prescribed 15:29 time.
  A next-day gap can leave a remainder unfilled; the oracle never forces a fill.

These cases use the existing declared five-bps quotes, HKD 0.20 price tick,
100-share lot/capacity, HKD 100,000 starting cash and long-only accounting.
Capacity is a simulation assumption and is separate from the real depth replay.
The oracle reconstructs side quotes, eligibility, trailing ratchets, exact
native final states, cumulative fees and balances from original minute opens.
Every fill/cancellation must match, with no missing or extra events.

Overnight persistence means persistence **within one uninterrupted native
engine run**. Restart recovery, DAY/GTD exchange calendars over multiple days,
cross-day broker minimum/rounding conventions, percentage trailing offsets,
activation-price variants and multiple concurrent instruments remain outside
this acceptance contract. Native brackets and deterministic latency races have
their own later acceptance suite below.

## Results and artifacts

The final integrated evidence is
`output/joint/nautilus_features_verified_20261109/report.json`.
It records the original real-minute source hash
`7e35cdb971bfcfb1f5c6a83ae6db6ad464dda5d6f0414197411f1d15ef6a9128`
and the separate real-depth hash. Source files, native Parquet catalogues,
feature arrays, order/fill/position/account CSVs, native result statistics and
all additional order task/event journals are retained under that directory.

| Test component | Result |
|---|---|
| Catalogue | 6,270 bars + 6,270 quotes + one instrument round-trip |
| Two native composed runs | 3,300 bar callbacks + 660 aggregates total |
| Batch simulated orders | 25 fills; independently audited fees/cash/holdings |
| Split-schedule final cash / realized PnL | HKD 98,746.41 / −1,253.59 |
| Immediate-schedule final cash / realized PnL | HKD 102,917.03 / 2,917.03 |
| Real depth | 96 snapshots, 20 levels each, catalogue and managed-book replay |
| Additional order cases | 48 passed, 37 fills and 25 cancellations |
| Local adversarial tests | Nine passed |

The schedules are prescribed acceptance cases, not optimized strategies or
investment-return claims. Native position realized PnL sums, native total PnL
statistics, and the last native cash-report value must match the independently
audited ledger, not merely each other.

The local tests also reject missing bars, incomplete or inconsistent depth,
bad timestamps, altered levels, fees, quantities and balances; they verify
indicator reset/warmup/prefix causality and overnight fee identity. Tests stay
local and are excluded from the published wheel. No full-upstream-coverage
claim follows from this scoped integration; the shared capability catalogue
must retain that distinction.

## Native contingent orders and deterministic command latency

A subsequent independent suite is available through
`futu_bridge.joint.nautilus_features_contingent.run(response, output, repo)`:

```powershell
.\.venv-joint\Scripts\python.exe -m futu_bridge.joint.nautilus_features_contingent `
  --response output/joint/qlib_native_rl_data_20261109/provider_response.json `
  --output output/joint/my_nautilus_contingent_check
```

This suite builds native order lists using `OrderFactory.bracket` and submits
them through native `Strategy.submit_order_list`. It verifies the shared native
list identity, OTO parent links, reciprocal OCO/OUO sibling links, and native
reduce-only flags on both exit children. It does not replace contingent logic
with a converter cancellation callback.

The seven scenarios run independently on each of the five real holdout days:

| Case | Required native behavior |
|---|---|
| `bracket_tp` | A filled take-profit cancels its stop-loss sibling |
| `bracket_sl` | A stop-loss uses the current side quote and cancels its take-profit sibling; if no trigger occurs, prescribed cancellation removes both children |
| `parent_cancel` | Canceling an unfilled parent cancels both children; no child fill or fee is allowed |
| `ouo_partial_exit` | A two-lot parent completes, then an amended take-profit partially exits; native OUO reduces the protective stop from 200 to 100 shares before the remaining exit fills |
| `cancel_before_next_quote` | A one-second cancel arrives after the first 100-share fill but before the next quote, canceling the remainder |
| `fill_before_delayed_cancel` | A 61-second cancel arrives after the second fill; the native order remains FILLED and the expected late-cancel rejection is retained |
| `delayed_insert` | A 120-second delayed market submission fills against the quote at delivery, not its submission quote |

Bracket parent activation explicitly uses native `OtoTriggerMode.FULL`.
Nautilus's PARTIAL activation path was observed to risk-check full-size sell
children while only part of the parent was filled on the cash account, rejecting
the children before the expected proportional adjustment. That path is **not
certified** here. FULL waits until the parent is completely funded before
activating its children; subsequent native OUO partial-exit adjustments are
verified. There is no claim of protection while a FULL parent is still partial.

Canceling one native contingent order is sufficient to cancel the linked
siblings. The converter deliberately avoids sending redundant independent
cancel commands to every child. In the latency race, the late rejection is an
expected native result and is checked by identity, exact nanosecond timestamp,
reason, final FILLED state and account balance. Missing that rejection or
substituting an unrelated error fails verification.

`LatencyModel` uses zero base latency with explicit insertion or cancellation
delays. Clock-only control data advance the simulated exchange at each delivery
instant without changing its order book. Native market observations keep their
original captured times; strategies receive the corresponding quote callback
one nanosecond later. Every expected fill and cancellation is audited at the
exact native nanosecond phase. These deliberately large delays demonstrate
causal race behavior; they are not measurements or calibration of Futu/IBKR
network latency. Stochastic latency, simultaneous multi-instrument races and
general modification-latency combinations remain outside this matrix.

The separate evidence is
`output/joint/nautilus_contingent_verified_20261109/report.json`:
**35 passed real-data runs, 57 fills, 38 cancellations, 10 native child updates
and five expected late-cancel rejections**. Nine additional local tests check
linkage tampering, native child sizes, no overselling, parent cancellation,
before/after-fill races, delayed insertion quotes, exact times, fees/account
tampering, required native errors and future-data independence. The source is
frozen under the evidence directory; no provider or environment changes occur.

## Restart persistence remains an explicit gap

Native same-process GTC continuity and native `Trader.save()` are different
capabilities. Inspection of pinned source shows that Trader save/load stores
Actor/Strategy states through the cache; the kernel's configured persistent
cache backing uses Redis. The simulated exchange also owns in-memory matching
and in-flight message queues that its reset path clears. Saving strategy state
alone therefore does not demonstrate restoration of working native orders,
cash, positions, books, bracket links and pending delayed commands together.

No full-engine process-restart recovery claim is made. Closing that gap requires
a verified backing-store/reconciliation design and a two-process interruption
test matching an uninterrupted real-data reference, including failures during
partial fills and pending cancellations. Installed dependencies and local
providers are not changed to conceal the gap.
