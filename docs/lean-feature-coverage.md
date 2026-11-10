# LEAN native features and shared Python research

The converter integrates the pinned LEAN Algorithm Framework with the same real
HK.00700 minute capture used by Qlib and Nautilus. Universe selection, alpha
Insights, portfolio targets, drawdown risk, immediate simulated execution,
History, SMA/EMA indicators and consolidation remain native LEAN components.

The acceptance workflow evaluates three parameter configurations, each against
6,270 captured minute opens. Their native simulated fills and accounts are checked
independently with Decimal arithmetic and the existing HK fee scenario. The native
grid optimizer selects using the October 30 validation balance, before the
November 2-6 reused holdout. This verifies integration, not investment performance.

The Python algorithm delegates the same native framework behavior and proves
that NumPy, pandas, Qlib, Nautilus and MLflow come from `.venv-joint`. Its complete
event journal, source data and accounts must equal the C# run. A converter-owned
host adapts embedded-Python finalization ownership; it uses native engine teardown
and must exit successfully with the interpreter finalized. The upstream host's
shutdown failure is not ignored or converted to a successful exit.

An isolated QuantBook process reads native tick History and converts it through
the native pandas converter. Two 09:30-10:00 queries include both boundary ticks,
giving 62 observations. Every time and price is compared against the capture in
both representations. Only scalar columns are JSON-serialized for verification;
native Symbol objects remain in the research DataFrame. Research mode does not
access the uninitialized backtest order manager.

```powershell
.\.venv-joint\Scripts\python.exe -m futu_bridge.joint.lean_features `
  --response output/joint/qlib_native_rl_data_20261109/provider_response.json `
  --output output/joint/my_lean_feature_run
```

Use a new output directory outside the capture directory. The worker saves native
journals, source hashes, verification results, optimizer output, Python origins,
QuantBook output and process logs. Missing output, nonzero exit, failed account
checks and unsuccessful interpreter finalization fail the workflow.

The scoped combined evidence is
`output/joint/feature_coverage_parquet_foundation_20261110/lean/report.json`.
Eight local regression tests also reject corrupted fees, wrong optimizer winners,
duplicated/holdout scores, changed History prices/times, pandas corruption and
unsafe/incomplete research completion.

Limits remain explicit: one equity, derived BBO and one-share source-open ticks,
the capture-specific HK calendar, immediate settlement, three optimizer candidates
and selected research methods. Full multi-asset/fundamental universes, distributed
optimization, arbitrary research APIs and cloud deployment are not certified.
No brokerage routing or real trading is enabled.
