# Cross-sectional signal analysis on the observed HK basket

`futu_bridge.joint.panel_analysis` evaluates the saved predictions of six native
Qlib panel learners: GATs, GATs-TS, IGMTF, ADD, HFLGB and TRA. It does not train
models, change their budgets, choose a new model or submit orders.

```powershell
.venv-joint/Scripts/python.exe -m futu_bridge.joint.panel_analysis `
  --output output/joint/new_panel_analysis
```

The input is the verified 20-stock, 702-session Parquet basket. Each learner has
2,800 held-out scores across 140 dates. Every label is independently derived
from the untouched original captures: the next observed close divided by the
current close, minus one. Exact original decimals are used before conversion
to float64. These evaluation labels can differ slightly from the float32 labels
used inside native Qlib training.

The adapter checks the accepted learner report's source hashes, native model
artifact hashes and snapshot identity. It binds every saved prediction file,
original snapshot, native Qlib analytics source and adapter source to the output.
All input/source hashes must remain unchanged during analysis. Training is not
repeated simply to produce these diagnostics.

## Metrics and independent verification

Native `qlib.contrib.eva.alpha` supplies `calc_ic`, `calc_long_short_prec` and
`calc_long_short_return`. Independent centered dot products verify Pearson IC;
an independent average-rank implementation verifies Spearman IC. Independent
stable sorting verifies top/bottom quantile precision, label means and Qlib's
half-scaled long-minus-short return diagnostic. All checks require matching
undefined-value masks and absolute error at most 1e-12.

The fixed 20% quantile selects four stocks on each side of this twenty-stock
panel. Positive/negative precision tests whether their raw next-session returns
are above/below zero. Basket mean and top-minus-basket label returns are also
reported. The native return function's second output is the full basket mean;
it is not mislabeled as the top-quantile mean.

Predictions must contain exactly the declared stock/date panel, with unique,
ordered identities and finite scores. Labels must exist and end strictly after
their feature dates. Training and validation labels must end before the next
partition. Missing stocks, omitted dates, duplicate scores, infinities,
backdated targets and overlapping/unpurged partitions fail closed.

Equal scores use canonical instrument order for deterministic native quantile
selection. When large tie groups cause top/bottom sets to overlap, the overlap
is counted explicitly. Constant-score dates have undefined IC and selection
metrics and select no stocks. Constant-label dates have undefined IC; defined
selection diagnostics remain available. Undefined correlations are never
replaced by zero or one. Structural boundary tests distinguish these cases
from the real-score acceptance run.

## Outputs and scope

Each learner gets `aligned_scores_labels.parquet`, containing exact alignment,
target end dates, average ranks and quantile flags, and `daily_metrics.parquet`,
containing native-verified metrics, counts and degeneracy flags. Its JSON report
records descriptive means, split boundaries and verification errors. The root
report records all input hashes and the explicit scope.

This is a retrospective manually selected basket, not historical constituent or
publication-time evidence. Close-to-close targets do not establish executable
next-open returns. Quantile label means and spreads are analytical summaries,
not a funded portfolio: no lot sizes, tick sizes, corporate actions, fees,
borrow, fills or realizable profitability are asserted. These results do not
promote a model using an already examined holdout.
