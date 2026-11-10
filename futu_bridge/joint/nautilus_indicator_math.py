"""Independent vector arithmetic for common native indicator semantics.

These are verification oracles, not replacements for native calculations. They
use pandas/NumPy rather than native indicator instances. Native RSI is 0..1,
ROC's period includes the current observation, and bar VWAP is a typical-price
volume proxy. These choices are explicit, not silently standardized away.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def exponential(values: pd.Series, period: int) -> pd.Series:
    """Seed at the first observation and apply native exponential smoothing."""
    return values.ewm(alpha=2 / (period + 1), adjust=False).mean()


def common_oracles(frame: pd.DataFrame) -> dict[str, dict[str, np.ndarray]]:
    """Calculate independent references for the explicitly declared default configs."""
    close, high, low, volume = (frame[name].astype(float) for name in ("close", "high", "low", "volume"))
    typical = (high + low + close) / 3
    rolling = close.rolling(14, min_periods=1)
    delta = close.diff().fillna(0)
    gain = delta.clip(lower=0).rolling(14, min_periods=1).mean()
    loss = (-delta.clip(upper=0)).rolling(14, min_periods=1).mean()
    relative = gain / (gain + loss)
    relative = relative.where(loss != 0, 1.0)
    previous = close.shift().fillna(close.iloc[0])
    true_range = pd.concat([high, previous], axis=1).max(axis=1) - pd.concat([low, previous], axis=1).min(axis=1)
    bands = typical.rolling(20, min_periods=1)
    middle, deviation = bands.mean(), bands.std(ddof=0).fillna(0)
    oldest = close.shift(13)
    oldest.iloc[:13] = close.iloc[0]
    obv = np.sign(close - frame.open) * volume
    groups = frame.index.date
    weighted = (typical * volume).groupby(groups).cumsum() / volume.groupby(groups).cumsum()
    weighted = weighted.fillna(typical)
    raw = {
        "SimpleMovingAverage": {"value": rolling.mean()},
        "ExponentialMovingAverage": {"value": exponential(close, 14)},
        "DoubleExponentialMovingAverage": {
            "value": 2 * exponential(close, 14) - exponential(exponential(close, 14), 14)},
        "WilderMovingAverage": {"value": close.ewm(alpha=1 / 14, adjust=False).mean()},
        "RelativeStrengthIndex": {"value": relative},
        "AverageTrueRange": {"value": true_range.rolling(14, min_periods=1).mean()},
        "MovingAverageConvergenceDivergence": {"value": exponential(close, 12) - exponential(close, 26)},
        "BollingerBands": {"lower": middle - 2 * deviation, "middle": middle, "upper": middle + 2 * deviation},
        "RateOfChange": {"value": close / oldest - 1},
        "DonchianChannel": {"lower": low.rolling(14, min_periods=1).min(),
                            "upper": high.rolling(14, min_periods=1).max(),
                            "middle": (low.rolling(14, min_periods=1).min()
                                       + high.rolling(14, min_periods=1).max()) / 2},
        "OnBalanceVolume": {"value": obv.cumsum()},
        "VolumeWeightedAveragePrice": {"value": weighted},
        "Bias": {"value": close / rolling.mean() - 1},
    }
    return {name: {field: values.to_numpy(dtype=float) for field, values in fields.items()}
            for name, fields in raw.items()}
