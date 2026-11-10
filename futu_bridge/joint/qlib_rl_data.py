"""Convert immutable Futu minute responses to native Qlib execution-task data.

HK session selection belongs to this converter; the provider remains unchanged.
Only complete 330-minute continuous sessions are admitted. Each native tick is
one observed 30-minute block, and execution uses its first minute's open. The
current block's OHLCV features are masked by Qlib's FullHistoryStateInterpreter.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

FIELDS = ("open", "high", "low", "close", "volume")


@dataclass(frozen=True)
class ExecutionData:
    """Validated raw sessions, 30-minute blocks and chronological partitions."""

    minutes: pd.DataFrame
    blocks: dict[str, pd.DataFrame]
    partitions: dict[str, list[str]]
    provenance: dict[str, object]


def load_data(response: Path) -> ExecutionData:
    """Reject synthetic/truncated/malformed sources and incomplete market sessions."""
    payload = response.read_bytes()
    envelope = json.loads(payload)
    return from_envelope(envelope, hashlib.sha256(payload).hexdigest())


def from_envelope(envelope: dict[str, object], anchor_sha256: str) -> ExecutionData:
    """Apply the original session rules to source records, retaining a real archive anchor identity.

    A reconstructed union is an in-memory audit view, never a provider response
    or an archived synthetic capture. Its complete original sources are recorded
    separately by the Parquet lineage verifier.
    """
    source, data = envelope["source"], envelope["data"]
    if (source.get("provider") != "futu-desktop-session" or source.get("kind") != "historical"
            or data.get("symbol") != "HK.00700" or data.get("timeframe") != "1m"
            or data.get("adjustment") != "none" or data.get("truncated")
            or not data.get("complete_requested_range")):
        raise ValueError("Native RL requires a complete, unadjusted real Futu HK.00700 minute capture")
    frame = pd.DataFrame(data["bars"])
    index = pd.DatetimeIndex(pd.to_datetime(frame.timestamp, utc=True)).tz_convert("Asia/Hong_Kong")
    frame.index = index.tz_localize(None)
    if not frame.index.is_monotonic_increasing or frame.index.has_duplicates:
        raise ValueError("Duplicate or unordered market timestamps")
    values = frame[list(FIELDS)].to_numpy(dtype=float)
    if (not np.isfinite(values).all() or (values[:, :4] <= 0).any() or (values[:, 4] < 0).any()
            or (frame.low > frame[["open", "close"]].min(axis=1)).any()
            or (frame.high < frame[["open", "close"]].max(axis=1)).any()):
        raise ValueError("Invalid OHLCV values")
    blocks: dict[str, pd.DataFrame] = {}
    accepted, omitted = [], []
    for day, rows in frame.groupby(frame.index.normalize()):
        expected = pd.date_range(day + pd.Timedelta(hours=9, minutes=31), periods=150, freq="min").append(
            pd.date_range(day + pd.Timedelta(hours=13, minutes=1), periods=180, freq="min"))
        session = rows.loc[rows.index.isin(expected)].copy()
        if not session.index.equals(expected):
            raise ValueError(f"Incomplete continuous session: {day.date()}")
        omitted.extend(str(t) for t in rows.index.difference(expected))
        session.index = session.index - pd.Timedelta(minutes=1)
        grouped = session.groupby(np.arange(len(session)) // 30)
        block = grouped.agg({"open": "first", "high": "max", "low": "min",
                             "close": "last", "volume": "sum"})
        block.index = session.index[::30]
        if len(block) != 11 or (block.volume < 200).any():
            raise ValueError("Execution grid requires eleven liquid blocks")
        blocks[str(day.date())] = block.astype(float)
        accepted.append(session)
    dates = sorted(blocks)
    if len(dates) < 12:
        raise ValueError("Need at least twelve complete sessions for chronological training/validation/test")
    usable = dates[1:]  # First day supplies previous-day features only.
    n_train, n_val = int(len(usable) * .6), max(2, int(len(usable) * .2))
    parts = {"train": usable[:n_train], "validation": usable[n_train:n_train + n_val],
             "test": usable[n_train + n_val:]}
    if min(map(len, parts.values())) < 2:
        raise ValueError("Insufficient sessions in a partition")
    return ExecutionData(pd.concat(accepted), blocks, parts,
                         {"response_sha256": anchor_sha256, "source": source,
                          "raw_bars": len(frame), "continuous_bars": sum(map(len, accepted)),
                          "sessions": len(dates), "excluded_timestamps": omitted,
                          "timezone": "Asia/Hong_Kong", "grid": "30 observed minutes; lunch omitted",
                          "timestamp_assumption": "HK minute labels treated as bar end; subtract one minute",
                          "real_orders_enabled": False})


def normalized_features(data: ExecutionData) -> tuple[dict[str, pd.DataFrame], dict[str, object]]:
    """Fit scaling only on training dates; previous-close ratios are causal."""
    raw: dict[str, pd.DataFrame] = {}
    dates = sorted(data.blocks)
    for index, day in enumerate(dates):
        block = data.blocks[day]
        previous = data.blocks[dates[index - 1]].close.iloc[-1] if index else block.open.iloc[0]
        features = block[list(FIELDS)].copy()
        features.loc[:, ["open", "high", "low", "close"]] = features.iloc[:, :4] / previous - 1
        features["volume"] = np.log1p(features.volume)
        raw[day] = features
    training = pd.concat([raw[day] for day in data.partitions["train"]])
    mean, scale = training.mean(), training.std(ddof=0).replace(0, 1)
    result = {day: ((frame - mean) / scale).clip(-10, 10).astype(np.float32) for day, frame in raw.items()}
    return result, {"fields": list(FIELDS), "mean": mean.tolist(), "scale": scale.tolist(),
                    "fit_dates": data.partitions["train"], "clip": [-10, 10]}
