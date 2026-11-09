"""Exact reference features for checking optimized framework implementations."""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from .contracts import Bar

FEATURE_NAMES = ("return_1", "close_to_mean_5", "range_to_close")


def reference_features(bars: tuple[Bar, ...]) -> list[dict[str, object]]:
    """Calculate trailing features independently per symbol and trading day.

    All inputs are completed minute bars. A gap greater than one minute resets
    the window, avoiding implicit overnight or missing-bar returns. Warm-up is
    represented by None, never by a zero or a future-filled value.
    """
    histories: dict[str, list[Bar]] = defaultdict(list)
    result: list[dict[str, object]] = []
    for bar in sorted(bars, key=lambda b: (b.symbol, b.available_ns)):
        history = histories[bar.symbol]
        if history and bar.available_ns <= history[-1].available_ns:
            raise ValueError("Duplicate/nonincreasing bar time")
        if history and bar.available_ns - history[-1].available_ns != 60_000_000_000:
            history.clear()
        previous = history[-1].close if history else None
        history.append(bar)
        del history[:-5]
        mean = sum((b.close for b in history), Decimal(0)) / 5 if len(history) == 5 else None
        result.append({
            "symbol": bar.symbol, "available_ns": bar.available_ns,
            "return_1": float(bar.close / previous - 1) if previous else None,
            "close_to_mean_5": float(bar.close / mean - 1) if mean else None,
            "range_to_close": float((bar.high - bar.low) / bar.close),
        })
    return result
