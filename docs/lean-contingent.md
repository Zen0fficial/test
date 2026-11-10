# Native LEAN contingent groups

The new converter integrates pinned LEAN commit
`49b4aa7022abd38e99e9c7d3ec67929fa7f6d7b8` native `OrderFactory`,
`OrderContingency` and `BacktestingBrokerage` behavior. It exercises OCO, OUO
and complete-parent OTO on actual HK.00700 captures. Native grouping is retained;
the converter does not implement sibling cancellation or child release itself.

The isolated build compiles `lean_contingent.cs` and the unchanged cash-account
setup into a separate extension assembly against a private copy of the pinned
launcher DLLs. Shared native binaries, existing algorithms, providers and the
shared Python environment are unchanged.

## Verified native behavior

The 55-case matrix uses eleven cases on each of the five real holdout days:

| Case | Required result |
|---|---|
| OCO partial exit | First partial fill cancels the sibling; native cancellation propagation also cancels the winning order's remaining quantity |
| OUO partial exit | First 100-share fill reduces the sibling from 200 to 100; the second fill closes the position and cancels the sibling |
| OCO manual cancellation | Canceling one member cancels its sibling; held inventory remains explicitly unprotected |
| OTO complete parent | Children remain held during the first parent partial fill and release only after the complete 200-share parent fill; nonmarket children require newer data |
| OTO partial-parent cancellation | The remaining parent and held children cancel; the first 100 filled shares remain unprotected |
| Held-child amendment | Updating a held child makes no premature fill; a later parent amendment releases children only after full parent completion |
| Held-child cancellation | Canceling the held child cancels its sibling while the parent remains separate until its own prescribed cancellation |
| Active-child amendment | Native child tickets keep their linkage through a causal price amendment and partial fills |
| Three native rejection cases | Single-member OCO, duplicate-member OUO and the `Bracket` convenience method on a combo request must raise their exact upstream construction errors |

**Pinned OCO partial-fill caveat:** the native processor cancels the sibling
after the first fill. The sibling's cancellation then propagates back to cancel
the partial winner's remainder. This is reproduced from real native execution,
not described as continuing protection. Use the native OUO grouping when the
declared intent is proportional continuation through finite partial fills.

Native LEAN OTO currently has complete-parent semantics. Nautilus PARTIAL OTO
acceptance must not be attributed to LEAN. Combo orders themselves are distinct
from contingent groups; rejection of `.Bracket` on a combo is a limitation of
that convenience method, not a claim that native combo/contingency composition
through `.Triggers` is unavailable.

## Data, costs and verification

The unchanged minute source is
`output/joint/qlib_native_rl_data_20261109/provider_response.json`, SHA256
`7e35cdb971bfcfb1f5c6a83ae6db6ad464dda5d6f0414197411f1d15ef6a9128`.
Actual minute opens generate explicitly simulated adverse five-basis-point,
tick-rounded BBO. Capacity is 100 shares per side per observed minute. Those
are declared fill assumptions, not historical exchange liquidity.

The new fill adapter keeps native market/limit/stop candidate matching and
native grouping, then applies finite quantity and the existing cumulative
per-ticket HK IBKR Pro tiered scenario. It never reads the required placeholder
trade as an executable quote. Accounts start at HKD 100,000, long-only without
leverage, with the existing explicit immediate-settlement assumption.

A separate Python state machine enumerates expected fills from original prices
and prescribed commands. Independent Decimal calculations verify every fill's
price, time, quantity, incremental fee, cash and holding, plus native linkage,
activation times, sibling quantities, cancellation times and funded child
exposure. Native quirks are explicit in that source-level contract. Binding
checks cover the actual capture, inputs, configs, adapter sources, shared lock
and private native DLLs.

```powershell
.venv-joint/Scripts/python.exe -m futu_bridge.joint.lean_contingent `
  --response output/joint/qlib_native_rl_data_20261109/provider_response.json `
  --output output/joint/new_lean_contingent_acceptance
```

Use a new output directory. Final local evidence:
`output/joint/lean_contingent_verified_20261110/report.json`.
Local executable cases in `test_lean_contingent.py` re-audit all 55 journals and
reject altered fees, early OTO activation, missing native links, missing expected
rejections and premature unfunded children. Captures and tests remain local.

This closes the native LEAN contingent integration gap in the earlier order
inventory. The separate 30-case LEAN process-recovery report also supersedes
that inventory's old pending-recovery row. Neither proves grouped-order crash
recovery, general execution-algorithm composition, multileg option settlement,
auction behavior, or full P5/P6 coverage.
