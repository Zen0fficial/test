"""Causal cross-provider joins with explicit staleness and no interpolation."""

from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass

from .contracts import Bar, Probability


@dataclass(frozen=True, slots=True)
class JoinedPoint:
    """A bar with one outcome's most recent eligible observation, if fresh."""

    bar: Bar
    token: str
    probability: Probability | None
    age_ns: int | None


def align(
    bars: tuple[Bar, ...], points: tuple[Probability, ...], *, max_age_ns: int,
) -> tuple[JoinedPoint, ...]:
    """Join on availability <= bar close and age <= max_age_ns, inclusive.

    Returns a row for every bar/token pair. Missing or stale values remain
    None. Duplicate availability per token is rejected instead of selecting an
    arbitrary revision. Input order is preserved for bars, tokens sort by ID.
    """
    if type(max_age_ns) is not int or max_age_ns < 0:
        raise ValueError("max_age_ns must be a nonnegative integer")
    grouped: dict[str, list[Probability]] = defaultdict(list)
    identities: set[tuple[str, int]] = set()
    for point in points:
        key = (point.token, point.available_ns)
        if key in identities:
            raise ValueError("Ambiguous duplicate outcome availability")
        identities.add(key)
        grouped[point.token].append(point)
    result: list[JoinedPoint] = []
    seen_bars: set[tuple[str, int]] = set()
    ordered = {token: sorted(rows, key=lambda p: p.available_ns) for token, rows in grouped.items()}
    times = {token: [p.available_ns for p in rows] for token, rows in ordered.items()}
    for bar in bars:
        identity = (bar.symbol, bar.available_ns)
        if identity in seen_bars:
            raise ValueError("Duplicate bar identity")
        seen_bars.add(identity)
        for token in sorted(ordered):
            index = bisect_right(times[token], bar.available_ns) - 1
            point = ordered[token][index] if index >= 0 else None
            age = bar.available_ns - point.available_ns if point else None
            if age is not None and age > max_age_ns:
                point, age = None, None
            result.append(JoinedPoint(bar, token, point, age))
    return tuple(result)
