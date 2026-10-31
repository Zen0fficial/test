"""Observed Futu 16.33 native section mapping, without clock-based inference.

Request enums come from the installed descriptor. Section IDs12/21/22/23/24 are
observed responses (the bundled descriptor's old TradeSection list stops at11).
Mapping is deliberately limited to the tested instrument classes; unknown
section IDs cause explicit selection to fail rather than being dropped silently.
"""
from collections import Counter
from copy import deepcopy
from .provider import ProviderError, ProviderResult

SESSION_PROBS = {
    "provider_default": (0,), "regular": (0,), "premarket": (1,),
    "postmarket": (1,), "extended": (1,), "overnight": (2,), "all": (1, 2),
}
SECTIONS = {
    "us_stock": {12: "regular", 5: "premarket", 6: "postmarket", 11: "overnight"},
    # Observed US option regimes differ: SPCX uses21 for regular-only bars;
    # MSFT/AAPL extended-session responses use22/23/24. Preserve both raw IDs.
    "us_equity_option": {21: "regular", 22: "regular", 23: "premarket", 24: "postmarket"},
}

def validate_session(session, timeframe):
    if not isinstance(session, str) or session not in SESSION_PROBS:
        raise ValueError("session must be provider_default, regular, premarket, postmarket, extended, overnight, or all")
    if session != "provider_default" and timeframe not in ("1m", "3m", "5m", "15m", "30m", "1h"):
        raise ValueError("Explicit session selection requires intraday candles")
    return session

def classify_record(record):
    if record.get("instrument_type_v2") == 3 and record.get("cs_market") == 2:
        return "us_stock"
    if (record.get("cs_market") == 7 and record.get("side") in ("call", "put") and
            record.get("identity_status") == "native_metadata_verified"):
        return "us_equity_option"
    return None

def _previous_close_context_only(previous, incoming):
    """Allow only a paired native11/normalized previous-close difference.

    A union can place a different preceding section before the same regular
    bar. Every other native/normalized field must still match exactly.
    """
    before = deepcopy(previous)
    after = deepcopy(incoming)
    if ("previous_close" not in before or "previous_close" not in after or
            before["previous_close"] == after["previous_close"]):
        return False
    before_raw = before.get("raw_integer_fields")
    after_raw = after.get("raw_integer_fields")
    if (not isinstance(before_raw, dict) or not isinstance(after_raw, dict) or
            "11" not in before_raw or "11" not in after_raw or before_raw["11"] == after_raw["11"]):
        return False
    # The pair must be internally consistent with the parser's fixed price
    # scale, so a mutated normalized field cannot hide behind an unrelated raw
    # difference. None/sentinels are not accepted as this narrow exception.
    for row, raw in ((before, before_raw), (after, after_raw)):
        value = raw["11"]
        if type(value) is not int or type(row["previous_close"]) not in (int, float):
            return False
        signed_value = value - (1 << 64) if value >= 1 << 63 else value
        if signed_value / 1_000_000_000 != row["previous_close"]:
            return False
    before.pop("previous_close")
    after.pop("previous_close")
    before_raw.pop("11")
    after_raw.pop("11")
    return before == after

def filter_session(results, session, kind):
    selected = ({"premarket", "regular", "postmarket"} if session == "extended" else
                {"premarket", "regular", "postmarket", "overnight"} if session == "all" else {session})
    mapping = SECTIONS[kind]
    merged = {}
    origins = {}
    unknown = set()
    for result in results:
        for row in result.data["bars"]:
            raw = row.get("trade_section")
            if raw not in mapping:
                unknown.add(raw)
                continue
            stamp = row["timestamp_seconds"]
            if stamp in merged:
                # Compare every retained native/normalized field, including
                # unknown future fields. A derived session label is the only
                # excluded field. In particular previous_close can differ at
                # feed boundaries, so silently preferring the first row would
                # lose real provider context even when OHLCV agrees.
                stored = merged[stamp]
                variants = stored.get("native_session_feed_variants")
                original = variants[0]["bar"] if variants else stored
                previous = {key: value for key, value in original.items() if key != "session"}
                incoming = {key: value for key, value in row.items() if key != "session"}
                if previous != incoming:
                    if _previous_close_context_only(previous, incoming):
                        if not variants:
                            variants = [{"native_kline_type_prob": origins[stamp], "bar": deepcopy(original)}]
                            stored["native_session_feed_variants"] = variants
                        variants.append({"native_kline_type_prob": result.data["native_kline_type_prob"],
                                         "bar": deepcopy(row)})
                        stored["previous_close"] = None
                        stored["previous_close_ambiguous"] = True
                        stored["raw_integer_fields"].pop("11", None)
                        stored["ambiguous_native_integer_fields"] = ["11"]
                    else:
                        differing = sorted(key for key in previous.keys() | incoming.keys()
                                           if key not in previous or key not in incoming or previous[key] != incoming[key])
                        raise ProviderError("session_feed_conflict", "Overlapping native session feeds disagree on retained candle fields.", 502,
                                            {"timestamp_seconds": stamp, "differing_fields": differing,
                                             "native_kline_type_probs": [item.data["native_kline_type_prob"] for item in results]})
            else:
                merged[stamp] = deepcopy(row)
                origins[stamp] = result.data["native_kline_type_prob"]
    if unknown:
        raise ProviderError("session_section_unverified", "The response contains native section IDs without a verified session mapping.", 502,
                            {"native_section_ids": sorted(unknown, key=lambda value: str(value))})
    data = deepcopy(results[0].data)
    bars = []
    observed = Counter()
    for _, row in sorted(merged.items()):
        label = mapping[row["trade_section"]]
        observed[label] += 1
        if label in selected:
            row["session"] = label
            bars.append(row)
    metadata = {"session": session, "session_hours_verified": False,
                "session_filter_verified": True, "session_filter_method": "native_trade_section",
                "section_enum_labels_verified": False,
                "session_mapping_basis": "observed_provider_sections",
                "session_mapping_provenance": "observed_native_sections_16.33.17808",
                "native_section_mapping": {str(key): value for key, value in mapping.items()},
                "native_kline_type_prob": (SESSION_PROBS[session][0] if len(SESSION_PROBS[session]) == 1 else None),
                "native_kline_type_probs": list(SESSION_PROBS[session]),
                "ambiguous_previous_close_count": sum(bool(row.get("previous_close_ambiguous")) for row in bars),
                "previous_close_merge_policy": "null_ambiguous_value_preserve_complete_native_feed_variants"}
    data.update(metadata)
    data.update(bars=bars, count=len(bars), session_counts_before_filter=dict(observed),
                session_rows_excluded=len(merged)-len(bars),
                requests=[item for result in results for item in result.data["requests"]],
                truncated=any(result.data["truncated"] for result in results),
                complete_requested_range=(all(result.data["complete_requested_range"] for result in results)
                                          if results[0].data["complete_requested_range"] is not None else None),
                older_history_available=(True if any(result.data["older_history_available"] is True for result in results) else
                                         None if any(result.data["older_history_available"] is None for result in results) else False))
    if len(results) > 1:
        data["session_feed_completion"] = [{"native_kline_type_prob": result.data["native_kline_type_prob"],
                                            "completion_reason": result.data["completion_reason"]} for result in results]
        data["completion_reason"] = "combined_native_session_feeds"
    source = deepcopy(results[0].source)
    source.update(metadata)
    warnings = list(dict.fromkeys(warning for result in results for warning in result.warnings
                                if not warning.startswith("The provider's default candle session")))
    warnings.append("Session labels use observed native section IDs, without applying stock trading hours to options; exchange calendars and section semantics outside the tested build are not certified.")
    warnings.append("Futu can return replenished/zero-volume candles; point_type and native OHLCV values are retained.")
    if metadata["ambiguous_previous_close_count"]:
        warnings.append("Merged session feeds contain different previous_close values for the same candle. The unified previous_close is null and raw field11 is omitted; both complete native bars are retained in native_session_feed_variants with their request enum provenance.")
    if session == "overnight" and kind == "us_equity_option":
        warnings.append("No option overnight section was observed; an empty selection does not prove all option overnight data is unavailable.")
    return ProviderResult(data, source, warnings)
