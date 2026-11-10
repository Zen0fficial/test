"""Qlib storage adapters over pinned, verified Parquet snapshots.

Native Qlib providers, expressions, processors and learners remain unchanged.
Canonical decimals are converted only at the Qlib float32 boundary. A worker is
initialized against one immutable snapshot; changing its head cannot change the
worker's calendar indices. Published snapshots are read-only through Qlib's API.
"""

from __future__ import annotations

from functools import lru_cache
import hashlib
import json
from pathlib import Path
import struct
from typing import Any
from uuid import uuid4
import warnings

import numpy as np
import pandas as pd
from qlib.data.storage import CalendarStorage, FeatureStorage, InstrumentStorage

from .storage import SnapshotStore

FIELDS = ("open", "high", "low", "close", "volume")


class SnapshotView:
    """Bound the in-memory view to one verified source and an observed calendar."""

    def __init__(self, root: str, snapshot: str) -> None:
        """Load actual Parquet arrays once; do not reconstruct prices from archived JSON."""
        self.store = SnapshotStore(Path(root))
        self.snapshot = self.store.snapshot(snapshot)
        table = self.store.read(snapshot)
        if table.schema.metadata.get(b"joint_schema") != b"futu_source_bars_v1":
            raise ValueError("Qlib requires the normalized Futu bar schema")
        self.frame = table.to_pandas()
        timeframes = self.frame.timeframe.unique()
        if len(timeframes) != 1 or timeframes[0] not in ("1d", "1m"):
            raise ValueError("A Qlib snapshot must contain one supported frequency")
        self.freq = "day" if timeframes[0] == "1d" else "1min"
        if self.freq == "day":
            dates = pd.to_datetime(self.frame.trading_date)
        else:
            dates = pd.to_datetime(self.frame.event_ns, utc=True).dt.tz_convert("Asia/Hong_Kong").dt.tz_localize(None)
        self.frame["datetime"] = dates
        self.frame["instrument"] = self.frame.symbol.str.replace(".", "_", regex=False).str.upper()
        if self.frame.duplicated(["instrument", "datetime"]).any():
            raise ValueError("Resolve revisions into an immutable view before initializing Qlib")
        self.calendar = pd.DatetimeIndex(sorted(self.frame.datetime.unique()))
        self.frame = self.frame.set_index(["instrument", "datetime"]).sort_index()
        self.fields = {name: self.frame[name].astype(np.float32) for name in FIELDS}

    def require_frequency(self, freq: str) -> None:
        """Reject accidental frequency substitution instead of silently resampling."""
        normalized = {"1d": "day", "day": "day", "1m": "1min", "1min": "1min"}.get(freq)
        if normalized != self.freq:
            raise ValueError(f"Snapshot frequency is {self.freq}, requested {freq}")

    def series(self, instrument: str, field: str) -> pd.Series:
        """Return a contiguous native float32 view, preserving absent observations as NaN."""
        symbol = instrument.upper().replace(".", "_")
        if symbol not in self.frame.index.get_level_values("instrument"):
            return pd.Series(dtype=np.float32)
        rows = self.frame.xs(symbol, level="instrument")
        first = int(self.calendar.get_indexer([rows.index.min()])[0])
        last = int(self.calendar.get_indexer([rows.index.max()])[0])
        dates = self.calendar[first:last + 1]
        if field == "factor":
            values = pd.Series(np.ones(len(rows), dtype=np.float32), index=rows.index).reindex(dates)
        elif field == "vwap":
            values = pd.Series(np.full(len(dates), np.nan, dtype=np.float32), index=dates)
        elif field in self.fields:
            values = self.fields[field].xs(symbol, level="instrument").reindex(dates)
        else:
            return pd.Series(dtype=np.float32)
        return pd.Series(values.to_numpy(dtype=np.float32), index=pd.RangeIndex(first, last + 1))


@lru_cache(maxsize=4)
def view(root: str, snapshot: str) -> SnapshotView:
    """Cache at most four immutable market snapshots in each isolated worker."""
    return SnapshotView(str(Path(root).resolve()), snapshot)


class ParquetCalendarStorage(CalendarStorage):
    """Native calendar storage contract backed by observed Parquet timestamps."""

    def __init__(self, root: str, snapshot: str, freq: str, future: bool = False, **kwargs: Any) -> None:
        """Bind calendar indices to an immutable snapshot, never its mutable head."""
        super().__init__(freq, future, **kwargs)
        self.view = view(root, snapshot)
        self.view.require_frequency(freq)

    @property
    def data(self) -> list[pd.Timestamp]:
        """Return observed sessions only, even when future=True; do not invent trading dates."""
        if self.future:
            warnings.warn("The Parquet snapshot has observed sessions only; future exchange dates are unavailable",
                          RuntimeWarning, stacklevel=2)
        return list(self.view.calendar)

    def __getitem__(self, index: int | slice) -> pd.Timestamp | list[pd.Timestamp]:
        """Apply ordinary list indexing and half-open slicing."""
        return self.data[index]

    def __len__(self) -> int:
        """Count observed dates or minute labels."""
        return len(self.view.calendar)

    def index(self, value: str | pd.Timestamp) -> int:
        """Locate an exact observed timestamp with native list error semantics."""
        return self.data.index(pd.Timestamp(value))


class ParquetInstrumentStorage(InstrumentStorage):
    """Provide observed instrument spans; these are not point-in-time index memberships."""

    def __init__(self, root: str, snapshot: str, market: str, freq: str, **kwargs: Any) -> None:
        """Support the observed all-instruments universe for one pinned snapshot."""
        super().__init__(market, freq, **kwargs)
        self.view = view(root, snapshot)
        self.view.require_frequency(freq)
        if market.lower() != "all":
            raise ValueError("Only observed 'all' instruments exist; no historical constituent source is available")

    @property
    def data(self) -> dict[str, list[tuple[pd.Timestamp, pd.Timestamp]]]:
        """Retain each instrument's first and last observed timestamps."""
        return {symbol: [(rows.index.get_level_values("datetime").min(),
                          rows.index.get_level_values("datetime").max())]
                for symbol, rows in self.view.frame.groupby(level="instrument")}

    def __getitem__(self, key: str) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
        """Retrieve an instrument's observed lifespan."""
        return self.data[key]

    def __len__(self) -> int:
        """Count observed instruments."""
        return len(self.data)


class ParquetFeatureStorage(FeatureStorage):
    """Preserve native float32 indexed feature semantics over exact Parquet decimals."""

    def __init__(self, root: str, snapshot: str, instrument: str, field: str, freq: str, **kwargs: Any) -> None:
        """Resolve a field without consulting a provider or mutable dataset head."""
        super().__init__(instrument, field, freq, **kwargs)
        self.view = view(root, snapshot)
        self.view.require_frequency(freq)
        self.values = self.view.series(instrument, field.lower())

    @property
    def data(self) -> pd.Series:
        """Return a defensive copy so callers cannot mutate cached market history."""
        return self.values.copy()

    @property
    def start_index(self) -> int | None:
        """Return the first global calendar index, or None for an absent feature."""
        return int(self.values.index[0]) if len(self.values) else None

    @property
    def end_index(self) -> int | None:
        """Return the inclusive last global calendar index."""
        return int(self.values.index[-1]) if len(self.values) else None

    def __getitem__(self, index: int | slice) -> pd.Series | tuple[int | None, float | None]:
        """Use absolute calendar indices and the native exclusive slice stop."""
        if isinstance(index, int):
            if self.values.empty:
                return None, None
            if index not in self.values.index:
                raise IndexError(index)
            return index, float(self.values.loc[index])
        if index.step not in (None, 1):
            raise ValueError("Qlib feature storage does not support a slice step")
        start = self.start_index if index.start is None else index.start
        stop = self.end_index + 1 if index.stop is None and self.end_index is not None else index.stop
        if self.values.empty or stop is not None and start is not None and stop <= start:
            return pd.Series(dtype=np.float32)
        return self.values.loc[start:stop - 1 if stop is not None else None].copy()

    def __len__(self) -> int:
        """Count the aligned native feature range, including missing interior observations."""
        return len(self.values)


def initialize(root: Path, snapshot: str, *, cache: Path | None = None,
               context: dict[str, Any] | None = None, **kwargs: Any) -> None:
    """Configure isolated native Qlib providers and clear caches when changing snapshots.

Call this at worker startup, outside an active recorder. Writes go through the
snapshot coordinator; inherited Qlib mutation methods deliberately reject them.
"""
    import qlib
    from qlib.config import REG_US

    root = root.resolve()
    source = view(str(root), snapshot)
    common = {"root": str(root), "snapshot": snapshot}
    module = "futu_bridge.joint.qlib_parquet"
    configuration = {name + "_provider": {"class": f"Local{name.title()}Provider", "kwargs": {
        "backend": {"class": f"Parquet{name.title()}Storage", "module_path": module, "kwargs": common}}}
                     for name in ("calendar", "instrument", "feature")}
    if cache is not None:
        configuration["dataset_provider"] = {"class": "ParquetDatasetProvider",
            "module_path": "futu_bridge.joint.qlib_parquet_cache",
            "kwargs": {**common, "cache_root": str(cache.resolve()), "context": context or {}}}
    qlib.init(provider_uri={source.freq: str(root)}, region=REG_US, kernels=1, logging_level=40,
              expression_cache=None, dataset_cache=None, clear_mem_cache=True, **configuration, **kwargs)


def export_bin(root: Path, snapshot: str, output: Path) -> dict[str, Any]:
    """Publish a reproducible native bin cache from Parquet without editing the legacy exporter."""
    source = view(str(root.resolve()), snapshot)
    output = output.resolve()
    if output.exists() or output == root.resolve() or root.resolve() in output.parents:
        raise ValueError("Use a new native-format cache directory outside the canonical store")
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = output.with_name("." + output.name + "-" + uuid4().hex)
    staging.mkdir()
    for name in ("calendars", "instruments", "features"):
        (staging / name).mkdir()
    labels = [str(stamp.date()) if source.freq == "day" else str(stamp) for stamp in source.calendar]
    calendar = "\n".join(labels) + "\n"
    for suffix in ("", "_future"):
        (staging / "calendars" / f"{source.freq}{suffix}.txt").write_text(calendar, encoding="utf-8")
    instrument_rows = []
    for symbol in sorted(set(source.frame.index.get_level_values("instrument"))):
        folder = staging / "features" / symbol.lower()
        folder.mkdir()
        for field in (*FIELDS, "factor", "vwap"):
            values = source.series(symbol, field)
            payload = struct.pack("<f", int(values.index[0])) + values.to_numpy(dtype="<f4").tobytes()
            (folder / f"{field}.{source.freq}.bin").write_bytes(payload)
        rows = source.frame.xs(symbol, level="instrument")
        instrument_rows.append(f"{symbol}\t{rows.index.min()}\t{rows.index.max()}")
    (staging / "instruments" / "all.txt").write_text("\n".join(instrument_rows) + "\n", encoding="utf-8")
    manifest = {"source_snapshot": snapshot, "source_sha256": source.snapshot.sha256,
                "frequency": source.freq, "future_calendar": "observed_only", "real_orders_enabled": False,
                "files": {str(path.relative_to(staging)): hashlib.sha256(path.read_bytes()).hexdigest()
                          for path in staging.rglob("*") if path.is_file()}}
    (staging / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    staging.rename(output)
    return manifest
