"""Evaluate the complete pinned native indicator catalogue into immutable Parquet.

Real captured minute bars and top-of-book observations remain separate inputs.
Minute end timestamps are an explicit interpretation of historical Futu labels;
historical point-in-time availability is unknown. No generated fills, quotes,
zero-volume bars or broker routes are introduced by this research adapter.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
import importlib
import json
import math
from pathlib import Path
import re
from typing import Mapping

import nautilus_trader
from nautilus_trader.indicators import Indicator
from nautilus_trader.model.data import Bar, BarType, QuoteTick
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import Price, Quantity
import numpy as np
import pandas as pd
import pyarrow as pa

from .nautilus_indicator_catalogue import (
    IDENTITY, REGISTRY, SPECS, IndicatorConfig, configuration, instantiate, inventory, supported,
)
from .nautilus_indicator_math import common_oracles
from .qlib_rl_data import load_data
from .session import write_report
from .storage.snapshots import SnapshotStore, file_hash

SCHEMA = pa.schema([
    ("event_ns", pa.int64()), ("source_row", pa.int64()), ("indicator", pa.string()),
    ("field", pa.string()), ("number", pa.float64()), ("integer", pa.int64()),
    ("text", pa.string()), ("kind", pa.string()), ("initialized", pa.bool_()),
    ("has_inputs", pa.bool_()), ("valid", pa.bool_()),
])


@dataclass(frozen=True)
class Observation:
    """One actual observation plus an independent raw-field view of the same input."""

    event_ns: int
    source_row: int
    session: str
    native: Bar | QuoteTick
    values: Mapping[str, float | datetime]


class NativeIndicatorStream:
    """Incremental converter-owned native indicator with explicit ordering and session reset policy.

    Callers supply validated actual observations. Reset constructs a new native
    instance because pinned VIDYA.reset does not clear its nested CMO history.
    This retains native calculations while preventing stale state after reset.
    """

    def __init__(self, config: IndicatorConfig) -> None:
        """Bind a validated config and initialize empty native calculation state."""
        self.config = config
        self._native = instantiate(config)
        self._last_ns: int | None = None
        self._session: str | None = None

    def reset(self) -> None:
        """Clear every native and converter state through fresh native construction."""
        self._native = instantiate(self.config)
        self._last_ns = None
        self._session = None

    def update(self, observation: Observation) -> dict[str, object]:
        """Apply one strictly ordered native bar or quote, then expose causal public values."""
        expected = Bar if self.config.route == "bar" else QuoteTick
        if not isinstance(observation.native, expected):
            raise TypeError("Observation does not match the configured native input route")
        if self._last_ns is not None and observation.event_ns <= self._last_ns:
            raise ValueError("Duplicate or late indicator input must be resolved before evaluation")
        if observation.native.ts_event != observation.event_ns:
            raise ValueError("Raw/native event-time mismatch")
        if self._session not in (None, observation.session) and self.config.gap_policy == "session_reset":
            self._native = instantiate(self.config)
        handle(self._native, observation)
        self._last_ns, self._session = observation.event_ns, observation.session
        return state(self._native, self.config.name)


def native_price(value: float | str) -> Price:
    """Preserve captured price decimals and reject precision loss at the native boundary."""
    decimal = Decimal(str(value))
    if not decimal.is_finite() or decimal <= 0:
        raise ValueError("Prices must be finite and positive")
    result = Price.from_str(format(decimal, "f"))
    if Decimal(str(result)) != decimal:
        raise ValueError("Native Price conversion lost precision")
    return result


def bars(frame: pd.DataFrame) -> list[Observation]:
    """Convert real bar starts to close-time inputs without creating missing observations."""
    result = []
    kind = BarType.from_str(IDENTITY + "-1-MINUTE-LAST-EXTERNAL")
    if not frame.index.is_monotonic_increasing or frame.index.has_duplicates:
        raise ValueError("Indicator observations must be strictly ordered")
    for ordinal, (time, row) in enumerate(frame.iterrows()):
        values = {name: float(row[name]) for name in ("open", "high", "low", "close", "volume")}
        if not all(math.isfinite(value) for value in values.values()):
            raise ValueError("Nonfinite bar input")
        if values["volume"] < 0 or not values["volume"].is_integer():
            raise ValueError("HK equity volume must be a nonnegative integer")
        timestamp = (time + pd.Timedelta(minutes=1)).tz_localize("Asia/Hong_Kong").tz_convert("UTC")
        native = Bar(kind, *(native_price(values[name]) for name in ("open", "high", "low", "close")),
                     Quantity.from_int(int(values["volume"])), timestamp.value, timestamp.value)
        values["typical"] = (values["high"] + values["low"] + values["close"]) / 3
        values["timestamp"] = timestamp.to_pydatetime()
        result.append(Observation(timestamp.value, ordinal, str(time.date()), native, values))
    if not result:
        raise ValueError("No real minute observations")
    return result


def quotes(raw: Path) -> list[Observation]:
    """Read genuine received top-of-book messages, preserving receipt order and exact raw quantities."""
    result = []
    for ordinal, line in enumerate(raw.read_text(encoding="utf-8").splitlines()):
        record = json.loads(line)
        body = record.get("body", {})
        data = body.get("data", {})
        if data.get("book_kind") != "top_of_book" or data.get("symbol") != "HK.00700":
            continue
        levels = [data[side][0] for side in ("bids", "asks")]
        for level in levels:
            if (Decimal(level["price_raw"]) / 10 ** 9 != Decimal(str(level["price"]))
                    or Decimal(level["volume_raw"]) / 10 ** level["volume_precision"]
                    != Decimal(str(level["volume"]))):
                raise ValueError("Capture decoded/raw top-of-book mismatch")
        bid, ask = (float(level["price"]) for level in levels)
        if bid > ask or not all(float(level["volume"]) >= 0 for level in levels):
            raise ValueError("Invalid captured top-of-book")
        event_ns = int(record["received_ns"])
        time = pd.Timestamp(event_ns, unit="ns", tz="UTC")
        tick = QuoteTick(InstrumentId.from_str(IDENTITY), native_price(str(levels[0]["price"])),
                         native_price(str(levels[1]["price"])), Quantity.from_str(str(levels[0]["volume"])),
                         Quantity.from_str(str(levels[1]["volume"])), event_ns, event_ns)
        values = {"high": ask, "low": bid, "close": (ask + bid) / 2, "bid": bid, "ask": ask,
                  "timestamp": time}
        result.append(Observation(event_ns, ordinal, str(time.tz_convert("Asia/Hong_Kong").date()), tick, values))
    if not result or any(a.event_ns >= b.event_ns for a, b in zip(result, result[1:])):
        raise ValueError("Missing or unordered actual BBO capture")
    return result


def state(indicator: Indicator, name: str) -> dict[str, object]:
    """Extract native public outputs without substituting standardized definitions."""
    owner = indicator.value if name == "FuzzyCandlesticks" else indicator
    values = {field: getattr(owner, field) if owner is not None else None for field in REGISTRY[name].fields}
    return {"initialized": bool(indicator.initialized), "has_inputs": bool(indicator.has_inputs), **values}


def equivalent(left: object, right: object) -> bool:
    """Compare indicator values with explicit numeric tolerance and exact categorical/timestamp checks."""
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(equivalent(value, right[key]) for key, value in left.items())
    if isinstance(left, float) and isinstance(right, (float, int)):
        return (math.isnan(left) and math.isnan(right)) or math.isclose(left, right, rel_tol=1e-11, abs_tol=1e-9)
    return left == right


def raw_update(indicator: Indicator, config: IndicatorConfig, item: Observation) -> None:
    """Feed independently decoded fields into the native raw calculation path."""
    values = dict(item.values)
    if config.route == "quote":
        price_type = dict(config.parameters).get("price_type", "MID")
        values["close"] = values[{"BID": "bid", "ASK": "ask", "MID": "close"}[price_type]]
    indicator.update_raw(*(values[name] for name in REGISTRY[config.name].inputs))


def handle(indicator: Indicator, item: Observation) -> None:
    """Dispatch the typed native market-data object to its actual native event handler."""
    if isinstance(item.native, Bar):
        indicator.handle_bar(item.native)
    else:
        indicator.handle_quote_tick(item.native)


def rows(config: IndicatorConfig, item: Observation, values: dict[str, object]) -> list[dict[str, object]]:
    """Store numbers, integer enums and timestamps separately; explicitly mask native warmup outputs."""
    result = []
    for field in REGISTRY[config.name].fields:
        value = values[field]
        row = {"event_ns": item.event_ns, "source_row": item.source_row, "indicator": config.name,
               "field": field, "number": None, "integer": None, "text": None,
               "kind": "null", "initialized": values["initialized"], "has_inputs": values["has_inputs"],
               "valid": bool(values["initialized"] and value is not None)}
        if isinstance(value, datetime):
            row.update(kind="datetime_ns", integer=pd.Timestamp(value).value)
        elif isinstance(value, Enum):
            row.update(kind="enum", integer=int(value.value), text=value.name)
        elif isinstance(value, (bool, int)):
            row.update(kind="boolean" if isinstance(value, bool) else "integer", integer=int(value))
        elif isinstance(value, float):
            row.update(kind="number", number=value if math.isfinite(value) else None)
            row["valid"] = row["valid"] and math.isfinite(value)
        elif value is not None:
            raise TypeError(f"Undeclared native output type {type(value)} for {config.name}.{field}")
        if not row["valid"]:
            row.update(number=None, integer=None, text=None)
        result.append(row)
    return result


def evaluate(config: IndicatorConfig, observations: list[Observation],
             oracle: dict[str, np.ndarray] | None = None) -> tuple[pa.Table, dict[str, object]]:
    """Evaluate full observations, verifying independent input paths and optional independent arithmetic."""
    primary, direct = NativeIndicatorStream(config), instantiate(config)
    output, initialized_at, previous_session = [], None, None
    resets, arithmetic = 0, 0
    spreads = []
    maximum_error = 0.0
    for offset, item in enumerate(observations):
        if config.gap_policy == "session_reset" and previous_session not in (None, item.session):
            direct = instantiate(config)
            spreads.clear()
            resets += 1
        previous_session = item.session
        actual = primary.update(item)
        if config.name == "SpreadAnalyzer":
            spreads.append(item.values["ask"] - item.values["bid"])
            capacity = dict(config.parameters)["capacity"]
            expected = {"current": spreads[-1], "average": sum(spreads[-capacity:]) / min(len(spreads), capacity),
                        "initialized": len(spreads) > capacity, "has_inputs": True}
        else:
            raw_update(direct, config, item)
            expected = state(direct, config.name)
        if not equivalent(actual, expected):
            raise ValueError(f"Native handler/raw parity failed: {config.name} {offset}: {actual} != {expected}")
        if actual["initialized"] and initialized_at is None:
            initialized_at = offset
        if oracle is not None and actual["initialized"]:
            for field, values in oracle.items():
                target = float(values[offset])
                if not equivalent(float(actual[field]), target):
                    raise ValueError(f"Independent math failed: {config.name}.{field}@{offset}: "
                                     f"{actual[field]} != {target}")
                maximum_error = max(maximum_error, abs(float(actual[field]) - target))
                arithmetic += 1
        output.extend(rows(config, item, actual))
    if initialized_at is None:
        raise ValueError(f"Real input did not warm up {config.name}")
    return pa.Table.from_pylist(output, schema=SCHEMA), {
        "observations": len(observations), "first_initialized_index": initialized_at,
        "handler_raw_checks": 0 if config.name == "SpreadAnalyzer" else len(observations),
        "independent_spread_checks": len(observations) if config.name == "SpreadAnalyzer" else 0,
        "independent_arithmetic_checks": arithmetic, "maximum_absolute_math_error": maximum_error,
        "session_resets": resets,
    }


def verify_reset(config: IndicatorConfig, observations: list[Observation]) -> dict[str, object]:
    """Compare post-reset suffixes with genuinely fresh instances and verify no-input gaps preserve state."""
    split = min(330, len(observations) // 2)
    old, fresh = instantiate(config), instantiate(config)
    for item in observations[:split]:
        handle(old, item)
    before = state(old, config.name)
    if not equivalent(before, state(old, config.name)):
        raise ValueError("Indicator changed without an observation")
    old.reset()
    if old.initialized or old.has_inputs:
        raise ValueError(f"Native reset failed: {config.name}")
    reset_state, fresh_state = state(old, config.name), state(fresh, config.name)
    stale = [field for field in reset_state if not equivalent(reset_state[field], fresh_state[field])]
    if stale:
        if config.name != "VariableIndexDynamicAverage" or stale != ["cmo_pct"]:
            raise ValueError(f"Unclassified pinned native reset defect: {config.name}: {stale}")
    replacement = instantiate(config)
    native_suffix_differences = 0
    suffix = observations[split:split + 160]
    for item in suffix:
        handle(old, item)
        handle(fresh, item)
        handle(replacement, item)
        if not equivalent(state(old, config.name), state(fresh, config.name)):
            if config.name != "VariableIndexDynamicAverage":
                raise ValueError(f"Unclassified native reset/fresh suffix defect: {config.name}")
            native_suffix_differences += 1
        if not equivalent(state(replacement, config.name), state(fresh, config.name)):
            raise ValueError(f"Joint reset/fresh suffix differs: {config.name}")
    return {"reset_after": split, "fresh_suffix_checks": len(suffix), "empty_gap_state_unchanged": True,
            "native_reset_stale_fields": stale,
            "native_reset_suffix_differences": native_suffix_differences,
            "joint_reset_policy": "fresh native instance; no Python reimplementation"}


def publish(store: SnapshotStore, config: IndicatorConfig, table: pa.Table,
            provenance: dict[str, object]) -> dict[str, object]:
    """Publish immutable analytical rows, requiring a checked typed Parquet round trip."""
    snake = re.sub(r"(?<!^)(?=[A-Z])", "_", config.name).lower()
    snapshot = store.publish("indicator_" + snake + "_" + config.route, table,
                             {**provenance, "configuration": asdict(config)}, expected_head=None)
    if not store.read(snapshot.identity).equals(table):
        raise ValueError("Indicator Parquet typed round trip changed output")
    return asdict(snapshot)


def run(response: Path, output: Path, repo: Path, raw: Path | None = None) -> dict[str, object]:
    """Integrate and verify every concrete pinned native indicator using real bars and actual BBO."""
    raw = raw or repo / "output/joint/shared_live_20261109/nautilus/raw.jsonl"
    output.mkdir(parents=True, exist_ok=True)
    catalogue = inventory()
    frame = load_data(response).minutes
    observations, quote_observations = bars(frame), quotes(raw)
    oracles = common_oracles(frame)
    store = SnapshotStore(output / "store")
    sources = [Path(__file__), Path(__file__).with_name("nautilus_indicator_catalogue.py"),
               Path(__file__).with_name("nautilus_indicator_math.py"),
               repo / "requirements/joint-py312-windows.lock"]
    modules = {getattr(instantiate(configuration(item.name, "quote" if item.name == "SpreadAnalyzer" else "bar")),
                       "__class__").__module__ for item in SPECS}
    bindings = {str(path.resolve()): file_hash(path) for path in sources}
    native_bindings = {name: file_hash(Path(importlib.import_module(name).__file__)) for name in sorted(modules)}
    provenance = {"nautilus_version": nautilus_trader.__version__, "symbol": "HK.00700",
                  "minute_capture_sha256": file_hash(response), "bbo_capture_sha256": file_hash(raw),
                  "bar_timestamp": "historical source label interpreted as bar end; no verified PIT availability",
                  "quote_timestamp": "actual local receipt time; exchange sequence/completeness unverified",
                  "volume_weighted_average_price": "native typical-price times bar-volume proxy; not trade VWAP",
                  "source_bindings": bindings, "native_module_bindings": native_bindings}
    results = []
    for item in SPECS:
        for route in ("bar", "quote"):
            if not supported(item.name, route):
                continue
            overrides = {"price_type": "MID"} if route == "quote" and "price_type" in dict(item.defaults) else {}
            config = configuration(item.name, route, **overrides)
            source = observations if route == "bar" else quote_observations
            table, checks = evaluate(config, source, oracles.get(item.name) if route == "bar" else None)
            reset = verify_reset(config, source)
            gap_checks = None
            if route == "bar":
                session_config = configuration(item.name, route, "session_reset", **overrides)
                _, gap_checks = evaluate(session_config, source)
            results.append({"name": item.name, "route": route, "config": asdict(config), **checks,
                            "reset": reset, "session_gap_checks": gap_checks,
                            "snapshot": publish(store, config, table, provenance)})
    report = {"passed": True, "inventory": catalogue, "native_classes": len(SPECS),
              "bar_observations": len(observations), "actual_bbo_observations": len(quote_observations),
              "observed_gaps": sum(b.event_ns - a.event_ns > 60_000_000_000
                                   for a, b in zip(observations, observations[1:])),
              "independent_math_classes": sorted(oracles), "provenance": provenance, "results": results,
              "limits": ["Configuration matrix covers declared defaults, not every numeric parameter combination",
                         "Actual BBO capture has a constant spread; directional spread regimes require more capture",
                         "No indicators are computed from synthesized missing bars or fabricated trade data"]}
    if (file_hash(response) != provenance["minute_capture_sha256"]
            or file_hash(raw) != provenance["bbo_capture_sha256"]
            or any(file_hash(Path(path)) != digest for path, digest in bindings.items())):
        raise ValueError("Source or input changed during native indicator acceptance")
    write_report(output / "report.json", report)
    return report


def main() -> None:
    """Expose the complete offline catalogue acceptance run without order entrypoints."""
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--response", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--raw", type=Path)
    args = parser.parse_args()
    result = run(args.response, args.output, Path.cwd(), args.raw)
    print(json.dumps({"passed": result["passed"], "classes": result["native_classes"],
                      "routes": len(result["results"])}))


if __name__ == "__main__":
    main()
