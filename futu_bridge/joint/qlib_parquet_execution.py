"""Faithful HK execution conversion from immutable Parquet arrays.

This mirrors the joint JSON converter's session selection and partitioning while
retaining the upstream native simple/nested simulators and learning algorithms.
Archived JSON is used only for provenance; all market values come from Parquet.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from .qlib_rl_data import ExecutionData, FIELDS
from .storage import SnapshotStore
from .qlib_parquet_lineage import verify


def execution_from_snapshot(root: Path, identity: str) -> ExecutionData:
    """Reproduce HK bar-end conversion, complete sessions and chronological splits."""
    store = SnapshotStore(root)
    snapshot = store.snapshot(identity)
    lineage = verify(store, identity)
    table = store.read(identity)
    if table.schema.metadata.get(b"joint_schema") != b"futu_source_bars_v1":
        raise ValueError("Execution requires canonical Futu source bars")
    frame = table.to_pandas()
    if set(frame.symbol) != {"HK.00700"} or set(frame.timeframe) != {"1m"}:
        raise ValueError("Execution requires the real HK.00700 minute dataset")
    frame.index = pd.DatetimeIndex(pd.to_datetime(frame.event_ns, utc=True)).tz_convert(
        "Asia/Hong_Kong").tz_localize(None)
    frame.index.name = "timestamp"
    frame.loc[:, list(FIELDS)] = frame[list(FIELDS)].astype(float)
    frame = frame.astype({field: float for field in FIELDS})
    values = frame[list(FIELDS)].to_numpy()
    if (not frame.index.is_monotonic_increasing or frame.index.has_duplicates or not np.isfinite(values).all()
            or (values[:, :4] <= 0).any() or (values[:, 4] < 0).any()
            or (frame.low > frame[["open", "close"]].min(axis=1)).any()
            or (frame.high < frame[["open", "close"]].max(axis=1)).any()):
        raise ValueError("Invalid ordered OHLCV arrays")
    blocks, accepted, omitted = {}, [], []
    for day, rows in frame.groupby(frame.index.normalize()):
        expected = pd.date_range(day + pd.Timedelta(hours=9, minutes=31), periods=150, freq="min").append(
            pd.date_range(day + pd.Timedelta(hours=13, minutes=1), periods=180, freq="min"))
        session = rows.loc[rows.index.isin(expected)].copy()
        if not session.index.equals(expected):
            raise ValueError(f"Incomplete continuous session: {day.date()}")
        omitted.extend(str(t) for t in rows.index.difference(expected))
        session.index = session.index - pd.Timedelta(minutes=1)
        grouped = session.groupby(np.arange(len(session)) // 30)
        block = grouped.agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
        block.index = session.index[::30]
        if len(block) != 11 or (block.volume < 200).any():
            raise ValueError("Execution grid requires eleven liquid blocks")
        blocks[str(day.date())] = block.astype(float)
        accepted.append(session)
    dates = sorted(blocks)
    if len(dates) < 12:
        raise ValueError("Need twelve complete sessions for chronological training/validation/test")
    usable = dates[1:]
    n_train, n_val = int(len(usable) * .6), max(2, int(len(usable) * .2))
    partitions = {"train": usable[:n_train], "validation": usable[n_train:n_train + n_val],
                  "test": usable[n_train + n_val:]}
    if min(map(len, partitions.values())) < 2:
        raise ValueError("Insufficient sessions in a partition")
    digest = snapshot.metadata.get("capture_sha256")
    if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ValueError("Original capture identity is required for provenance")
    if digest not in lineage.envelopes:
        raise ValueError("Execution anchor is absent from the verified original source manifest")
    source = lineage.envelopes[digest]["source"]
    provenance = {"response_sha256": digest, "source": source, "raw_bars": len(frame),
                  "continuous_bars": sum(map(len, accepted)), "sessions": len(dates), "excluded_timestamps": omitted,
                  "timezone": "Asia/Hong_Kong", "grid": "30 observed minutes; lunch omitted",
                  "timestamp_assumption": "HK minute labels treated as bar end; subtract one minute",
                  "real_orders_enabled": False, "snapshot_id": identity, "snapshot_sha256": snapshot.sha256,
                  "analytical_source": "parquet", "snapshot_root": str(root.resolve())}
    if len(lineage.envelopes) > 1 or any(value is not None for value in lineage.selections.values()):
        provenance["capture_lineage"] = lineage.evidence()
    return ExecutionData(pd.concat(accepted), blocks, partitions, provenance)
