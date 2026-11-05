# Daily HK.00700 ML baseline

This is a read-only prediction experiment for manual review. A small logistic
regression model predicts whether HK.00700's **next observed daily close is
higher than the completed feature day's close**. Equal closes count as not-up.
There is no trading strategy, order placement, simulated execution, or P&L test.
Local providers, platform converters, and the original Qlib exporter stay fixed.

## Run on this machine

The existing `.venv-qlib` already contains the required libraries. The tested
versions are scikit-learn 1.9.1, pandas 2.3.3, and NumPy 1.26.4. A separate
environment can install `pip install -e ".[ml]"`; the bridge itself still has
no mandatory ML dependencies.

Keep the existing local provider running, then choose a new output directory:

```powershell
.\.venv-qlib\Scripts\python.exe -m futu_bridge.ml.baseline `
  --output output/ml/hk00700-new-run --start 2020-01-01
```

The default end date is yesterday in Hong Kong. Current-day daily candles are
rejected because their final OHLCV is not yet known. Each invocation downloads
daily history from the unchanged local `/v1/bars` route and retrains locally.
It does not subscribe to live ticks or call DeepSeek or an external data API.

To reproduce the saved experiment without any network access:

```powershell
.\.venv-qlib\Scripts\python.exe -m futu_bridge.ml.baseline `
  --output output/ml/hk00700-reproduction `
  --input-response output/ml/hk00700_baseline_20261105/provider_response.json `
  --start 2020-01-01 --end 2026-11-04
```

## Model and evaluation

Eleven features use only the completed day and trailing observations:

- Close returns over 1, 5, and 20 observed days.
- Close relative to its 5-day and 20-day moving averages.
- Return volatility over 5 and 20 days.
- Close/open return, daily high-low range relative to close, and opening gap.
- Volume relative to its 20-day average.

The model is `StandardScaler` followed by L2 logistic regression with fixed
`C=1`, a 0.5 classification threshold, and no hyperparameter search. The scaler
and evaluation model fit only the earliest training partition. A chronological
60/20/20 split replaces random shuffling. One observation is purged at each
boundary so its future label cannot enter the next partition. Validation is a
diagnostic; it was not used to choose parameters or a threshold.

The constant baseline assigns the training up-day frequency to every later
day. Here that probability is 47.69%, so its hard prediction is always not-up.
Probabilities are experimental scores, not calibrated winning odds.

Implementation follows the documented [logistic regression API](https://scikit-learn.org/stable/modules/generated/sklearn.linear_model.LogisticRegression.html)
and [training-only preprocessing guidance](https://scikit-learn.org/stable/common_pitfalls.html#data-leakage).

## Result from November 5, 2026

Futu 16.37.27308 returned **1,683 raw daily bars**, January 2, 2020 through
November 4, 2026. Feature warm-up and the final unknown label leave 1,662
labeled observations. After the two boundary purges, training has 996 rows,
validation has 331, and testing has 333.

The held-out test uses feature dates June 30, 2025 through November 3, 2026,
with labels known through November 4:

| Metric | Logistic model | Constant baseline |
|---|---:|---:|
| Direction accuracy | 55.56% | 54.65% |
| Balanced accuracy | 52.63% | 50.00% |
| ROC AUC | 0.5327 | 0.5000 |
| Log loss, lower is better | 0.69155 | 0.68991 |
| Brier score, lower is better | 0.24907 | 0.24838 |

The accuracy difference amounts to three additional correct predictions. The
probability metrics are worse than the constant baseline. This establishes a
working, reproducible ML pipeline, **not a demonstrated predictive edge**.

## Saved outputs and inference

The verified run is `output/ml/hk00700_baseline_20261105/`:

- `report.json`: metrics, date ranges, source hash, versions, and limitations.
- `provider_response.json`: original local provider response bytes.
- `daily_bars.csv` and `features_and_labels.csv`: inspectable inputs.
- `predictions.csv`: evaluation-model probabilities, labels, and partitions.
- `evaluation_model.json`: frozen training-only model used for reported metrics.
- `model.json`: separate refit on all labeled history for subsequent inference.
- `latest_prediction.json`: refit prediction using the latest completed bar.

The refitted model is separate from the held-out evaluation model. It has no
new holdout score. On November 4 inputs it produced an up score of 40.18%,
generated November 5; this is a dated example, not a continuously updated feed.

Both model files contain plain JSON coefficients and scaling parameters rather
than executable pickle files. `predict_portable` supports inference without
scikit-learn; `features` uses pandas to build the same trailing inputs:

```python
import json
import pandas as pd
from futu_bridge.ml.baseline import features, predict_portable

folder = "output/ml/hk00700_baseline_20261105/"
model = json.load(open(folder + "model.json", encoding="utf-8"))
bars = pd.read_csv(folder + "daily_bars.csv", index_col="date", parse_dates=True)
probability_up = predict_portable(model, features(bars).iloc[-1])
```

Portable inference matches scikit-learn to an absolute tolerance of `1e-12`.
Nine ML regressions check future-data isolation, target alignment, boundary
purging, training-only scaling, portability, and invalid inputs. The full
suite passed 124 tests.

Raw price direction includes corporate-action price effects and is not total
return. The provider does not certify every trading date, so the target is the
next *observed* daily bar; missing dates are not filled. Daily timestamps are
date labels, not proof of when final OHLCV became available. Retrospective
history can contain revisions. Current Polymarket metadata is not inserted
into historical features without a verified availability history.
