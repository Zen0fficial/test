"""Isolated Qlib expression execution, checked against exact source features."""

from __future__ import annotations

import argparse
import json
import math
import time
from pathlib import Path

from .contracts import Dataset
from .features import FEATURE_NAMES, reference_features


def run(capture: Path, storage: Path, output: Path, backend: str = "qlib") -> None:
    """Evaluate Qlib expressions; reject missing rows or excessive float32 error."""
    dataset = Dataset.from_capture(capture)
    expected = reference_features(dataset.bars)
    if backend in ("numpy", "reference"):
        from .performance import compare, numpy_features

        started = time.perf_counter()
        rows = numpy_features(dataset.bars) if backend == "numpy" else reference_features(dataset.bars)
        elapsed = time.perf_counter() - started
        error = compare(expected, rows, 1e-12)
        output.write_text(json.dumps({"rows": rows, "max_absolute_error": error, "backend": backend,
                          "absolute_tolerance": 1e-12, "expression_seconds": elapsed}, allow_nan=False),
                          encoding="utf-8")
        return
    if backend != "qlib":
        raise ValueError("Unknown feature implementation")
    import qlib
    from qlib.config import REG_US
    from qlib.data import D

    qlib.init(provider_uri=str(storage.resolve()), region=REG_US,
              expression_cache=None, dataset_cache=None)
    started = time.perf_counter()
    frame = D.features(
        sorted({b.symbol[3:] for b in dataset.bars}),
        ["$close/Ref($close,1)-1", "$close/Mean($close,5)-1", "($high-$low)/$close"],
        freq="1min",
    )
    elapsed = time.perf_counter() - started
    actual = {(f"US.{symbol}", int(stamp.value)): values
              for (symbol, stamp), values in frame.iterrows()}
    rows: list[dict[str, object]] = []
    max_error = 0.0
    for reference in expected:
        key = (str(reference["symbol"]), int(str(reference["available_ns"])))
        if key not in actual:
            raise ValueError(f"Qlib omitted feature row {key}")
        values = actual[key]
        row: dict[str, object] = {"symbol": key[0], "available_ns": key[1]}
        for i, name in enumerate(FEATURE_NAMES):
            # Session/gap warm-up is a common policy, not Qlib's cross-gap Ref.
            if reference[name] is None:
                row[name] = None
                continue
            value = float(values.iloc[i])
            error = abs(value - float(str(reference[name])))
            if not math.isfinite(value) or error > 2e-6:
                raise ValueError(f"Qlib feature differs from exact source: {key} {name}")
            max_error = max(error, max_error)
            row[name] = value
        rows.append(row)
    output.write_text(json.dumps({"rows": rows, "max_absolute_error": max_error,
                                 "absolute_tolerance": 2e-6, "expression_seconds": elapsed},
                                allow_nan=False, indent=2), encoding="utf-8")


def main() -> None:
    """Execute only inside the Qlib environment selected by the coordinator."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--storage", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--backend", choices=("qlib", "numpy", "reference"), default="qlib")
    args = parser.parse_args()
    run(args.capture, args.storage, args.output, args.backend)


if __name__ == "__main__":
    main()
