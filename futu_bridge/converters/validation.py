"""Read-only boundary validation shared by the platform converters.

These helpers do not replace the provider contract or rewrite captured JSON.
"""

import datetime as dt
from decimal import Decimal, InvalidOperation
import re

UTC = dt.timezone.utc
EPOCH = dt.datetime(1970, 1, 1, tzinfo=UTC)
FIELDS = ("open", "high", "low", "close", "volume")


def number(value):
    if isinstance(value, bool) or value is None:
        raise ValueError("Expected a finite decimal")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("Expected a finite decimal") from exc
    if not result.is_finite():
        raise ValueError("Expected a finite decimal")
    return result


def timestamp(value):
    result = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("A timezone-aware provider timestamp is required")
    return result.astimezone(UTC)


def nanoseconds(value):
    delta = value - EPOCH
    return (delta.days * 86400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1000


def bar_end(row, convention):
    if convention not in ("start", "end"):
        raise ValueError("Unknown Futu bar timestamp convention")
    return timestamp(row["timestamp"]) + (dt.timedelta(minutes=1) if convention == "start" else dt.timedelta())


def ticker(symbol):
    if not re.fullmatch(r"US\.[A-Z][A-Z0-9.\-]{0,20}", symbol):
        raise ValueError("This converter currently supports US equity symbols only")
    return symbol[3:]


def bars(manifest, responses):
    if manifest.get("timeframe") != "1m" or manifest.get("bar_timestamp") not in ("start", "end"):
        raise ValueError("Compatibility converters require explicit one-minute timestamp semantics")
    if len(set(manifest["symbols"])) != len(manifest["symbols"]):
        raise ValueError("Duplicate symbols")
    for index, symbol in enumerate(manifest["symbols"]):
        ticker(symbol)
        response = responses[f"bars_{index}"]
        data = response["data"]
        if response["source"].get("provider") != "futu-desktop-session":
            raise ValueError("Expected real Futu desktop data, not samples")
        if data.get("symbol") != symbol or data.get("timeframe") != "1m" or data.get("adjustment") != "none":
            raise ValueError("Unexpected symbol, timeframe or price adjustment")
        if data.get("session") != "regular" or data.get("truncated") or data.get("complete_requested_range") is not True:
            raise ValueError("Require an untruncated regular-session response for the requested range")
        rows = data["bars"]
        if not rows:
            raise ValueError("Empty Futu response")
        previous = None
        for row in rows:
            end = bar_end(row, manifest["bar_timestamp"])
            if not manifest["start"] <= row.get("date", "") <= manifest["end"]:
                raise ValueError("Futu bar date is outside the capture window")
            if previous is not None and end <= previous:
                raise ValueError("Duplicate or unsorted Futu bars")
            previous = end
            values = {key: number(row[key]) for key in FIELDS}
            if min(values.values()) < 0 or min(values[k] for k in FIELDS[:4]) <= 0:
                raise ValueError("Invalid OHLCV sign")
            if values["low"] > min(values["open"], values["close"]) or values["high"] < max(values["open"], values["close"]):
                raise ValueError("Invalid OHLC range")
        yield symbol, response


def probabilities(manifest, responses):
    first = timestamp(manifest["start"] + "T00:00:00Z")
    last = timestamp(manifest["end"] + "T00:00:00Z") + dt.timedelta(days=1)
    tokens = set()
    for outcome in manifest["outcomes"]:
        token = outcome["token_id"]
        if not isinstance(token, str) or not token.isdigit() or token in tokens:
            raise ValueError("Unique exact-string outcome token IDs are required")
        tokens.add(token)
        response = responses[outcome["name"]]
        if response["source"].get("provider") != "polymarket":
            raise ValueError("Expected the existing Polymarket provider")
        params = response["source"].get("request_params", {})
        if params.get("token_id") != token:
            raise ValueError("History token identity mismatch")
        payload = response["data"]
        pagination = payload.get("pagination", {})
        if pagination.get("has_more") is not False:
            raise ValueError("Unfinished or unknown Polymarket pagination")
        previous = None
        rows = []
        for row in payload["data"]:
            seconds = number(row["timestamp"])
            width = number(row["resolution_seconds"])
            if seconds != seconds.to_integral_value() or width < 0 or width != width.to_integral_value():
                raise ValueError("Invalid historical bucket timestamp/resolution")
            event_time = EPOCH + dt.timedelta(seconds=int(seconds))
            if previous is not None and event_time <= previous:
                raise ValueError("Duplicate or unsorted probability history")
            previous = event_time
            if not 0 <= number(row["price"]) <= 1:
                raise ValueError("Probability price must be in [0, 1]")
            if first <= event_time < last:
                # Conservative replay convention: deliver a bucket at its end.
                # This is not proof of the original real-time availability.
                rows.append((row, event_time + dt.timedelta(seconds=int(width))))
        if not rows:
            raise ValueError("No probability observations in the requested window")
        yield outcome, rows


def expected_rows(manifest, responses):
    """Independent, exact-decimal comparison keys for engine callback output."""
    result = []
    for symbol, response in bars(manifest, responses):
        for row in response["data"]["bars"]:
            result.append(["bar", symbol, str(nanoseconds(bar_end(row, manifest["bar_timestamp"]))),
                           *[str(number(row[field])) for field in FIELDS]])
    for outcome, rows in probabilities(manifest, responses):
        for row, available in rows:
            result.append(["probability", outcome["token_id"], str(nanoseconds(available)), str(number(row["price"]))])
    return result


def compare_rows(actual, expected):
    def canonical(row):
        return tuple(row[:3]) + tuple(number(v) for v in row[3:])
    actual_rows, expected_values = sorted(map(canonical, actual)), sorted(map(canonical, expected))
    if actual_rows != expected_values:
        mismatch = next(((a, e) for a, e in zip(actual_rows, expected_values) if a != e), None)
        raise ValueError(f"Engine data mismatch: received={len(actual_rows)}, expected={len(expected_values)}, first={mismatch}")
    return {"exact_decimal_match": True, "rows": len(actual_rows),
            "bars": sum(r[0] == "bar" for r in actual_rows),
            "probabilities": sum(r[0] == "probability" for r in actual_rows)}
