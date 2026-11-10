# Native HK execution and full Qlib nested RL

The joint framework now runs the **full Qlib 0.9.7 execution hierarchy**,
NautilusTrader 1.231.0 native matching/accounting, and the pinned LEAN engine's
native equity execution on the same captured HK.00700 data. Providers and the
original Qlib exporter are unchanged. Qlib, Nautilus and MLflow use the same
`.venv-joint` Python 3.12 environment; LEAN uses its existing local .NET runtime.
No real order-routing client is configured. The existing continuous live feeds
remain data observers, not paper brokerage sessions.

## Run

Use new output directories. The source response below is an existing immutable
real Futu capture, not a generated fixture. Local captures are not distributed.

```powershell
.\.venv-joint\Scripts\python.exe -m futu_bridge.joint simulate-native `
  --response output/joint/qlib_native_rl_data_20261109/provider_response.json `
  --output output/joint/my_native_execution

.\.venv-joint\Scripts\python.exe -m futu_bridge.joint train-qlib-nested `
  --response output/joint/qlib_native_rl_data_20261109/provider_response.json `
  --output output/joint/my_nested_training --iterations 8 --episodes 16
```

The corresponding APIs are `JointResearch.simulate_native` and
`JointResearch.train_qlib_nested`. The comparison builds the converter-owned
LEAN algorithm in the existing pinned checkout, then runs eighteen scenarios
in separate workers using one Python executable. It refuses engine failures,
missing orders, failed audits or differing account results. It never substitutes
a custom cash ledger for a failed native engine.

## Native components and adaptations

| Platform | Native components exercised | Converter-owned behavior |
|---|---|---|
| Qlib | `SingleAssetOrderExecution`, day and 30-minute `NestedExecutor`, 1-minute `SimulatorExecutor`, `Exchange`, `Order`, `Account`, `Position`, trade indicators | Observed HK calendars, source quote hook, actual tiered fee hook, first-minute child strategy, HK execution-state adapter |
| Qlib RL | `Trainer`, `TrainingVessel` collect/update/test, recurrent PPO/DQN, `EnvWrapper`, `FullHistoryStateInterpreter`, categorical actions | Immutable local minutes, training-only scaling, synchronous Windows seed iterator, fee-aware native reward |
| Nautilus | `BacktestEngine`, `SimulatedExchange`, native equity, market orders, cash account, positions and order events | Derived bid/ask ticks, prescribed schedule, HKD fee model |
| LEAN | `BacktestingTransactionHandler`, native equity, `EquityFillModel`, `SecurityMarginModel(1x)`, portfolio and order events | Local `hkpaper` market/calendar, derived tick files, HKD fee model, long-only guard |

LEAN's native `CashBuyingPowerModel` is for crypto/forex. Its native cash-equity
configuration uses `SecurityMarginModel` at 1x leverage. Both LEAN and Nautilus
reject unfunded test orders. Qlib can clip an order to affordable whole lots;
its converter reserves actual fees before allowing the native account update.
These different rejection/clipping semantics are preserved and tested.
Nautilus's instrument fee estimate and LEAN's mark-based margin estimate do
not fully represent this custom all-in HK charge model. Narrow converter guards
also reject the boundary where cash covers native notional but not the actual
side quote plus fees. Native engines still perform ordinary buying-power
checks, matching, fee debits and portfolio updates; no external cash ledger
replaces them.

Qlib's upstream state adapter uses mainland-China minute offsets, which map HK
afternoon fills incorrectly. `HKStateAdapter.update` retains upstream metric
formulas while indexing the actual observed session calendar. It also sums
multiple fills at the same minute instead of overwriting them, rejects invalid
indices/overfills, and retains real timestamps across lunch. The proxy avoids
upstream's date-only backtest-data cache and includes one-minute intervals.
A differential test checks matching state/metrics against upstream on a
CN-compatible interval; separate tests cover HK lunch and duplicate-minute fills.
The adaptation's MIT notice is included in `futu_bridge/joint/QLIB_NOTICE.txt`.
No installed framework source or learning algorithm is patched.

## Data, execution and cost contract

The capture contains 6,289 Futu minute records over 19 sessions, October 12 to
November 6, 2026. Its SHA-256 is
`7e35cdb971bfcfb1f5c6a83ae6db6ad464dda5d6f0414197411f1d15ef6a9128`.
The adapter accepts 6,270 continuous-session bars (330/session), excluding the
separate 09:30 record. **Minute labels are assumed to represent bar ends**;
subtracting one minute gives 09:30–11:59 and 13:00–15:59. This interpretation
is recorded as an assumption, not independently verified exchange timestamp truth.

Each simulated fill references an observed minute open. At 0, 5 or 10 bps,
buys round upward and sells downward to HKD 0.20. That tick applies in the
supported price band above HKD 200 through 500; other bands are rejected rather
than guessed. See the [HKEX spread table, including Phase 2](https://www.hkex.com.hk/-/media/HKEX-Market/Services/Circulars-and-Notices/Participant-and-Members-Circulars/SEHK/2026/ReductionofMinimumSpreadsFAQP2_E.pdf).

For Nautilus and LEAN, these prices become explicitly **simulated bid/ask
quotes**, with assumed depth of 200 shares. They are not captured BBO or tick
history. LEAN also needs a trade mark to initialize an equity's `HasData` flag;
the mark uses that observed open and a placeholder quantity of one, not the
future minute's aggregate volume. The full Qlib optional participation-cap test
uses completed historical minute volume; it is an ex-post fill constraint, not
information supplied to the policy at the open.

Each artificial portfolio starts at HKD 100,000, long-only, with 100-share lots,
no leverage, and immediate settlement. The split schedule buys 100 at 10:00
and 100 at 13:00; the immediate schedule buys 200 at 09:30. Both sell 200 at
15:30. The schedule is fixed before reading prices. Current IBKR Tier I costs
are debited by each native engine: commission, exchange/clearing fees, SFC/FRC
levies and rounded-up stamp duty. The formulas and sources are in
[the costed model comparison](ml-comparison.md) and `joint/costs.py`.
This is the existing fee scenario, not a historical reconstruction of every
broker bill or fee tier. Minimum commissions assume one full fill per child
order; multi-fill commission aggregation remains outside this adapter's scope.

## Real-data verification on November 9, 2026

The held-out November 2–6 sessions pass all **18 engine/scenario combinations**:
three engines, two schedules, three slippage settings. All **225 fills** match
independent Decimal calculations of chronology, side, quantity, tick price,
notional and fees. Intermediate native Qlib/LEAN cash and inventory, final
native cash, and zero final holdings also agree. Nautilus fills are taken from
native events and final cash/positions from its native account/cache.

| Schedule | Adverse slippage before tick rounding | Final HKD cash, identical across engines |
|---|---:|---:|
| Split | 0 bps | 99,367.42 |
| Split | 5 bps | 98,746.41 |
| Split | 10 bps | 98,346.41 |
| Immediate | 0 bps | 103,516.99 |
| Immediate | 5 bps | 102,917.03 |
| Immediate | 10 bps | 102,517.02 |

These are execution conformance results for prescribed schedules, not evidence
of a predictive strategy. Reports and per-engine journals are in
`output/joint/native_execution_final_20261109/`.

Native full-simulator PPO and DQN each passed two training iterations of eight
episodes: **32 total training episodes and 352 decisions**, followed by native
validation/test. Both updated finite weights; saved/reloaded logits and actions
were identical at every held-out decision. Ten held-out native buy-account
journals passed independent source/fee/cash checks. Two MLflow runs finished and
both weights and trainer checkpoints round-tripped byte-for-byte. Evidence:
`output/joint/qlib_nested_final_20261109/report.json`. This is an integration
budget, not the earlier 4,608-episode simple-simulator experiment or a claim
that nested RL improves returns. Nested training uses 330 minute feature rows,
eleven decisions, and a prescribed funded 200-share buy; it does not learn
market entry, allocation or shorting.

Local tests in `futu_bridge/tests/test_native_paper.py` exercise tick/band
validation, lunch/lot rejection, native fee reservation, cash and volume partial
fills, prevention of short inventory, upstream state parity, multiple fills,
tampered journals and native Nautilus risk denials. LEAN unfunded-order evidence
is in `output/joint/lean_paper_rejection_20261109/verification.json`. Tests and
market-data artifacts remain local, excluded from published updates.

The final converter boundary check in
`output/joint/native_execution_boundaries_20261109/report.json` also verifies
LEAN's native insufficient-cash rejection, the custom-fee reservation boundary,
and unchanged funded fills after adding that guard. Thirteen new local tests
cover these Python adapters. The complete regression suite passes 196 Python
tests and three JavaScript tests; all 211 shared dependency pins pass. Existing
native historical replay still matches 1,560 equity bars and 5,760 Polymarket
observations in `output/joint/native_execution_history_20261109/report.json`.

## Remaining scope

The subsequent [order lifecycle milestone](order-lifecycle.md) adds tested
limit/stop-market orders, cancellations, limit partial fills and cumulative
intraday parent fees. The [advanced lifecycle extension](advanced-order-lifecycle.md)
also covers stop-limits, amendments and GTC/DAY/GTD. The market-order evidence above remains a separate,
unchanged regression contract.

This closes the requested paths for HK.00700 continuous-session market orders.
It does not complete every upstream feature family. Queue/latency simulation,
trailing orders and IOC/FOK, realistic depth, cross-day partial-fill fee
aggregation, auctions, corporate actions, settlement calendars,
multi-asset/margin accounts, and broader Qlib portfolio/model/workflow and LEAN
Algorithm Framework features remain open in the capability inventory.
The Qlib hierarchy uses native trade indicators and account updates with
portfolio-performance reporting disabled; full native analysis remains a
separate gap. Qlib emits upstream warnings for means of empty no-order
indicators and pandas empty-frame concatenation; verified rewards, learning
weights and published account results remain finite.
