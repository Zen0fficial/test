"""Equivalent-workload benchmarks and verified feature implementation selection.

The merged NumPy implementation retains the joint gap/warmup contract while
computing the three simple expressions together. Native Qlib remains available
for its broader expression language and its existing compatible binary storage.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
import hashlib
import json
import os
from importlib.metadata import version
from pathlib import Path
import platform
import statistics
import time

from .contracts import Bar, Dataset
from .features import FEATURE_NAMES, reference_features
from .session import write_report


def fingerprint() -> dict[str, object]:
    """Bind measured choices to code, pinned runtimes and the machine that was timed."""
    root = Path(__file__).parent
    return {"code": {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                     for name in ("performance.py", "features.py", "contracts.py", "qlib_features_models.py")},
            "versions": {name: version(name) for name in ("numpy", "pandas", "pyqlib")},
            "python": platform.python_version(),
            "lock_sha256": hashlib.sha256((root.parents[1] /
                                            "requirements/joint-py312-windows.lock").read_bytes()).hexdigest(),
            "threads": {name: os.environ.get(name) for name in
                        ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "LOKY_MAX_CPU_COUNT")},
            "machine": hashlib.sha256(platform.node().encode()).hexdigest()}


def numpy_features(bars: tuple[Bar, ...]) -> list[dict[str, object]]:
    """Fuse three expressions while resetting windows at each symbol or minute gap."""
    import numpy as np

    ordered = sorted(bars, key=lambda b: (b.symbol, b.available_ns))
    groups: list[list[Bar]] = []
    for bar in ordered:
        prior = groups[-1][-1] if groups else None
        if prior and bar.symbol == prior.symbol and bar.available_ns <= prior.available_ns:
            raise ValueError("Duplicate/nonincreasing feature timestamp")
        if prior is None or bar.symbol != prior.symbol or bar.available_ns - prior.available_ns != 60_000_000_000:
            groups.append([])
        groups[-1].append(bar)
    result = []
    for group in groups:
        values = np.array([[float(b.close), float(b.high), float(b.low)] for b in group])
        if not np.isfinite(values).all() or np.any(values <= 0):
            raise ValueError("Feature prices cannot be represented as finite positive float64")
        close = values[:, 0]
        returns = np.full(len(close), np.nan)
        returns[1:] = close[1:] / close[:-1] - 1
        means = np.full(len(close), np.nan)
        if len(close) >= 5:
            # A fixed-width convolution avoids cancellation from long cumulative sums.
            means[4:] = close[4:] / (np.convolve(close, np.ones(5), mode="valid") / 5) - 1
        ranges = (values[:, 1] - values[:, 2]) / close
        for index, bar in enumerate(group):
            result.append({"symbol": bar.symbol, "available_ns": bar.available_ns,
                           "return_1": None if index == 0 else float(returns[index]),
                           "close_to_mean_5": None if index < 4 else float(means[index]),
                           "range_to_close": float(ranges[index])})
    return result


def compare(reference: list[dict[str, object]], actual: list[dict[str, object]], tolerance: float) -> float:
    """Require identical identities, ordering and missing masks before checking values."""
    import math

    if len(reference) != len(actual):
        raise ValueError("Feature benchmark row-count mismatch")
    error = 0.
    for expected, observed in zip(reference, actual):
        if any(expected[k] != observed.get(k) for k in ("symbol", "available_ns")):
            raise ValueError("Feature benchmark identity mismatch")
        for name in FEATURE_NAMES:
            value = observed.get(name)
            if expected[name] is None:
                if value is not None:
                    raise ValueError("Feature benchmark warmup/gap mask mismatch")
            elif value is None or not math.isfinite(value):
                raise ValueError("Feature benchmark omitted a finite value")
            else:
                error = max(error, abs(value - expected[name]))
    if error > tolerance:
        raise ValueError(f"Feature benchmark correctness failed: {error} > {tolerance}")
    return error


def qlib_callable(dataset: Dataset, storage: Path) -> Callable[[], list[dict[str, object]]]:
    """Prepare native Qlib expressions with the same explicit session/gap mask."""
    import qlib
    from qlib.config import REG_US
    from qlib.data import D

    qlib.init(provider_uri=str(storage.resolve()), region=REG_US, expression_cache=None, dataset_cache=None, kernels=1)
    expected = reference_features(dataset.bars)
    symbols = sorted({bar.symbol[3:] for bar in dataset.bars})

    def evaluate() -> list[dict[str, object]]:
        """Include native query, identity reconstruction and common output materialization."""
        frame = D.features(symbols, ["$close/Ref($close,1)-1", "$close/Mean($close,5)-1",
                                    "($high-$low)/$close"], freq="1min")
        values = {(f"US.{symbol}", int(stamp.value)): row for (symbol, stamp), row in frame.iterrows()}
        rows = []
        for reference in expected:
            native = values[(reference["symbol"], reference["available_ns"])]
            rows.append({"symbol": reference["symbol"], "available_ns": reference["available_ns"],
                         **{name: None if reference[name] is None else float(native.iloc[index])
                            for index, name in enumerate(FEATURE_NAMES)}})
        return rows

    return evaluate


def benchmark(operations: dict[str, Callable[[], list]], expected: list, repeats: int = 11) -> dict[str, object]:
    """Time correctness-qualified candidates in rotated order after two warmup calls."""
    if repeats < 5 or not operations:
        raise ValueError("Benchmarks require candidates and at least five measured repetitions")
    values = {name: [] for name in operations}
    errors = {}
    for name, operation in operations.items():
        operation()
        errors[name] = compare(expected, operation(), 2e-6 if name == "qlib" else 1e-12)
    names = list(operations)
    for iteration in range(repeats):
        order = names[iteration % len(names):] + names[:iteration % len(names)]
        for name in order:
            started = time.perf_counter_ns()
            actual = operations[name]()
            elapsed = (time.perf_counter_ns() - started) / 1e9
            compare(expected, actual, 2e-6 if name == "qlib" else 1e-12)
            values[name].append(elapsed)
    timings = {name: {"samples_seconds": samples, "median_seconds": statistics.median(samples),
                      "min_seconds": min(samples), "max_seconds": max(samples),
                      "max_absolute_error": errors[name]} for name, samples in values.items()}
    ordered = sorted(timings, key=lambda name: timings[name]["median_seconds"])
    winner = ordered[0]
    advantage = timings[ordered[1]]["median_seconds"] / timings[winner]["median_seconds"] if len(ordered) > 1 else 1.
    return {"passed": True, "candidates": timings, "selected": winner,
            "runner_up_ratio": advantage, "repeats": repeats, "warmups": 2,
            "reason": "Lowest measured median among correctness-qualified equivalent feature outputs",
            "scope": "Warm same-process complete output; storage construction and process startup excluded"}


def choose(profile: Path, dataset: Dataset) -> str:
    """Accept a measured choice only for unchanged source, implementation and feature contract."""
    report = json.loads(profile.read_text(encoding="utf-8"))
    selected = report.get("selected")
    if (report.get("passed") is not True or report.get("capture_sha256") != dataset.capture_sha256
            or report.get("fingerprint") != fingerprint() or selected not in ("numpy", "reference", "qlib")
            or selected not in report.get("candidates", {}) or report.get("repeats", 0) < 5):
        raise ValueError("Performance selection is stale, unverified or for a different capture")
    scores = report["candidates"]
    if selected != min(scores, key=lambda name: scores[name]["median_seconds"]):
        raise ValueError("Performance profile did not select its measured winner")
    for name, score in scores.items():
        samples = score["samples_seconds"]
        if (len(samples) != report["repeats"] or any(v <= 0 for v in samples)
                or statistics.median(samples) != score["median_seconds"]
                or not 0 <= score["max_absolute_error"] <= (2e-6 if name == "qlib" else 1e-12)):
            raise ValueError("Performance profile contains inconsistent timings or unqualified outputs")
    return selected


def benchmark_processor(source: Path, output: Path, repeats: int = 11) -> dict[str, object]:
    """Compare native and faithful merged infinity handling on a real Alpha158 slice."""
    import numpy as np
    import pandas as pd
    from pandas.testing import assert_frame_equal
    from qlib.data.dataset.processor import ProcessInf
    from .qlib_features_models import FiniteByDate

    if repeats < 11:
        raise ValueError("Processor benchmarks require at least eleven repetitions")
    frame = pd.read_parquet(source).iloc[-64:]
    expected = ProcessInf()(frame.copy())
    functions = {"native_qlib": ProcessInf(), "merged_vectorized": FiniteByDate()}
    times = {name: [] for name in functions}
    for operation in functions.values():
        assert_frame_equal(expected, operation(frame.copy()), check_exact=True)
    names = list(functions)
    for repeat in range(repeats + 2):
        for name in names[repeat % 2:] + names[:repeat % 2]:
            started = time.perf_counter_ns()
            actual = functions[name](frame.copy())
            elapsed = (time.perf_counter_ns() - started) / 1e9
            if repeat >= 2:
                times[name].append(elapsed)
            assert_frame_equal(expected, actual, check_exact=True)
    medians = {k: statistics.median(v) for k, v in times.items()}
    winner = min(medians, key=medians.get)
    report = {"passed": True, "rows": len(frame), "columns": len(frame.columns),
              "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "warmups": 2,
              "repeats": repeats, "fingerprint": fingerprint(), "real_orders_enabled": False,
              "source_infinities": int(np.isinf(frame.to_numpy()).sum()), "selected": winner,
              "samples_seconds": times, "median_seconds": medians,
              "speedup": max(medians.values()) / min(medians.values()),
              "semantics": "Only infinities replaced by same-date finite mean; existing NaNs retained",
              "scope": "Real final 64 Alpha158 rows; equal input copy/output contract; no model-quality claim"}
    write_report(output, report)
    return report


def run(capture: Path, storage: Path, output: Path) -> dict[str, object]:
    """Benchmark the three overlapping implementations on the same real captured bars."""
    dataset = Dataset.from_capture(capture)
    output = output.resolve()
    if output == capture.resolve() or capture.resolve() in output.parents:
        raise ValueError("Benchmark output must be outside its source capture")
    output.mkdir(parents=True, exist_ok=False)
    expected = reference_features(dataset.bars)
    native = qlib_callable(dataset, storage)
    report = benchmark({"reference": lambda: reference_features(dataset.bars),
                        "numpy": lambda: numpy_features(dataset.bars), "qlib": native}, expected)
    report.update(capture_sha256=dataset.capture_sha256, bars=len(dataset.bars),
                  fingerprint=fingerprint(),
                  real_orders_enabled=False, providers_modified=False)
    write_report(output / "report.json", report)
    return report


def main() -> None:
    """Run the benchmark in an isolated shared-Python worker."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--storage", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.capture, args.storage, args.output), indent=2))


if __name__ == "__main__":
    main()
