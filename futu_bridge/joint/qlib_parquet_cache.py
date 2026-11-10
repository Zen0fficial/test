"""Snapshot-scoped Parquet expression and dataset caches with transactional publication.

Qlib evaluates expressions unchanged. This module replaces persistence and local
cache coordination only. Competing workers may compute twice, but exactly one
immutable result is published; both must agree before the loser accepts it.
"""

from __future__ import annotations

import ast
from functools import lru_cache
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
from typing import Any, Callable

import pandas as pd
import pyarrow as pa
from qlib.data.data import LocalDatasetProvider, LocalExpressionProvider
from qlib.utils import parse_field

from .storage import SnapshotConflict, SnapshotStore


@lru_cache(maxsize=1)
def implementation() -> dict[str, Any]:
    """Bind keys to the native operator implementation and shared numeric runtime."""
    import qlib.data.base
    import qlib.data.data
    import qlib.data.ops

    paths = [Path(module.__file__) for module in (qlib.data.base, qlib.data.data, qlib.data.ops)]
    paths += [Path(__file__), Path(__file__).with_name("qlib_parquet.py")]
    return {"files": {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths},
            "runtime": {package: version(package) for package in ("pyqlib", "numpy", "pandas", "pyarrow")}}


def canonical(value: Any) -> str:
    """Serialize deterministic request configuration, rejecting arbitrary object state."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


class FrameCache:
    """Publish pandas index/column metadata and immutable values through SnapshotStore."""

    def __init__(self, root: str, snapshot: str, cache_root: str,
                 context: dict[str, Any] | None = None) -> None:
        """Pin data, implementation, adjustment and caller-declared fit/availability context."""
        source = SnapshotStore(Path(root)).snapshot(snapshot)
        self.store = SnapshotStore(Path(cache_root))
        self.binding = {"snapshot": snapshot, "source_hash": source.sha256,
                        "source_schema": source.metadata.get("schema_version"), "adjustment": "none",
                        "availability_cutoff": None, "processor_fit_interval": None,
                        "context": context or {}, "implementation": implementation()}
        self.hits, self.misses = 0, 0
        canonical(self.binding)

    def key(self, request: dict[str, Any]) -> str:
        """Hash the complete ordered expression, universe, range and context contract."""
        return hashlib.sha256(canonical({"binding": self.binding, "request": request}).encode()).hexdigest()

    def get(self, request: dict[str, Any], calculate: Callable[[], pd.DataFrame],
            *, refresh: bool = False) -> pd.DataFrame:
        """Read verified cached data or atomically accept a freshly calculated result."""
        identity = self.key(request)
        dataset = "c_" + identity[:62]
        head = self.store.head(dataset)
        if head is not None and not refresh:
            saved = self.store.snapshot(head)
            if saved.metadata.get("cache_key") != identity:
                raise ValueError("Cache-key collision")
            self.hits += 1
            return self.store.read(head).to_pandas()
        self.misses += 1
        frame = calculate()
        table = pa.Table.from_pandas(frame, preserve_index=True)
        metadata = {"cache_key": identity, "binding": self.binding, "request": request,
                    "real_orders_enabled": False}
        try:
            self.store.publish(dataset, table, metadata, expected_head=head)
        except SnapshotConflict:
            winner = self.store.head(dataset)
            if winner is None or self.store.snapshot(winner).metadata.get("cache_key") != identity:
                raise RuntimeError("Concurrent cache publication lost its committed result")
            other = self.store.read(winner).to_pandas()
            pd.testing.assert_frame_equal(frame, other, check_exact=True)
            return other
        return frame


class ParquetDatasetProvider(LocalDatasetProvider):
    """Delegate computation to native Qlib and persist complete dataset results locally."""

    def __init__(self, root: str, snapshot: str, cache_root: str,
                 context: dict[str, Any] | None = None) -> None:
        """Preserve native time alignment and fit-independent feature evaluation."""
        super().__init__()
        self.cache = FrameCache(root, snapshot, cache_root, context)

    def dataset(self, instruments: Any, fields: list[str], start_time: Any = None, end_time: Any = None,
                freq: str = "day", disk_cache: int = 1,
                inst_processors: list[Any] | None = None) -> pd.DataFrame:
        """Support native skip/use/replace semantics and bypass unkeyable processor objects."""
        if disk_cache not in (0, 1, 2):
            raise ValueError("disk_cache must be 0, 1 or 2")
        processors = inst_processors or []
        native = super().dataset

        def calculate() -> pd.DataFrame:
            """Run the upstream implementation without changing expressions or chronology."""
            return native(instruments, fields, start_time, end_time, freq, inst_processors=processors)

        if disk_cache == 0 or processors:
            return calculate()
        universe = self.get_instruments_d(instruments, freq)
        if isinstance(universe, dict):
            universe = {symbol: [[str(start), str(end)] for start, end in spans]
                        for symbol, spans in universe.items()}
        else:
            universe = list(universe)
        request = {"kind": "dataset", "universe": universe, "fields": list(fields),
                   "expression_ast": [ast.dump(ast.parse(parse_field(field), mode="eval")) for field in fields],
                   "start": str(start_time), "end": str(end_time), "frequency": freq}
        return self.cache.get(request, calculate, refresh=disk_cache == 2)


class ParquetExpressionProvider(LocalExpressionProvider):
    """Optional persistent native-expression cache for independently requested expressions."""

    def __init__(self, root: str, snapshot: str, cache_root: str,
                 context: dict[str, Any] | None = None) -> None:
        """Use the same complete snapshot/runtime/context binding as dataset caches."""
        super().__init__()
        self.cache = FrameCache(root, snapshot, cache_root, context)

    def expression(self, instrument: str, field: str, start_time: Any = None, end_time: Any = None,
                   freq: str = "day") -> pd.Series:
        """Cache the native index, name, dtype and values without changing window semantics."""
        native = super().expression

        def calculate() -> pd.DataFrame:
            """Retain the Series name as its single column label in Parquet metadata."""
            return native(instrument, field, start_time, end_time, freq).to_frame()

        request = {"kind": "expression", "instrument": instrument, "field": field,
                   "expression_ast": ast.dump(ast.parse(parse_field(field), mode="eval")),
                   "start": str(start_time), "end": str(end_time), "frequency": freq}
        return self.cache.get(request, calculate).iloc[:, 0]
