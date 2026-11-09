"""Immutable, exact-decimal contracts at the research boundary."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from futu_bridge.converters.capture import load_capture
from futu_bridge.converters.validation import bar_end, bars, nanoseconds, probabilities


@dataclass(frozen=True, slots=True)
class Bar:
    """A completed bar; availability is an explicit research convention in UTC ns."""

    symbol: str
    available_ns: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    def __post_init__(self) -> None:
        """Reject invalid identities, timestamps, numbers, and OHLC ranges."""
        values = (self.open, self.high, self.low, self.close, self.volume)
        if not self.symbol or type(self.available_ns) is not int or self.available_ns < 0:
            raise ValueError("Invalid bar identity/time")
        if not all(isinstance(v, Decimal) and v.is_finite() for v in values):
            raise ValueError("OHLCV must be finite Decimals")
        if min(values[:4]) <= 0 or self.volume < 0:
            raise ValueError("Invalid OHLCV signs")
        if self.low > min(self.open, self.close) or self.high < max(self.open, self.close):
            raise ValueError("Invalid OHLC range")


@dataclass(frozen=True, slots=True)
class Probability:
    """One exact outcome token, event time, and conservative availability time."""

    token: str
    event_ns: int
    available_ns: int
    value: Decimal

    def __post_init__(self) -> None:
        """Validate identity and prohibit a point becoming available before its event."""
        if not self.token.isascii() or not self.token.isdigit():
            raise ValueError("Outcome token must be an exact ASCII digit string")
        if (type(self.event_ns) is not int or type(self.available_ns) is not int
                or not 0 <= self.event_ns <= self.available_ns):
            raise ValueError("Invalid probability event/availability times")
        if not isinstance(self.value, Decimal) or not self.value.is_finite() or not 0 <= self.value <= 1:
            raise ValueError("Probability must be a finite Decimal in [0, 1]")


@dataclass(frozen=True, slots=True)
class Dataset:
    """Validated snapshot; historical availability is never asserted as observed."""

    bars: tuple[Bar, ...]
    probabilities: tuple[Probability, ...]
    capture_sha256: str
    availability: str = "retrospective_bucket_end"

    def __post_init__(self) -> None:
        """Require a nonempty equity snapshot and explicit retrospective provenance."""
        if not self.bars:
            raise ValueError("Research dataset must contain bars")
        if len(self.capture_sha256) != 64 or any(c not in "0123456789abcdef" for c in self.capture_sha256):
            raise ValueError("Expected a SHA-256 capture digest")
        if self.availability != "retrospective_bucket_end":
            raise ValueError("This capture adapter cannot certify observed historical availability")

    @classmethod
    def from_capture(cls, path: Path) -> Dataset:
        """Read the existing immutable capture and verify every original response hash.

        The old converter validators enforce pagination, symbols, raw prices,
        monotonicity, and explicit minute-bar timestamp interpretation.
        """
        manifest, responses = load_capture(path)
        bar_rows: list[Bar] = []
        points: list[Probability] = []
        for symbol, response in bars(manifest, responses):
            for row in response["data"]["bars"]:
                bar_rows.append(Bar(
                    symbol, nanoseconds(bar_end(row, manifest["bar_timestamp"])),
                    *(Decimal(str(row[field])) for field in ("open", "high", "low", "close", "volume")),
                ))
        for outcome, rows in probabilities(manifest, responses):
            for row, available in rows:
                points.append(Probability(
                    outcome["token_id"], int(row["timestamp"]) * 1_000_000_000,
                    nanoseconds(available), Decimal(str(row["price"])),
                ))
        return cls(tuple(bar_rows), tuple(points), hashlib.sha256((path / "manifest.json").read_bytes()).hexdigest())
