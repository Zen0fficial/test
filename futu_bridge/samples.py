"""Explicit access to installed Futu's decoded, bundled AAPL example data."""

import datetime as dt
import hashlib
import json
from pathlib import Path
from typing import Optional

from .provider import ProviderError, ProviderResult


DEFAULT_SAMPLE = Path(__file__).resolve().parent.parent / "coverage_audit" / "decoded_chart_sample.json"


class SampleBars:
    def __init__(self, path: Path = DEFAULT_SAMPLE):
        self.path = Path(path)

    def metadata(self):
        if not self.path.is_file():
            return {"available": False, "reason": "Decoded bundled sample file is missing."}
        raw = self.path.read_bytes()
        value = json.loads(raw)
        bars = value["bars"]
        return {"available": True, "symbol": value["symbol"], "timeframe": "1d",
                "first_date": bars[0]["date"] if bars else None,
                "last_date": bars[-1]["date"] if bars else None,
                "count": len(bars), "kind": "sample", "live": False,
                "sha256": hashlib.sha256(raw).hexdigest()}

    def bars(self, symbol: str, timeframe: str, start: Optional[str],
             end: Optional[str]) -> ProviderResult:
        if symbol.upper() not in ("AAPL", "US.AAPL"):
            raise ProviderError("sample_symbol_unavailable",
                                "The decoded bundled equity sample only contains AAPL.", 404)
        if timeframe not in ("1d", "1Day"):
            raise ProviderError("sample_timeframe_unavailable",
                                "The decoded bundled sample only contains daily bars.", 422)
        if not self.path.is_file():
            raise ProviderError("sample_unavailable", "Decoded bundled sample file is missing.", 503)
        raw = self.path.read_bytes()
        value = json.loads(raw)
        def day(text):
            return dt.date.fromisoformat(text[:10]) if text else None
        first, last = day(start), day(end)
        rows = [bar for bar in value["bars"]
                if (first is None or day(bar["date"]) >= first)
                and (last is None or day(bar["date"]) <= last)]
        return ProviderResult(
            {"symbol": value["symbol"], "timeframe": "1d", "currency": value["currency"],
             "adjustment": value["adjustment"], "count": len(rows), "bars": rows},
            {"provider": "futu-bundled-quant-example", "kind": "sample", "live": False,
             "description": value["source_class"], "origin": value["source"],
             "decoded_file_sha256": hashlib.sha256(raw).hexdigest()},
            ["Bundled offline data, not a live chart download or entitlement test.",
             "Prices are raw/unadjusted (inferred); account for corporate actions before backtesting."])
