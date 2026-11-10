# HK.00700 model comparison and artificial portfolio

The joint framework now evaluates regularized models on real local Futu data
and records a fully funded, long-only artificial portfolio. **The selected
candidate has not demonstrated a useful trading edge.** Its probability error
improves slightly, but directional accuracy falls, and its zero-trade portfolio
merely matches cash. The original baseline remains intact as the benchmark.
No brokerage connection or real order submission is introduced.

## Data and evaluation

The unchanged local API supplied 1,685 unadjusted daily bars from January 2,
2020 through November 6, 2026. All overlapping OHLCV values equal the original
baseline snapshot. Current-day bars and the final unknown target are excluded.
The original baseline uses its saved **training-only evaluation model**, not
the separate model refitted on all history.

| Partition | Feature dates | Labeled observations |
|---|---|---:|
| Training | 2020-02-03 to 2024-02-16 | 996 |
| Validation | 2024-02-20 to 2025-06-26 | 331 |
| Retrospective test | 2025-06-30 to 2026-11-03 | 333 |
| Newly completed labels | Feature dates 2026-11-04 and 2026-11-05 | 2 |

Boundary labels are purged. Features use completed-day trailing windows only.
The test period was already examined during the original baseline study; it
is **not a new untouched holdout**. Two additional labeled sessions are too
few to establish performance. Raw price labels can reflect ex-dividend moves;
they are not total-return labels.

The fixed candidate family contains the saved 11-feature logistic baseline,
a stronger-regularized logistic model (`C=0.1`) with 15 features, and Qlib's
native `LGBModel` with 7 or 15 leaves. Qlib computes four added expressions:
10-day return, close relative to its 10-day mean, position within a trailing
20-day price range, and the ratio of 5-day to 20-day volume means. An independent
pandas calculation checks every expression after full-window warm-up; observed
maximum error was `9.2833e-7`, below the declared `1e-4` absolute tolerance.

LightGBM uses 250 maximum rounds, 25-round validation early stopping, 80 minimum
rows per leaf, L2 penalty 10, learning rate 0.03, two threads, and seed 7.
Each fitted family is also blended with the training up-rate using fixed
weights 0.25, 0.5, and 1.0. Thirteen candidate entries, including duplicate
unblended baseline representations, are compared by **validation log loss**.
`plan.json` precedes fitting, and `selection.json` precedes test scoring.
Thresholds and slippage scenarios are reported, not optimized on the test.

The selected candidate is `qlib_lgb_15_w0.25`: 25% Qlib prediction and 75%
training prior. Early stopping retained one tree. The constant training prior
is a separately reported diagnostic benchmark, not a post-hoc replacement
candidate. Its validation log loss is actually slightly lower than the
selected model's, reinforcing the lack of established predictive value.

| Retrospective test metric | Original baseline | Selected model | Constant training prior |
|---|---:|---:|---:|
| Accuracy | 55.56% | 54.65% | 54.65% |
| ROC AUC | 0.53271 | 0.49993 | 0.50000 |
| Log loss, lower is better | 0.691547 | 0.689906 | 0.689912 |
| Brier score, lower is better | 0.249073 | 0.248381 | 0.248384 |

The selected-minus-baseline log-loss difference is `-0.001641`. A descriptive
20-session circular-block bootstrap gives a 95% interval of approximately
`[-0.014904, 0.011985]`. This includes zero, is not adjusted for model search,
and does not turn the reused test into independent evidence. No test-driven
parameter search or live promotion follows these results.

## IBKR Pro Tiered costs

The user selected Tiered pricing. For ordinary HKD-denominated SEHK stocks,
the implemented current schedule, checked November 9, 2026, is:

| Charge, on each buy and sell | Calculation |
|---|---|
| Tier-I commission | 0.05% of notional, minimum HKD18 per order |
| Exchange fee | 0.00565% |
| Clearing fee | 0.0042%, no minimum/maximum |
| SFC transaction levy | 0.0027% |
| FRC transaction levy | 0.00015% |
| Stamp duty | 0.1%, rounded upward to the next whole HKD |

Sources: [IBKR commission schedule](https://www.interactivebrokers.com/en/pricing/commissions-stocks.php),
[SEHK third-party fees](https://www.interactivebrokers.com/en/accounts/fees/SEHK-StkFees.php),
and [IBKR's worked example](https://www.interactivebrokers.com.hk/en/index.php?f=1316).
At HKD50,000 notional, the component calculation totals **HKD81.35 per side**.
Components retain Decimal precision; the sum is rounded to cents after stamp
duty's separate ceiling. This reproduces the published example; individual
broker-statement aggregation can differ with order splitting.

The simulator fails if monthly two-sided turnover exceeds HKD15 million,
rather than silently applying Tier I outside its range. Other account trading
is not included in that threshold. The same current schedule is applied to
every simulated date: this is a current-cost scenario, not historical fee
reconstruction. Market data still uses the existing free/local providers;
these fees are simulated trading costs, not data-source charges.

## Artificial execution rules and results

Starting capital is **HKD100,000**, long-only, without leverage. The primary
threshold is 0.50. After the previous observed session closes, a qualifying
signal permits entry at the next observed session's open. The position exits
at that same session's close, keeping the account flat overnight. Entry size
uses available cash, a 100-share board-lot assumption, and buying fees; it
never uses that session's later close to choose the entry or quantity.

The primary scenario charges all fees plus **5 basis points adverse slippage
per side**. Prices are hypothetical execution values, not claimed auction
fills or tick-compliant order prices. The original close-to-close prediction
includes overnight movement which this next-open-to-close simulation cannot
capture. This first study therefore evaluates a specific executable timing
assumption, not all possible ways to trade the forecast.

| Primary scenario, 333 execution sessions | Baseline | Selected | Always long intraday | Cash |
|---|---:|---:|---:|---:|
| Ending equity, HKD | 78,387.65 | 100,000.00 | 38,302.65 | 100,000.00 |
| Net return | -21.61% | 0.00% | -61.70% | 0.00% |
| Closing-equity max drawdown | -21.70% | 0.00% | -62.36% | 0.00% |
| Round trips | 61 | 0 | 218 | 0 |
| Fees, HKD | 10,342.46 | 0.00 | 40,200.10 | 0.00 |

The selected probabilities stay below both 0.50 and 0.55. Its preservation of
capital results from abstention, not demonstrated profitable signals. The
always-long control eventually cannot afford one board lot on some days; it
is not buy-and-hold. Frictionless baseline equity is HKD93,670 (-6.33%), so
costs aggravate an already negative intraday result. Costed baseline equity
at 0/5/10 bps per side is HKD81,141.83 / 78,387.65 / 76,136.92. At threshold
0.55 and 5 bps, it is HKD89,721.12 from 24 round trips. These scenarios are
sensitivity checks, not a tuned trading policy.

Every artificial fill retains date, side, shares, notional, fee components,
and resulting cash. Cash stays nonnegative, but settlement dates are not
modeled: reuse of unsettled proceeds is assumed permitted. No dividend,
financing, FX conversion, or cash interest is credited. Daily closing-equity
drawdown does not capture intraday drawdown. There are no live orders.

## Reproduce and inspect

Use the [shared runtime](shared-runtime.md), signed-in local Futu service, and
new output directories. Download only completed sessions through the existing
API; the exact raw response used here is already saved locally.

```powershell
.\.venv-joint\Scripts\python.exe -m futu_bridge.joint compare-models `
  --response output/ml/comparison_20261109/provider_response.json `
  --baseline-dir output/ml/hk00700_baseline_20261105 `
  --output output/joint/new-model-comparison
.\.venv-joint\Scripts\python.exe scripts/plot_ml_comparison.py `
  output/joint/new-model-comparison/comparison
```

The final evidence is under `output/joint/shared_ml_final_20261109/comparison`:
`report.json`, `plan.json`, `selection.json`, `predictions.csv`, `features.csv`,
`selected_model.json`, native LightGBM files, portable logistic coefficients,
24 complete simulation ledgers, `mlflow.db`, local MLflow artifacts, and
`comparison.png`. The model reloader verifies predictions within `1e-12`.
It does not refit on test observations:

```python
from pathlib import Path
import pandas as pd
from futu_bridge.joint.comparison import predict_saved

root = Path("output/joint/shared_ml_final_20261109/comparison")
features = pd.read_csv(root / "features.csv", index_col=0, parse_dates=True).dropna()
probabilities = predict_saved(root, features)
```

Raw response SHA-256:
`d0be1cc239e7aad3930a977f8b89a4f4228d7f451dfb13f697772c63cd1c0210`.
Original baseline response SHA-256:
`dca287f5d9eab9490958802edff9fd8a81ed1245ba906b188cc8395113035680`.

## Test cases

The 15 new local accounting/model tests cover the official fee example, minimum
commission, stamp rounding, invalid values/dates, both-side costs, whole lots,
cash affordability, no-trade behavior, inclusive thresholds, full ledger cash
reconciliation, next-observed-session alignment across weekends, future-close
independence of entry sizing, adverse slippage, duplicate sessions, turnover
overflow, saved-model inference, and identical-forecast bootstrap behavior.
Shared-runtime/native import checks, real-data replay, fresh live streams,
SQLite experiment/artifact checks, baseline reproduction, and ledger audits
provide separate integration evidence. Tests and generated market-data artifacts
remain local and ignored by Git, following the existing publication preference.

```powershell
.\.venv-joint\Scripts\python.exe -m unittest discover -s futu_bridge/tests
.\.venv-joint\Scripts\python.exe -m futu_bridge.joint.quality
```
