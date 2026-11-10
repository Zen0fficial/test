# Native Qlib models on Parquet

The joint framework provides native configuration and dataset adapters for 29 of
the 32 public `Model`/`ModelFT` learner classes in pinned Qlib 0.9.7. This is a
class inventory, not a claim that every hyperparameter, architecture, dataset or
market regime is validated. `qlib_model_inventory.inventory()` scans the actual
installed source and accepts explicit passing reports. Extension reports must
match current adapter hashes and prove unchanged sources during verification.

The existing eight classes retain their original configurations and budgets:
LinearModel (OLS, NNLS, Ridge, Lasso), LightGBM, flat LSTM, GRU, ALSTM, TCN,
Transformer and the compatible native DNN. The additional configurations below
exercise native fitting, changed learned state, finite held-out predictions,
index alignment, MLflow artifact lineage and exact serialized model reload.

## Additional datasets and learners

| Dataset contract | Native learners |
| --- | --- |
| `TSDatasetH` / `TSDataSampler` | LSTM-TS, GRU-TS, ALSTM-TS, TCN-TS, Transformer-TS, Localformer-TS, GeneralPTNN |
| Native Alpha360 `DatasetH` | Localformer, AdaRNN, KRNN, Sandwich, SFM |
| Native multi-horizon labels | TCTS, with one-, two- and five-session targets and five-session split purges |
| Native Alpha158 `DatasetH` | TabNet pretraining/fine-tuning; DoubleEnsemble sample reweighting and feature selection |
| Real multi-stock panel | GATs, GATs-TS, IGMTF, ADD and HFLGBModel |
| Native `MTSDatasetH` transport memory | TRA with two router states and native transport loss |

The single-stock input is the unchanged 1,683-session HK.00700 capture. The panel
contains 14,040 observed daily bars, 702 sessions for each of 20 manually selected
HK stocks. It supports actual same-date stock relationships and cross-sectional
labels. It is a retrospective selected basket, not a historical constituent
universe; results cannot establish unbiased universe returns or historical
publication-time availability.

Sequential datasets use six current-bar price/volume expressions, native
training-only robust normalization and explicit missing-value handling. Flat
datasets retain native Alpha158/360 feature definitions. The absent provider
VWAP field remains explicitly missing/imputed; no turnover scale is invented.
TabNet's pretraining validation stays entirely inside the original training
period. Predictive test data never fit normalization or select hyperparameters.

New acceptance configurations use two neural epochs or twenty boosting rounds,
CPU execution, fixed seeds and one numerical thread. They are bounded integration
tests, not reductions to any existing model/RL/reasoning budget and not optimized
strategies. Reported test MSE is descriptive only; classifiers such as HFLGB have
different native targets and must not be ranked against return regressors by
that diagnostic. Native training losses and early stopping remain in control.

## Faithful compatibility repairs

Installed packages remain unchanged. Three small converter-owned subclasses
repair demonstrated compatibility failures:

- TCN-TS applies the same time/feature transpose in prediction that native
  training already uses. Native convolutions, optimizer parameter objects,
  losses and training are unchanged.
- IGMTF retains computed hidden tensors in a one-dimensional NumPy object array
  instead of permitting NumPy to expand equal-shaped tensors. Tests compare all
  real-data hidden values and native daily aggregation exactly. Its explicitly
  selected native `ic` metric uses the real multi-stock panel; this also avoids
  an upstream default-metric tuple-comparison defect.
- TRA restores its optional diagnostic writer as closed after native
  deserialization. Model weights, transport state and prediction algorithms
  remain native. Each reload comparison starts from an equivalent native
  dataset-memory state.

Failed exploratory acceptance reports are retained; they are not counted as
coverage. Passing reports bind each native configuration, snapshot, model hash,
MLflow run and converter source hashes. See `qlib_model_extensions_verify`.

## Native data-selection meta-learning

`qlib_meta_parquet` uses native `InternalData`, `MetaDatasetDS`, `MetaModelDS`,
`PredNet`, `TimeReweighter` and `TrainerR` over the same real panel. A minimal
`InternalData` subclass explicitly retains pandas datetime group labels during
the unchanged Spearman calculation. Independent centered average-rank
correlation verifies every proxy/date matrix value.

Acceptance trains 29 native Ridge proxy models, produces a 702-by-29 performance
matrix, checks overlap/future masking, fits seven native meta tasks (four
training, three held out), and trains three native models with inferred time
weights. It proves learned finite weighting parameters, exact weight reload and
exact reweighted-model prediction reload. The native IC loss receives actual
stock returns. Its explicit minimum-count parameter is ten for the twenty-stock
basket; no artificial IC values or replacement loss are used. Three meta epochs
are a new conformance setting, not an optimized model-quality claim.

Native `InternalData.update()` is an upstream TODO. New snapshots currently
require explicit recomputation; there is no claim of native incremental meta
updates. The durable online scheduler is described separately in
[online scheduling](qlib-online-parquet.md).

## Remaining external requirements

CatBoost and XGBoost are absent from the frozen shared environment. They remain
explicit optional-dependency gaps. HIST requires genuine HK stock-to-concept
membership and instrument mappings; substituting Qlib's CSI300 mapping would
invalidate the experiment. Enhanced indexing needs dated risk exposures,
covariance, specific risk and benchmark weights. These are different from the
available manually selected market-data basket.

All work is local research. This adapter does not construct broker clients or
place real orders. Existing provider implementations, package pins and the shared
interpreter are unchanged. Overall full-framework coverage remains false while
these and other documented plan acceptance gaps remain.
