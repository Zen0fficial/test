"""Deterministic, trailing OHLCV indicators, computed locally with no extra data.

These are explicitly specified formulas, not a claim of exact Futu chart parity.
Periods count supplied bars, not elapsed days. Missing sessions are not filled.
No prehistory is fetched: changing the input's first bar changes recursive seeds.

SMA uses a full trailing close window. EMA uses alpha=2/(n+1), seeded by the
first n closes' mean. MACD is EMA12-EMA26; its signal is EMA9 of valid DIF values
and its histogram is 2*(DIF-signal). RSI uses Wilder averages of n close changes,
then alpha=1/n; zero gains/losses gives50, zero losses100 and zero gains0.
KDJ uses9-bar RSV=100*(close-lowest_low)/(highest_high-lowest_low), with flat
range RSV50; K=(2*Kprev+RSV)/3, D=(2*Dprev+K)/3, J=3*K-2*D, seeded Kprev=Dprev=50
just before the first full RSV window. Bollinger uses20-bar mean and population
standard deviation (ddof0), bands +/-2 std. ATR uses Wilder smoothing14 of
TR=max(high-low, abs(high-prev_close), abs(low-prev_close)); first TR=high-low.
OBV starts0 and adds/subtracts the current bar's volume for rising/falling close,
or adds0 for unchanged close. Missing volume makes OBV unavailable from that
bar onward; it never substitutes missing volume with zero.

Every output is null until its full seed window is available. Finite, internally
consistent OHLC is mandatory. Zero and negative prices are permitted for assets
such as futures; volume, when present, must be finite and nonnegative.
"""

from collections.abc import Mapping, Sequence
import copy
import datetime as dt
import math
from numbers import Real


MAX_BARS = 100000
DEFAULT_INDICATORS = ("sma", "ema", "macd", "rsi", "kdj", "bollinger", "atr", "obv")
PARAMETERS = {
    "sma": {"periods": [5, 10, 20, 60]},
    "ema": {"periods": [12, 26], "seed": "sma"},
    "macd": {"fast": 12, "slow": 26, "signal": 9, "histogram_multiplier": 2, "seed": "sma"},
    "rsi": {"periods": [6, 12, 24], "smoothing": "wilder", "flat_value": 50},
    "kdj": {"period": 9, "k_smoothing": 3, "d_smoothing": 3, "seed": 50, "flat_rsv": 50},
    "bollinger": {"period": 20, "standard_deviations": 2, "ddof": 0},
    "atr": {"period": 14, "smoothing": "wilder", "first_true_range": "high-low"},
    "obv": {"seed": 0, "missing_volume": "unavailable_from_first_missing_bar"},
}
DEFINITIONS = {
    "sma": "Mean of the last n closes; null before n bars.",
    "ema": "alpha=2/(n+1), SMA seed over first n closes; null before n bars.",
    "macd": "DIF=EMA12-EMA26; signal=EMA9(DIF), SMA seeded; histogram=2*(DIF-signal).",
    "rsi": "Wilder mean of n gains/losses, then (prev*(n-1)+current)/n; 100*gain/(gain+loss); flat=50.",
    "kdj": "RSV=100*(close-lowest9)/(highest9-lowest9), flat=50; K=(2*prevK+RSV)/3; D=(2*prevD+K)/3; J=3*K-2*D; seed K=D=50.",
    "bollinger": "Middle=SMA20(close); upper/lower=middle +/-2*population_std20(close), ddof=0.",
    "atr": "TR=max(high-low, abs(high-prevClose), abs(low-prevClose)); firstTR=high-low; seed mean14 TR then Wilder smoothing.",
    "obv": "Seed0; add/subtract current volume when close rises/falls, unchanged close adds0. Any missing volume makes this and following OBV null.",
}


def validate_selection(selection=None):
    """Return unique supported family names; custom periods are not accepted."""
    if selection is None:
        return list(DEFAULT_INDICATORS)
    if isinstance(selection, (str, bytes)) or not isinstance(selection, Sequence):
        raise ValueError("indicators must be a sequence of indicator family names")
    if not 1 <= len(selection) <= len(DEFAULT_INDICATORS):
        raise ValueError("Select between 1 and 8 indicator families")
    if any(not isinstance(name, str) for name in selection):
        raise ValueError("Supported indicators: " + ", ".join(DEFAULT_INDICATORS))
    aliases = {"ma": "sma", "boll": "bollinger"}
    normalized = [aliases.get(name.lower(), name.lower()) for name in selection]
    if any(name not in DEFAULT_INDICATORS for name in normalized):
        raise ValueError("Supported indicators: " + ", ".join(DEFAULT_INDICATORS))
    return list(dict.fromkeys(normalized))


def _number(value, field, index):
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"Bar {index} {field} must be a finite number")
    try:
        result = float(value)
    except (ValueError, OverflowError) as exc:
        raise ValueError(f"Bar {index} {field} must be a finite number") from exc
    if not math.isfinite(result):
        raise ValueError(f"Bar {index} {field} must be a finite number")
    return result


def _bar_time(row, index):
    if "timestamp_seconds" in row:
        return _number(row["timestamp_seconds"], "timestamp_seconds", index)
    value = row.get("timestamp", row.get("date"))
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"Bar {index} timestamp/date must be an ISO string")
    try:
        if len(value) == 10:
            parsed = dt.datetime.combine(dt.date.fromisoformat(value), dt.time(), dt.timezone.utc)
        else:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError("Timezone required")
        # Avoid platform-specific timestamp failures for legitimate early dates.
        return (parsed - dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)).total_seconds()
    except (ValueError, OverflowError) as exc:
        raise ValueError(f"Bar {index} timestamp/date must be an ISO date or aware timestamp") from exc


def _validated(bars):
    if isinstance(bars, (str, bytes)) or not isinstance(bars, Sequence) or len(bars) > MAX_BARS:
        raise ValueError(f"bars must be a sequence of at most {MAX_BARS} OHLCV mappings")
    prices, volumes, times = [], [], []
    for index, row in enumerate(bars):
        if not isinstance(row, Mapping):
            raise ValueError(f"Bar {index} must be an OHLCV mapping")
        values = [_number(row.get(field), field, index) for field in ("open", "high", "low", "close")]
        opening, high, low, close = values
        if low > high or not low <= opening <= high or not low <= close <= high:
            raise ValueError(f"Bar {index} has inconsistent OHLC bounds")
        volume = None if row.get("volume") is None else _number(row["volume"], "volume", index)
        if volume is not None and volume < 0:
            raise ValueError(f"Bar {index} volume must be nonnegative")
        prices.append(values)
        volumes.append(volume)
        times.append(_bar_time(row, index))
    if any(value is not None for value in times):
        if any(value is None for value in times) or any(a >= b for a, b in zip(times, times[1:])):
            raise ValueError("Bars must have unique, strictly increasing timestamps; missing mixed timestamps are unsupported")
    return prices, volumes, bool(times and times[0] is not None)


def _mean(values):
    # Divide before summation to avoid overflow from large finite same-sign prices.
    return math.fsum(value / len(values) for value in values)


def _ema(values, period):
    result = [None] * len(values)
    if len(values) < period:
        return result
    current = _mean(values[:period])
    result[period - 1] = current
    alpha = 2.0 / (period + 1)
    for index in range(period, len(values)):
        current = (1 - alpha) * current + alpha * values[index]
        result[index] = current
    return result


def compute_indicators(bars, selection=None):
    """Compute selected fixed-parameter families for supplied chronological bars.

    Returns original bar fields plus a nested ``indicators`` dict in each row.
    Input is never sorted, mutated, filled, or fetched. Timestamped inputs must
    be strictly increasing; untimestamped inputs explicitly assume caller order.
    Raises ValueError for malformed input or nonfinite calculations. Missing
    volume is allowed, with explicit OBV nulls and availability metadata.
    """
    selected = validate_selection(selection)
    prices, volumes, order_verified = _validated(bars)
    closes = [values[3] for values in prices]
    count = len(bars)
    columns, required = {}, {}

    def put(name, values, seed_bars):
        if any(value is not None and not math.isfinite(value) for value in values):
            raise ValueError("Indicator calculation overflowed: " + name)
        columns[name], required[name] = values, seed_bars

    if "sma" in selected:
        for period in PARAMETERS["sma"]["periods"]:
            put(f"sma_{period}", [None if i + 1 < period else _mean(closes[i + 1 - period:i + 1])
                                  for i in range(count)], period)
    if "ema" in selected or "macd" in selected:
        fast, slow = _ema(closes, 12), _ema(closes, 26)
        if "ema" in selected:
            put("ema_12", fast, 12)
            put("ema_26", slow, 26)
        if "macd" in selected:
            dif = [None if a is None or b is None else a - b for a, b in zip(fast, slow)]
            signal = [None] * min(25, count) + _ema(dif[25:], 9)
            put("macd_dif", dif, 26)
            put("macd_signal", signal, 34)
            put("macd_histogram", [None if b is None else 2 * (a - b) for a, b in zip(dif, signal)], 34)
    if "rsi" in selected:
        changes = [current - previous for previous, current in zip(closes, closes[1:])]
        for period in PARAMETERS["rsi"]["periods"]:
            values = [None] * count
            if len(changes) >= period:
                gain = _mean([max(change, 0) for change in changes[:period]])
                loss = _mean([max(-change, 0) for change in changes[:period]])
                for index in range(period, count):
                    if index > period:
                        gain = gain * ((period - 1) / period) + max(changes[index - 1], 0) / period
                        loss = loss * ((period - 1) / period) + max(-changes[index - 1], 0) / period
                    values[index] = 50.0 if gain == loss == 0 else 100.0 / (1.0 + loss / gain) if gain else 0.0
            put(f"rsi_{period}", values, period + 1)
    if "kdj" in selected:
        rsvs, ks, ds, js = ([None] * count for _ in range(4))
        k = d = 50.0
        for index in range(8, count):
            high = max(row[1] for row in prices[index - 8:index + 1])
            low = min(row[2] for row in prices[index - 8:index + 1])
            rsv = 50.0 if high == low else 100 * ((closes[index] - low) / (high - low))
            k = (2 * k + rsv) / 3
            d = (2 * d + k) / 3
            rsvs[index], ks[index], ds[index], js[index] = rsv, k, d, 3 * k - 2 * d
        for name, values in (("kdj_rsv", rsvs), ("kdj_k", ks), ("kdj_d", ds), ("kdj_j", js)):
            put(name, values, 9)
    if "bollinger" in selected:
        middle, upper, lower = ([None] * count for _ in range(3))
        for index in range(19, count):
            window = closes[index - 19:index + 1]
            mean = _mean(window)
            std = math.hypot(*((value - mean) / math.sqrt(20) for value in window))
            middle[index], upper[index], lower[index] = mean, mean + 2 * std, mean - 2 * std
        for name, values in (("bollinger_middle", middle), ("bollinger_upper", upper), ("bollinger_lower", lower)):
            put(name, values, 20)
    if "atr" in selected:
        ranges = [max(high - low, abs(high - closes[index - 1]), abs(low - closes[index - 1])) if index else high - low
                  for index, (_, high, low, _) in enumerate(prices)]
        values = [None] * count
        if count >= 14:
            current = _mean(ranges[:14])
            values[13] = current
            for index in range(14, count):
                current = current * (13 / 14) + ranges[index] / 14
                values[index] = current
        put("atr_14", values, 14)
    missing_volume = next((index for index, value in enumerate(volumes) if value is None), None)
    if "obv" in selected:
        values, current = [None] * count, 0.0
        for index, volume in enumerate(volumes):
            if volume is None:
                break
            if index and closes[index] != closes[index - 1]:
                current += volume if closes[index] > closes[index - 1] else -volume
            values[index] = current
        put("obv", values, 1)
    rows = [dict(row, indicators={name: values[index] for name, values in columns.items()})
            for index, row in enumerate(bars)]
    availability = {name: {"valid_count": sum(value is not None for value in values),
                           "null_count": sum(value is None for value in values)} for name, values in columns.items()}
    if "obv" in selected:
        availability["obv"].update(missing_volume_from_index=missing_volume,
                                   reason="missing_volume" if missing_volume is not None else None)
    return {"rows": rows, "count": count, "selected": selected,
            "parameters": copy.deepcopy({name: PARAMETERS[name] for name in selected}),
            "definitions": {name: DEFINITIONS[name] for name in selected},
            "warmup": {name: {"required_bars": period, "seed_index": period - 1} for name, period in required.items()},
            "availability": availability, "prehistory_loaded": False,
            "timestamp_order_verified": order_verified, "missing_sessions_filled": False,
            "futu_chart_parity_verified": False}


def derive_indicator_result(bar_result, selection=None):
    """Wrap computed values with the fetched bars' provenance and coverage limits."""
    from .provider import ProviderError, ProviderResult
    selected = validate_selection(selection)
    if not isinstance(bar_result, ProviderResult) or not isinstance(bar_result.source, dict) or not bar_result.source:
        raise ProviderError("provider_contract_error", "Indicator input lacks bar provenance.", 500)
    if not isinstance(bar_result.data, dict) or "bars" not in bar_result.data:
        raise ProviderError("provider_contract_error", "The provider returned no bars collection.", 500)
    try:
        computed = compute_indicators(bar_result.data["bars"], selected)
    except (ValueError, OverflowError, TypeError) as exc:
        raise ProviderError("invalid_bar_data", "The returned OHLCV bars cannot support these indicator calculations.", 502) from exc
    data = copy.deepcopy({key: value for key, value in bar_result.data.items() if key != "bars"})
    data.update(computed)
    underlying_source = copy.deepcopy(bar_result.source)
    source = {"provider": "local-technical-indicators",
              "kind": "derived_from_Futu_bars" if "futu" in str(underlying_source.get("provider", "")).lower() else "derived_from_provider_bars",
              "computed_locally": True, "native_indicator_retrieval": False,
              "futu_chart_parity_verified": False, "live_push": False,
              "timeframe": data.get("timeframe"), "adjustment": data.get("adjustment"),
              "parameters": copy.deepcopy(computed["parameters"]),
              "warmup": copy.deepcopy(computed["warmup"]), "prehistory_loaded": False,
              "underlying_source": underlying_source}
    warnings = list(bar_result.warnings) + [
        "Indicators are computed locally from the returned bars; exact Futu chart formula and seed parity is unverified.",
        "No extra prehistory was loaded. Warm-up values are null and recursive seeds depend on the first returned bar.",
        "Periods count supplied bars; missing sessions are not filled and the latest candle may be incomplete."]
    if "obv" in computed["availability"] and computed["availability"]["obv"]["reason"]:
        warnings.append("OBV is unavailable from the first missing-volume bar onward; missing volume was not replaced with zero.")
    return ProviderResult(data, source, warnings)
