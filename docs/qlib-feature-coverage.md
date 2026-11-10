# Native Qlib feature, model and workflow coverage

This extension connects additional Qlib 0.9.7 feature families to the unchanged
local daily Futu capture. It uses the shared Python environment and does not
install packages, modify Qlib, alter providers or send brokerage orders.

```powershell
.\.venv-joint\Scripts\python.exe -m futu_bridge.joint.qlib_features `
  --response output/joint/model_20261109/model/provider_response.json `
  --output output/joint/my_qlib_feature_run
```

The worker API is `run(response: Path, output: Path, repo: Path)`. Its input must
be raw **daily** HK.00700 data; the separate minute execution capture is not a
substitute. The captured dataset contains 1,683 sessions from January 2, 2020
through November 4, 2026. A new output directory retains frozen source bytes,
native raw/processed feature tables, serialized models, prediction CSVs, local
MLflow recorders, rolling tasks, native portfolio accounts, fill journals and
the combined report. Failures retain logs and a failed status.

## Implemented native paths

| Family | Verified behavior | Explicit scope |
|---|---|---|
| Alpha158 and Alpha360 | Both complete native feature schemas; train-only robust normalization, finite-value processing, imputation and DropnaLabel | Unavailable VWAP columns remain missing in raw features and are explicitly imputed for training |
| Supervised model catalogue | OLS, NNLS, ridge, lasso, LightGBM, LSTM, GRU, ALSTM, TCN, Transformer and DNN configurations fit, predict, serialize and reload | Eleven configurations across eight model families; does not imply every Qlib model is supported |
| Workflow/task configuration | Native `RollingGen`, `TrainerR`, `SignalRecord`, model/dataset/task artifacts | Three chronological expanding folds; local execution, not distributed MongoDB workers |
| Online recorder lifecycle | Native `OnlineToolR` promotion, replacement, rollback and `PredUpdater` | A historical final-day update; not a continuous service or live trading |
| Dataset cache | Native `SimpleDatasetCache` persistent local roundtrip and cache-byte stability | Immutable capture/output namespace; no Redis dependency or expression-cache service |
| Portfolio strategies | Native TopkDropout and native `WeightStrategyBase`/order generator | Single-stock, HKD 100,000, long-only, 100-share lots, real source opens and simulated fills |
| Native analysis | Full daily native account/position reports and arithmetic/geometric `risk_analysis` | Independent fee, account and metric verification; available matplotlib renders native account data |

## Missing fields and causality

The original provider exposes `turnover_raw` without a verified currency scale.
The converter does **not** guess that scale or use close/typical price as fake
VWAP. Native Alpha158 therefore has one unavailable raw VWAP column; Alpha360
has 60. Their names and missingness are retained, then explicitly zero-imputed
after train-only normalization. Models can be exercised, but full observed
VWAP feature coverage remains a data prerequisite.

All 300 available Alpha360 price/volume lag formulas are checked independently
against source OHLCV. Seven representative Alpha158 formulas and all 518
processed columns are checked against independent formulas/normalization.
Source-to-float32 tolerances are declared in the verifier. Native processors
fit only the training interval. The label is next observed close/current close
minus one; one label is purged at each train/validation/test boundary. The
default cross-sectional label normalization is deliberately omitted because a
single-stock cross-section cannot supply a meaningful normalized target.

Native neural networks receive two training epochs (the DNN receives twelve
steps) for compatibility/conformance, not optimized research claims. Tests
require actual neural weights to change, finite aligned predictions and exact
prediction identity after serialization. Model selection uses validation MSE
only. Historical test data has been examined before and is labeled as reused;
these results do not establish future trading skill.

## Merged implementation and compatibility fixes

`FiniteByDate` is a vectorized implementation of Qlib's native `ProcessInf`
semantics. It replaces only infinite values using the finite mean for the same
date/column, preserving existing missing values and index order. Tests compare
it directly with native `ProcessInf`, including cross-sectional all-infinite
and mixed-missing boundaries. Native normalization, imputation and data-handler
machinery remain in use. This removes nested Python date/column loops without
changing the transformation; serialized common benchmarks determine whether to
select it for a particular workload.

The native DNN compares Torch versions as strings, so version `2.15` follows
the obsolete `<=2.6` scheduler branch and passes the removed `verbose` argument.
`CompatibleDNN` supplies the same native ReduceLROnPlateau parameters without
that logging argument. Its native IC diagnostic also requires at least 50
stocks per date. For this single-stock test the diagnostic is explicitly
negative MSE; native MSE training, checkpoint selection and early stopping are
unchanged. It is never reported as cross-sectional IC. Scheduler parameters,
step behavior and the diagnostic are tested.

Qlib's configuration probes Redis even when selecting its Redis-free local
cache. The converter initializes without caches, then registers the unchanged
native `SimpleDatasetCache` through Qlib's wrapper hook. No Redis service is
required, no global package is patched, and each immutable source capture has
its own cache namespace.

The fixed environment does not contain Plotly. Native numeric performance
reports and risk analysis are retained; the converter renders those native
account values with installed matplotlib. It does not claim to provide
Qlib's entire interactive Plotly report catalogue.

## Native portfolio checks

Both strategies run on the last 40 observed daily sessions, using the previous
session's close return as their input. TopkDropout uses `topk=1`, `n_drop=1` and
90% risk degree. This is a deliberately small conformance case whose native
single-stock dropout can churn holdings; it is not an investment recommendation.
The weighting policy uses Qlib's native order generator to switch between a
positive-return long target and cash. Inventory, lot/cash clipping, position
updates and daily performance metrics remain native.

The daily exchange reuses the audited HK converter's five-bps, HKD 0.20 rounded
open-price fill and existing IBKR Tier I fee policy. Native strategy trial
positions are excluded from the committed execution journal. The independent
Decimal verifier reconstructs every fee, cash movement, inventory change and
daily marked account from original source prices. All 80 daily account rows
are checked. Risk summaries are independently checked in both native arithmetic
and compounded modes with an explicit 252-session annualization assumption.
No liquidity/depth, dividends, corporate actions or settlement calendar is
inferred from daily candles.

## Tests and remaining prerequisites

`futu_bridge/tests/test_qlib_features.py` stays local and covers native feature
schema/formulas, normalization boundaries, unavailable VWAP, nonzero labels,
purged forward labels, source tampering, vectorized/native processor parity,
model alignment/finite checks, DNN compatibility, risk formula tampering,
account tampering and invalid capture types. Full real-data evidence is in
`output/joint/qlib_features_final_20261109/report.json`.

Remaining families are not certified by this milestone:

- Native `MetaModelDS` can use MSE as its training loss, but its native
  `InternalData._calc_perf` supplies the time-performance matrix using per-date
  cross-sectional Spearman correlation. The single-stock dataset yields all
  missing values. Changing the loss does not provide that missing data, and
  filling the matrix with fabricated values would not be a faithful test.
- Distributed `TaskManager` requires a configured MongoDB service. Redis-backed
  expression caches/data services require Redis. The report records local
  executable/listener checks; the tested local TrainerR and local dataset cache
  are not represented as substitutes for distributed services.
- Enhanced indexing requires a multi-stock universe, point-in-time factor
  exposures/covariances, specific risk and benchmark constituent weights.
- Optional CatBoost/XGBoost/Optuna availability is reported from the fixed
  environment. Missing packages are not silently installed or renamed native
  replacements. Other untested neural, ranking and ensemble models remain
  outside the verified catalogue.
- Continuous online scheduling, point-in-time fundamentals and general
  multi-asset/corporate-action coverage remain separate work.

`full_feature_coverage` remains false. Native presence, successful import and
feature-family coverage are distinct from a passed real-data behavioral test.
