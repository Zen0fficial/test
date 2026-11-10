# Native LEAN equity combos and adapted immediate intents

These additions close specific execution gaps in the pinned LEAN integration.
They use separate extension assemblies in copied native runtimes; shared DLLs,
providers and the common Python environment remain unchanged. Orders and
accounts are simulated only.

## Three native combo order types

`lean_combos.run(panel, output, repo)` integrates native `ComboMarket`,
`ComboLimit` and `ComboLegLimit`, their `GroupOrderManager`, and native
complete-group fill models. It also checks net-limit and per-leg amendments,
whole-group cancellation, and native GTC/Day/GoodTilDate properties.

The two legs are real HK.00700 and HK.00388 observations from the existing
20-stock canonical Parquet panel in
`output/joint/plan_completion_20261110/panel`. Both original provider captures
must match their recorded hashes, and all five OHLCV fields must match Parquet
exactly. Twelve aligned real dates form three disjoint four-session windows;
ten scenarios per window produce **30 verified native cases**.

Each actual daily open becomes one explicitly assumed 09:30 observation with
zero bid/ask spread. This is a daily-data execution conformance model, not
historical BBO or an invented intraday path. No forward fill creates a missing
leg. Both equities use declared 100-share simulation lots, existing HKD 100,000
cash, long-only/no leverage, per-leg IBKR tiered fees and immediate settlement.
Historical lot/security-master and point-in-time publication metadata are not
certified by the daily capture.

Native complete-group semantics are preserved: either both legs completely
fill at the same observation or neither fills. Partial ratio requests fail
explicitly before native submission. This does not certify finite partial
combo fills or multileg options. Pinned native equity net-limit calculation
scales each leg's price by its ratio divided by 100; the independent oracle
uses that declared native convention instead of silently replacing it with a
different meaning of net price.

Market groups can execute at submission. Native nonmarket groups cannot fill
on the submission observation and wait for a later genuine price. Amendments
retain the original native creation time. These native differences remain
visible rather than forcing every order type into identical fill timestamps.

### Native equity GoodTilDate

Native equity `GoodTilDateTimeInForce` expires at the market close of the
specified date; it does not use the supplied intraday clock as an exact cutoff.
Acceptance supplies 09:31, checks orders are still working at noon, and requires
both native legs to cancel at that date's 16:00 close. Separate clock-only data
advances simulation time without inventing new equity prices. Native Day has
the corresponding date-close behavior. Existing `HKExactTimeInForce` remains
a distinct joint exact-timestamp policy.

## Adapted IOC and FOK

Pinned LEAN has no native IOC/FOK TIF classes. `lean_immediate.run` implements
an explicitly **adapted immediate parent-intent controller**, with native
Market/Limit tickets and native cash accounting underneath.

At the prescribed actual observation, the controller checks the executable
side quote against the parent limit and the declared 100-share immediate
capacity. A marketable IOC dispatches a native market ticket through a second
limit/capacity guard; any partial remainder is canceled at that same timestamp.
A FOK dispatches only when the entire requested amount can execute immediately.
Unmarketable requests and capacity-insufficient FOK requests produce native
limit tickets canceled immediately without execution or fees.

The report separately exposes joint IOC/FOK intent, integration mode, native
order type and native GTC property. It never claims a native IOC/FOK class or
calls cancellation at the next minute immediate execution. This implementation
assumes deterministic simulated BBO/capacity at one event; it does not imply
exchange-level FOK atomicity, live latency guarantees or lossless queue data.

Six policies in both buy and funded-sell directions run on five real minute
sessions: full IOC, partial IOC, unmarketable IOC, full FOK,
capacity-insufficient FOK and unmarketable FOK. Independent Decimal/account
checks require every target fill and cancellation at its submission timestamp.
Seed-position partial fills retain cumulative fees and remain separate from
the target immediate intent.

## Verification and boundaries

Independent real-price oracles verify native group identity, complete atomic
legs, account conservation, per-leg or per-ticket fees, expiry timestamps,
immediate quantity decisions, cancellations and explicit unsupported requests.
All raw/canonical inputs, configs, sources, shared lock and private runtime
DLLs are hash-bound. Local adversarial tests reject changed prices/times,
fees, group IDs, missing aligned legs and false native-TIF labels.

Local evidence:

- `output/joint/lean_combos_final_20261110/report.json`: 30 native group cases.
- `output/joint/lean_immediate_final_20261110/report.json`: 60 adapted immediate cases.
- `futu_bridge/tests/test_lean_combos.py` and `test_lean_immediate.py`: private executable regressions.

```powershell
.venv-joint/Scripts/python.exe -m futu_bridge.joint.lean_combos `
  --panel output/joint/plan_completion_20261110/panel `
  --output output/joint/new_native_combo_acceptance
.venv-joint/Scripts/python.exe -m futu_bridge.joint.lean_immediate `
  --response output/joint/qlib_native_rl_data_20261109/provider_response.json `
  --output output/joint/new_joint_immediate_acceptance
```

Use new output directories. These cases do not close grouped-order crash
recovery, a general common strategy/intent contract, arbitrary native execution
algorithms, partial multileg ratios, auction models or option settlement.
Those remain distinct engineering/input workstreams, not implicit coverage.
