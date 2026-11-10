# Native Nautilus indicator catalogue

The joint framework exposes every **37 concrete public indicator classes** in
the shared environment's pinned NautilusTrader 1.231.0. Calculations run inside
the existing native implementation; the converter handles typed parameters,
real market-data objects, explicit gaps/warmup, verification and immutable
Parquet outputs. There are no order or broker entrypoints in this component.

This is complete class coverage for this pinned Nautilus indicator catalogue,
not a claim to cover all indicators in LEAN, all possible parameter combinations
or indicators added by later releases. The runtime inventory fails closed when
the set of concrete native classes changes.

## Public API

```python
from pathlib import Path
from futu_bridge.joint.nautilus_indicator_catalogue import configuration
from futu_bridge.joint.nautilus_indicators import (
    NativeIndicatorStream, bars, evaluate, run,
)

# Typed constructor overrides: integers, floats, bools and enum names stay distinct.
config = configuration("RelativeStrengthIndex", period=14, ma_type="SIMPLE")
stream = NativeIndicatorStream(config)
for observation in bars(real_minute_dataframe):
    current = stream.update(observation)
    if current["initialized"]:
        consume(current["value"])

# An independent offline batch retains typed values and warmup flags in Arrow.
table, checks = evaluate(config, bars(real_minute_dataframe))

# Full catalogue acceptance and immutable Parquet publication.
report = run(Path("output/joint/qlib_native_rl_data_20261109/provider_response.json"),
             Path("output/joint/new_native_indicator_acceptance"), Path.cwd())
```

`configuration` accepts only declared constructor names/parameters, validates
scalar types and finite bounded periods, and invokes native validation. Enum
names are mapped only to the pinned native enum. Weighted-average weights are
an explicit tuple of floats and must match the period. The default configs are
listed directly in `nautilus_indicator_catalogue.SPECS`; the report records every
resolved config, including every native default parameter.

`NativeIndicatorStream.update` is the incremental path for converters. It
rejects duplicate/late inputs, native/raw event-time disagreement and mixing
quote objects into a bar config. The batch path compares each handler update
with a separately instantiated native `update_raw` path. SpreadAnalyzer has no
raw method; a separate deque/mean calculation verifies its output instead.

## Covered native functions

| Family | Concrete native classes |
|---|---|
| Moving averages | AdaptiveMovingAverage, DoubleExponentialMovingAverage, ExponentialMovingAverage, HullMovingAverage, SimpleMovingAverage, VariableIndexDynamicAverage, WeightedMovingAverage, WilderMovingAverage |
| Trends | ArcherMovingAveragesTrends, AroonOscillator, DirectionalMovement, IchimokuCloud, LinearRegression, MovingAverageConvergenceDivergence, Swings |
| Momentum | Bias, ChandeMomentumOscillator, CommodityChannelIndex, EfficiencyRatio, PsychologicalLine, RateOfChange, RelativeStrengthIndex, Stochastics |
| Volatility/channels | AverageTrueRange, BollingerBands, DonchianChannel, KeltnerChannel, KeltnerPosition, RelativeVolatilityIndex, VerticalHorizontalFilter, VolatilityRatio |
| Volume | KlingerVolumeOscillator, OnBalanceVolume, Pressure, VolumeWeightedAveragePrice |
| Categorical/microstructure | FuzzyCandlesticks, SpreadAnalyzer |

All 36 native bar handlers and all 12 native quote handlers are integrated.
The quote routes cover eight moving averages, MACD, BollingerBands,
DonchianChannel and SpreadAnalyzer. Quote-based moving averages explicitly use
MID in the acceptance matrix; callers may request native BID/ASK. LAST is
rejected for quote input because a quote has no last-trade field.

The inventory explains every omitted symbol: Indicator/MovingAverage are base
classes, MovingAverageFactory is a factory for included concrete classes,
FuzzyCandle is an exported output record, and the remaining enum/constant
classes configure or describe calculations. It also records why unsupported
input routes cannot be used: inherited native handlers raise
NotImplementedError. No OHLC bar is synthesized from a quote to bypass that.

## Real observations and timing

Acceptance uses 6,270 actual continuous-session HK.00700 minute bars from 19
sessions and 95 actual captured top-of-book messages. Bar source:
`output/joint/qlib_native_rl_data_20261109/provider_response.json`, SHA256
`7e35cdb971bfcfb1f5c6a83ae6db6ad464dda5d6f0414197411f1d15ef6a9128`.
Live BBO source: `output/joint/shared_live_20261109/nautilus/raw.jsonl`.

The existing loader expresses minute starts. The converter timestamps each
bar at start plus one minute, when a completed-bar calculation can first be
used under that label convention. Historical source labels are interpreted as
bar ends; historical point-in-time availability is **not verified**. Quotes use
actual local receipt nanoseconds, not fabricated exchange timestamps. Their
raw fixed-point prices/volumes must agree with the decoded fields.

Lunch and overnight gaps contain no synthesized bars, zero volumes or forward
fills. `gap_policy="carry"` preserves native observation-count windows.
`gap_policy="session_reset"` constructs a fresh native instance on each new
Hong Kong session and requires warmup again. Native VWAP also applies its own
day reset under carry mode. This HK daytime capture does not cross a UTC-day
boundary within a trading session.

The actual BBO capture has constant bid/ask prices. It validates native quote
conversion and window mechanics but does not establish performance across
changing spread regimes. More real BBO observations are required for that.

## Native semantics retained

* RSI is native **0 to 1**, not a silently rescaled 0 to 100 feature.
* ROC's `period` includes the current observation, yielding period-minus-one
  intervals after warmup.
* BollingerBands uses typical price `(high + low + close) / 3` and population
  standard deviation for its default SIMPLE config.
* Native bar VWAP is a **typical-price times bar-volume proxy**. It is neither a
  close-only approximation nor a claim to true trade-level exchange VWAP.
* Native OBV assigns volume by each bar's close versus its own open, not the
  previous bar's close.
* Native MACD exposes the fast-minus-slow line; this class has no signal line.
* SpreadAnalyzer becomes initialized on capacity-plus-one observations in the
  pinned implementation. The adapter records that behavior rather than changing it.

The acceptance checks independent NumPy/pandas arithmetic for SMA, EMA, DEMA,
Wilder average, RSI, ATR, MACD, BollingerBands, ROC, DonchianChannel, OBV, bar VWAP
proxy and Bias. The other native classes are checked through independent raw
versus typed-handler input paths, reset/fresh-instance comparisons and session
gap processing. Input-path parity is not represented as an independently
derived formula oracle for every long-tail indicator.

### Pinned native reset defect

Native `VariableIndexDynamicAverage.reset()` retains `cmo_pct` and its nested
CMO history. The real-data acceptance reproduces both stale public state and
different post-reset suffixes. The joint `NativeIndicatorStream.reset()` and
session policy construct a fresh native instance, clearing all hidden state
without rewriting its arithmetic. The report records the upstream defect;
it does not count native reset as passing when only the joint policy passes.

## Parquet contract and evidence

Each indicator/route has an immutable snapshot with long typed columns:
event/source-row identity, indicator/field, number/integer/text value, value
kind, native initialized/has-input flags and validity. Integer enums retain
their names, and datetime outputs retain integer nanoseconds. Uninitialized or
nonfinite output is null with `valid=false`; it is not replaced by zero.

Snapshot metadata binds both actual inputs, exact configurations, new adapter
sources, shared lock, native module hashes and timestamp/proxy semantics.
Every publication requires a typed Parquet round trip. The existing
SnapshotStore keeps analytical values in Parquet and transactional membership
in its small SQLite control catalogue; this component introduces no separate
analytical SQL database or MongoDB dependency.

Final evidence: `output/joint/nautilus_indicators_verified_20261110/report.json`.
Local tests: `futu_bridge/tests/test_nautilus_indicators.py`. These cover complete
inventory, real-data independent formulas, config/domain failures, invalid
input order, warmup masking, overnight policy, actual BBO validation, native
VIDYA reset regression, immutable typed round trips and source/hash bindings.
Captures and tests remain local under the repository publishing policy.

```powershell
.venv-joint/Scripts/python.exe -m futu_bridge.joint.nautilus_indicators `
  --response output/joint/qlib_native_rl_data_20261109/provider_response.json `
  --output output/joint/new_native_indicator_acceptance
```

Use a new output directory: publication requires that an indicator dataset
does not already have a head. This preserves prior evidence rather than
overwriting it during repeated acceptance runs.
