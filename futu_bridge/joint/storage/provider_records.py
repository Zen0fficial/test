"""Typed observed provider records, with exact archives and conservative availability.

Converters extend the fixed provider rather than replacing it. Financial dates,
publication timestamps and receipt times remain distinct. A link is not document
content; unverified native enum values and scales never become guessed units.
"""

from __future__ import annotations

import ast
from datetime import date
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from typing import Any

import pyarrow as pa

from .captures import timestamp
from .incremental import VersionContract, as_of, merge
from .snapshots import Snapshot, SnapshotStore, file_hash

STRINGS = pa.list_(pa.field("element", pa.string()))
PRICE = pa.decimal128(38, 9)
RATIO = pa.decimal256(76, 38)
COMMON = [
    ("record_id", pa.string()), ("provider", pa.string()), ("symbol", pa.string()),
    ("source_observed_ns", pa.int64()), ("response_served_ns", pa.int64()), ("observed_ns", pa.int64()),
    ("source_sha256", pa.string()), ("payload_json", pa.large_string()), ("quality_flags", STRINGS),
]
FIELDS = {
    "news": [("news_id", pa.string()), ("stock_id", pa.string()), ("headline", pa.large_string()),
             ("published_ns", pa.int64()), ("url", pa.string()), ("publisher", pa.string()),
             ("language", pa.string())],
    "instruments": [("stock_id", pa.string()), ("code", pa.string()), ("futu_symbol", pa.string()),
                    ("name", pa.string()), ("exchange", pa.string()), ("listed_exchange", pa.string()),
                    *[(name, pa.int64()) for name in ("currency_code", "market_code", "instrument_type_v2",
                                                    "sub_instrument_type_v2", "price_accuracy", "delisting_flag")]],
    "option_contracts": [("stock_id", pa.string()), ("underlying", pa.string()),
                         ("underlying_stock_id", pa.string()), ("side", pa.string()), ("strike", PRICE),
                         ("strike_raw", pa.string()), ("strike_scale", pa.int64()), ("expiry_date", pa.string()),
                         ("identity_status", pa.string()), ("multiplier_raw", pa.string()),
                         ("contract_share_size_raw", pa.string()), ("timezone", pa.string()),
                         ("exchange", pa.string()), ("is_delisted", pa.bool_())],
    "earnings_calendar": [("stock_id", pa.string()), ("scheduled_date", pa.string()),
                          ("fiscal_period", pa.string()), ("fiscal_year", pa.int64()),
                          ("fiscal_period_raw", pa.int64()), ("provider_timestamp_ns", pa.int64()),
                          ("timestamp_meaning", pa.string()), ("market_timezone", pa.string()),
                          ("confirmed", pa.bool_()), ("estimated", pa.bool_())],
    "earnings_report_links": [("stock_id", pa.string()), ("report_id", pa.string()),
                              ("fiscal_period", pa.string()), ("url", pa.string()),
                              ("content_type", pa.string()), ("pdf_available", pa.bool_())],
    "issuer_documents": [("title", pa.string()), ("url", pa.string()), ("year", pa.int64()),
                         ("period_type", pa.string()), ("format", pa.string()), ("verified_download", pa.bool_())],
    "earnings_calls": [("stock_id", pa.string()), ("fiscal_period", pa.string()), ("feed_id", pa.string()),
                       ("live_id", pa.string()), ("title", pa.large_string()), ("url", pa.string()),
                       ("meeting_start_ns", pa.int64()), ("page_published_ns", pa.int64()),
                       ("status_code", pa.int64()), ("transcript_available", pa.bool_()),
                       ("issuer_verified", pa.bool_()), ("ai_content_unverified", pa.bool_()),
                       ("recording_count", pa.int64()), ("subtitle_count", pa.int64())],
    "transcript_paragraphs": [("feed_id", pa.string()), ("fiscal_period", pa.string()), ("ordinal", pa.int64()),
                             ("text", pa.large_string()), ("language", pa.string()), ("format", pa.string()),
                             ("issuer_verified", pa.bool_()), ("machine_generated_or_translated", pa.bool_()),
                             ("timestamped", pa.bool_()), ("completeness_verified", pa.bool_())],
    "short_volume": [("stock_id", pa.string()), ("trading_date", pa.string()),
                     ("provider_timestamp_ns", pa.int64()), ("short_volume", pa.int64()),
                     ("total_volume", pa.int64()), ("short_volume_ratio", RATIO),
                     ("short_volume_ratio_percent", RATIO), ("ratio_source", pa.string()),
                     ("quantity_unit", pa.string())],
    "polymarket_outcomes": [("event_id", pa.string()), ("market_id", pa.string()), ("condition_id", pa.string()),
                            ("token_id", pa.string()), ("outcome_index", pa.int64()), ("outcome", pa.string()),
                            ("question", pa.large_string()), ("indicative_probability", RATIO),
                            ("active", pa.bool_()), ("closed", pa.bool_()), ("resolved", pa.bool_()),
                            ("resolution_status", pa.string()), ("resolution_source", pa.string())],
}
UNITS = {
    "news": "published_ns: UTC nanoseconds; text: Unicode; source IDs: exact strings",
    "instruments": "numeric codes: original undocumented enums; price_accuracy: source integer, not tick size",
    "option_contracts": "strike: strike_raw / 1e9 in source price units; multiplier/share-size raw: unverified scale",
    "earnings_calendar": "scheduled_date: provider-local ISO date; timestamp: UTC ns, announcement meaning unverified",
    "earnings_report_links": "URL metadata only; fiscal_period: source label, no numerical financial statements",
    "issuer_documents": "URL metadata only; publication time unknown; downloaded content not supplied",
    "earnings_calls": "meeting_start_ns/page_published_ns: UTC ns; status_code: untranslated provider enum",
    "transcript_paragraphs": "ordinal: zero-based source order; text: Unicode; no paragraph timestamp",
    "short_volume": "volume: shares; ratio: fraction; ratio_percent: percentage points; date: source-local ISO date",
    "polymarket_outcomes": "probability: indicative 0..1 fraction, not execution quote; IDs: exact strings",
}
SCHEMAS = {family: pa.schema([*COMMON, *fields], metadata={
    b"joint_schema": f"provider_{family}_v1".encode(), b"units": UNITS[family].encode(),
    b"availability": b"max_source_observation_response_completion_not_historical_release",
    b"payload_format": b"exact_canonical_json_v1", b"receipt_time_unit": b"UTC_nanoseconds",
    b"missing_record_semantics": b"absence_is_not_deletion_or_cancellation",
}) for family, fields in FIELDS.items()}
FAMILIES = tuple(SCHEMAS)
VERSIONS = VersionContract(("provider", "record_id"), "observed_ns", ("payload_json",))
SOURCES = {
    "news": ("futu-desktop-session", "stock_news"),
    "instruments": ("futu-desktop-security-catalog", "security_master"),
    "option_contracts": ("futu-desktop-session", "option_chain"),
    "earnings_calendar": ("futu-desktop-session", "earnings_calendar"),
    "earnings_report_links": ("futu-desktop-session", "earnings_report_link"),
    "issuer_documents": ("tencent_issuer_archive", "historical"),
    "earnings_calls": ("futu_public_earnings", "historical"),
    "transcript_paragraphs": ("futu_public_earnings", "historical"),
    "short_volume": ("futu-desktop-session", "daily_short_volume"),
    "polymarket_outcomes": ("polymarket", "metadata"),
}
CAPTURES = {
    "news": "news_hk.json", "option_contracts": "options_us.json", "earnings_calendar": "calendar_hk.json",
    "earnings_report_links": "report_hk.json", "short_volume": "short_hk.json", "instruments": "instrument_hk.json",
    "issuer_documents": "issuer_hk.json", "earnings_calls": "call_hk.json", "transcript_paragraphs": "call_hk.json",
    "polymarket_outcomes": "event.json",
}


def exact_json(value: Any) -> str:
    """Canonicalize semantic JSON while retaining every decimal digit as a JSON number."""
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("Nonfinite provider number")
        rendered = format(value, "f")
        rendered = rendered.rstrip("0").rstrip(".") if "." in rendered else rendered
        return "0" if value == 0 else rendered
    if isinstance(value, dict):
        return "{" + ",".join(exact_json(k) + ":" + exact_json(value[k]) for k in sorted(value)) + "}"
    if isinstance(value, list):
        return "[" + ",".join(exact_json(item) for item in value) + "]"
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def identifier(value: Any, name: str) -> str:
    """Reject numeric or missing identities instead of silently stringifying precision loss."""
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a nonempty exact string")
    return value


def iso_date(value: Any) -> str:
    """Validate a date without inventing a timezone or intraday release instant."""
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError("Expected canonical ISO calendar date")
    return value


def integer(value: Any, name: str) -> int:
    """Require an exact non-boolean integer within Arrow's signed 64-bit range."""
    if isinstance(value, bool) or not isinstance(value, int) or not -(2**63) <= value < 2**63:
        raise ValueError(f"{name} must be an exact int64")
    return value


def unix_ns(value: Any, multiplier: int) -> int | None:
    """Convert explicitly labeled Unix seconds or milliseconds without floating arithmetic."""
    if value is None:
        return None
    if isinstance(value, str) and value.isdigit():
        value = int(value)
    return integer(integer(value, "Unix timestamp") * multiplier, "UTC nanoseconds")


def selections(data: dict[str, Any], family: str) -> list[dict[str, Any]]:
    """Select supplied records; missing optional content yields an explicit empty table."""
    keys = {"news": "items", "instruments": "instruments", "option_contracts": "contracts",
            "earnings_calendar": "events", "issuer_documents": "documents", "short_volume": "items"}
    if family == "instruments" and "instruments" not in data:
        return [data]
    if family == "earnings_report_links":
        return [data["report"]] if data.get("available") and data.get("report") else []
    if family == "earnings_calls":
        return [data] if data.get("available") and data.get("meeting") else []
    if family == "transcript_paragraphs":
        transcript = data.get("transcript") or {}
        return [{"ordinal": i, "text": paragraph} for i, paragraph in enumerate(transcript.get("paragraphs", []))]
    if family == "polymarket_outcomes":
        return outcome_records(data)
    result = data[keys[family]]
    if not isinstance(result, list) or any(not isinstance(row, dict) for row in result):
        raise ValueError("Expected provider record objects")
    return result


def list_field(value: Any) -> list[Any]:
    """Decode Gamma's JSON-encoded arrays without rounding embedded decimal values."""
    result = json.loads(value, parse_float=Decimal) if isinstance(value, str) else value
    if not isinstance(result, list):
        raise ValueError("Expected a provider outcome array")
    return result


def outcome_records(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Align arbitrary outcome counts, token identities and optional indicative probabilities."""
    records = []
    for event in data["events"]:
        event_id = identifier(event["id"], "event_id")
        event_metadata = {k: value for k, value in event.items() if k != "markets"}
        for market in event["markets"]:
            market_id = identifier(market["id"], "market_id")
            labels, tokens = list_field(market["outcomes"]), list_field(market["clobTokenIds"])
            prices = list_field(market["outcomePrices"]) if market.get("outcomePrices") else [None] * len(labels)
            if not labels or len(labels) != len(tokens) or len(labels) != len(prices):
                raise ValueError("Outcome/token/probability arrays do not align")
            for index, (label, token, price) in enumerate(zip(labels, tokens, prices, strict=True)):
                identifier(label, "outcome label")
                if not identifier(token, "token_id").isdigit():
                    raise ValueError("Token IDs must remain exact decimal strings")
                probability = Decimal(str(price)) if price is not None else None
                if probability is not None and (not probability.is_finite() or not 0 <= probability <= 1):
                    raise ValueError("Indicative outcome probability is outside 0..1")
                records.append({"event_id": event_id, "market_id": market_id, "event_metadata": event_metadata,
                                "market": market, "outcome_index": index, "outcome": label, "token_id": token,
                                "indicative_probability": probability, "symbol": "POLYMARKET." + market_id})
    return records


def option_fields(item: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    """Verify native option identity and exact strike scale before exposing typed contracts."""
    native = item["native_metadata"]
    stock = identifier(item["stock_id"], "option stock_id")
    underlying = identifier(item["underlying_stock_id"], "underlying stock_id")
    raw = identifier(item["strike_raw"], "raw strike")
    scale = integer(item["strike_scale"], "strike scale")
    if not raw.isdigit() or scale != 1_000_000_000:
        raise ValueError("Unverified option strike scaling")
    # Construct directly from the decimal tuple, without the process Decimal context.
    strike = Decimal((0, tuple(int(digit) for digit in raw), -9))
    if (item.get("identity_status") != "native_metadata_verified" or native["option_id"] != stock
            or native["underlying_stock_id"] != underlying or data["underlying_stock_id"] != underlying
            or native["strike_price_raw"] != raw or Decimal(str(item["strike"])) != strike
            or item["side"] not in ("call", "put")
            or native["option_type_raw"] != (1 if item["side"] == "call" else 2)
            or native["real_expiration_date_raw"] != iso_date(item["expiry"]).replace("-", "")):
        raise ValueError("Inconsistent native option contract identity")
    return {"stock_id": stock, "underlying": data["underlying"], "underlying_stock_id": underlying,
            "side": item["side"], "strike": strike, "strike_raw": raw, "strike_scale": scale,
            "expiry_date": item["expiry"], "identity_status": item["identity_status"],
            "multiplier_raw": native.get("multiplier_raw"),
            "contract_share_size_raw": native.get("contract_share_size_raw"),
            "timezone": native.get("timezone"), "exchange": native.get("exchange"),
            "is_delisted": native.get("is_delisted")}


def typed_fields(item: dict[str, Any], data: dict[str, Any], family: str) -> tuple[dict[str, Any], list[str]]:
    """Map documented provider fields while keeping unsupported interpretations explicit."""
    if family == "polymarket_outcomes":
        market = item["market"]
        flags = ["indicative_price_not_executable_quote", "closed_does_not_imply_resolved"]
        if item["indicative_probability"] is None:
            flags.append("probability_not_supplied")
        return {**{k: item[k] for k in ("event_id", "market_id", "token_id", "outcome_index", "outcome",
                                        "indicative_probability")},
                "condition_id": identifier(market["conditionId"], "condition_id"), "question": market["question"],
                "active": market.get("active"), "closed": market.get("closed"), "resolved": market.get("resolved"),
                "resolution_status": market.get("umaResolutionStatus"),
                "resolution_source": market.get("resolutionSource")}, flags
    if family == "news":
        published = timestamp(item["published_at"])
        if published != unix_ns(item["published_at_unix"], 1_000_000_000):
            raise ValueError("News publication timestamps disagree")
        return {"news_id": identifier(item["news_id"], "news_id"), "stock_id": data["stock_id"],
                "published_ns": published, **{k: item.get(k) for k in
                                             ("headline", "url", "publisher", "language")}}, ["headlines_only"]
    if family == "instruments":
        fields = {name: item.get(name) for name, _ in FIELDS[family]}
        fields["stock_id"], fields["name"] = identifier(item["stock_id"], "stock_id"), item.get("eng_name")
        return fields, ["catalog_only", "entitlement_unverified", "currency_enum_untranslated"]
    if family == "option_contracts":
        return option_fields(item, data), ["current_contract_metadata_only", "multiplier_scale_unverified",
                                          "strike_currency_not_supplied", "expiration_instant_unverified"]
    if family == "earnings_calendar":
        value = unix_ns(item.get("provider_timestamp_unix"), 1_000_000_000)
        if item.get("provider_timestamp_utc") and value != timestamp(item["provider_timestamp_utc"]):
            raise ValueError("Calendar provider timestamps disagree")
        return {"stock_id": identifier(item["stock_id"], "stock_id"), "scheduled_date": iso_date(item["date"]),
                "fiscal_period": item.get("fiscal_period_label"), "fiscal_year": item.get("fiscal_year"),
                "fiscal_period_raw": item.get("fiscal_period_raw"), "provider_timestamp_ns": value,
                "timestamp_meaning": item.get("provider_timestamp_meaning"),
                "market_timezone": data.get("market_timezone"), "confirmed": item.get("confirmed"),
                "estimated": item.get("estimated")}, ["schedule_not_earnings_actuals", "bounded_native_selection"]
    if family == "earnings_report_links":
        if item["stock_id"] != data["stock_id"] or item["fiscal_period"] != data["fiscal_period"]:
            raise ValueError("Report link identity disagrees with its request")
        return {"stock_id": identifier(item["stock_id"], "stock_id"),
                "report_id": identifier(item["report_id"], "report_id"), "fiscal_period": item["fiscal_period"],
                "url": item["url"], "content_type": data.get("content_type"),
                "pdf_available": data.get("pdf_available")}, ["link_metadata_only", "release_time_unknown"]
    if family == "issuer_documents":
        return {name: item.get(name) for name, _ in FIELDS[family]}, ["link_metadata_only", "release_time_unknown"]
    if family == "earnings_calls":
        meeting, call = data["meeting"], data.get("call") or {}
        feed = identifier(meeting["feed_id"], "feed_id")
        if call.get("feed_id", feed) != feed:
            raise ValueError("Earnings page and meeting feed identities disagree")
        return {"stock_id": data["stock_id"], "fiscal_period": data["fiscal_period"], "feed_id": feed,
                "live_id": identifier(meeting["live_id"], "live_id"), "title": meeting["title"],
                "url": meeting["url"], "meeting_start_ns": unix_ns(meeting.get("start_timestamp_ms"), 1_000_000),
                "page_published_ns": unix_ns(call.get("published_timestamp"), 1_000_000_000),
                "status_code": meeting.get("status_code"), "transcript_available": call.get("transcript_available"),
                "issuer_verified": call.get("issuer_verified"),
                "ai_content_unverified": call.get("ai_content_unverified"),
                "recording_count": len(call.get("recordings", [])), "subtitle_count": len(call.get("subtitles", []))
                }, ["content_period_identity_unverified", "media_urls_may_expire", "public_page_not_issuer_transcript"]
    if family == "transcript_paragraphs":
        transcript = data["transcript"]
        if not isinstance(item["text"], str):
            raise ValueError("Transcript paragraphs must be Unicode strings")
        return {"feed_id": identifier(data["meeting"]["feed_id"], "feed_id"),
                "fiscal_period": data["fiscal_period"], **item,
                **{k: transcript.get(k) for k in ("language", "format", "issuer_verified",
                                                 "machine_generated_or_translated", "timestamped",
                                                 "completeness_verified")}}, ["content_period_identity_unverified"]
    if data.get("quantity_unit") != "shares" or data.get("frequency") != "daily":
        raise ValueError("Only documented daily short-share volume is supported")
    short, total = integer(item["short_volume"], "short_volume"), integer(item["total_volume"], "total_volume")
    if not 0 <= short <= total:
        raise ValueError("Invalid short share quantities")
    return {"stock_id": identifier(data["stock_id"], "stock_id"), "trading_date": iso_date(item["date"]),
            "provider_timestamp_ns": unix_ns(item.get("timestamp"), 1_000_000_000), "short_volume": short,
            "total_volume": total, "short_volume_ratio": item.get("short_volume_ratio"),
            "short_volume_ratio_percent": item.get("short_volume_ratio_percent"),
            "ratio_source": item.get("ratio_source"), "quantity_unit": "shares"
            }, ["not_short_interest", "historical_release_time_unknown", "daily_completeness_unverified"]


def record_identity(fields: dict[str, Any], symbol: str, family: str) -> str:
    """Use business identities without conflating prices or mutable content with identity."""
    names = {"news": ("news_id",), "instruments": ("stock_id",), "option_contracts": ("stock_id",),
             "earnings_calendar": ("fiscal_period", "scheduled_date"), "earnings_report_links": ("report_id",),
             "issuer_documents": ("url",), "earnings_calls": ("feed_id",),
             "transcript_paragraphs": ("feed_id", "ordinal"), "short_volume": ("trading_date",)}
    names["polymarket_outcomes"] = ("event_id", "market_id", "token_id")
    return exact_json([family, symbol, *[fields[name] for name in names[family]]])


def provider_table(payload: bytes, family: str) -> pa.Table:
    """Convert a genuine provider envelope to versionable Arrow records without network access."""
    if family not in SCHEMAS:
        raise ValueError("Unsupported typed provider family")
    envelope = json.loads(payload, parse_float=Decimal)
    source, data = envelope["source"], envelope["data"]
    if (source.get("provider"), source.get("kind")) != SOURCES[family]:
        raise ValueError("Capture provenance does not match the requested provider family")
    if family == "polymarket_outcomes" and source.get("api") != "gamma":
        raise ValueError("Outcome metadata requires the fixed Gamma provider")
    source_time, served = timestamp(source["observed_at"]), timestamp(envelope["served_at"])
    observed, digest = max(source_time, served), hashlib.sha256(payload).hexdigest()
    rows = []
    for item in selections(data, family):
        fields, flags = typed_fields(item, data, family)
        symbol = identifier(item.get("symbol") or data.get("symbol") or data.get("underlying"), "symbol")
        for name, field in FIELDS[family]:
            if name.endswith("_id") and fields.get(name) is not None:
                identifier(fields[name], name)
            if pa.types.is_boolean(field) and fields.get(name) is not None and not isinstance(fields[name], bool):
                raise ValueError(f"{name} must be a nullable boolean")
            if pa.types.is_int64(field) and fields.get(name) is not None:
                integer(fields[name], name)
            if pa.types.is_string(field) and fields.get(name) is not None and not isinstance(fields[name], str):
                raise ValueError(f"{name} must retain its source string type")
        # The field mapping is included so any meaningful typed revision participates in conflict detection.
        record = {"source_record": item, "typed_fields": fields, "quality_flags": flags}
        rows.append({"record_id": record_identity(fields, symbol, family), "provider": source["provider"],
                     "symbol": symbol, "source_observed_ns": source_time, "response_served_ns": served,
                     "observed_ns": observed, "source_sha256": digest, "payload_json": exact_json(record),
                     "quality_flags": flags, **fields})
    if len({row["record_id"] for row in rows}) != len(rows):
        raise ValueError("Duplicate business identities within one provider response")
    return pa.Table.from_pylist(rows, schema=SCHEMAS[family])


def ingest_provider(store: SnapshotStore, source: Path, dataset: str, family: str, *,
                    expected_head: str | None = None) -> Snapshot:
    """Retain exact bytes and publish idempotent typed revisions through the common store."""
    payload = source.read_bytes()
    table = provider_table(payload, family)
    metadata = {"capture_kind": family, "capture_sha256": store.archive(payload), "schema_version": 1,
                "availability": "observed_after_response_completion", "units": UNITS[family],
                "typed_rows": table.num_rows, "empty_response_certifies_coverage": False}
    return merge(store, dataset, table, VERSIONS, expected_head=expected_head, metadata=metadata)


def inventory(repo: Path) -> dict[str, Any]:
    """Enumerate pinned provider methods and every public REST catalog route, without calling them."""
    provider = repo / "futu_bridge/provider.py"
    catalog = repo / "futu_bridge/polymarket_endpoints.json"
    methods = []
    for node in ast.walk(ast.parse(provider.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "CAPABILITIES"
                                               for t in node.targets):
            methods = list(ast.literal_eval(node.value))
    mapping = {"quotes": "SSE raw payload retained; no dedicated typed quote fundamental fields",
               "bars": "typed complete unadjusted daily/minute OHLCV; source turnover raw, release time unknown",
               "orderbook": "typed observed L2 levels; no exchange order IDs or guaranteed losslessness",
               "trades": "typed recent SSE trades with exact IDs and size; historical completeness unavailable",
               "stream": "journal + typed book/trades; quote/status/unknown payload archived",
               "search_symbols": "typed instruments", "resolve_symbol": "typed instruments",
               "option_chain": "typed current option contracts; Greeks/prices/multiplier units unavailable",
               "stock_news": "typed news headlines and links; article bodies unavailable",
               "earnings_calendar": "typed dated schedule; confirmed/estimated nullable; no actual results",
               "earnings_report_link": "typed earnings report page metadata; no original financial statement values",
               "earnings_call": "typed meeting metadata and unverified paragraph text; media links archived",
               "technical_indicators": "derived local bars; indicator values remain raw, no native chart parity",
               "daily_short_volume": "typed daily short-share volume/ratios; no short positions or borrow inventory"}
    if set(methods) != set(mapping):
        raise ValueError("Fixed provider capability inventory changed; review field coverage explicitly")
    endpoints = []
    for entry in json.loads(catalog.read_text(encoding="utf-8"))["endpoints"]:
        scope = "raw_json_only_no_typed_contract"
        if entry["api"] == "gamma" and entry["path"] in ("/events", "/events/keyset", "/events/pagination"):
            scope = "typed_event_identity_text_and_market_outcomes; categories_popularity_resolutions_full_json"
        if entry["api"] == "data" and entry["path"] == "/v2/prices-history":
            scope = "history_adapter_typed_token_time_probability; historical_availability_unknown"
        endpoints.append({"api": entry["api"], "method": entry["method"], "path": entry["path"],
                          "typed_scope": scope, "live_endpoint_acceptance_claimed": False})
    return {"provider_source_sha256": file_hash(provider), "catalog_sha256": file_hash(catalog),
            "provider_methods": {name: mapping[name] for name in methods}, "polymarket_endpoints": endpoints,
            "extra_public_documents": ["issuer report links typed", "announcement links raw; no real acceptance"],
            "streams": "CLOB/RTDS/sports raw JSON only; exchange gaps and historical receipt times not inferred",
            "onchain": "status/logs/balance/receipt raw only; exact integer strings retained; no settled event table",
            "removed": "Goldsky key-required source remains removed; no new source or key introduced"}


def run(captures: Path, output: Path, repo: Path) -> dict[str, Any]:
    """Verify real captured fields, immutable round trips and no knowledge before receipt."""
    output.mkdir(parents=True, exist_ok=False)
    baseline = inventory(repo)
    store, reports = SnapshotStore(output / "store"), {}
    for family, filename in CAPTURES.items():
        path = captures / filename
        table = provider_table(path.read_bytes(), family)
        if not table.num_rows:
            raise ValueError(f"Real acceptance requires nonempty {family} records")
        snapshot = ingest_provider(store, path, family, family)
        restored = store.read(snapshot.identity).drop(["_version_id"])
        expected = table.sort_by([("provider", "ascending"), ("record_id", "ascending")])
        duplicate = ingest_provider(store, path, family, family, expected_head=snapshot.identity)
        first = min(table["observed_ns"].to_pylist())
        if (not restored.equals(expected, check_metadata=True) or snapshot.identity != duplicate.identity
                or as_of(store, snapshot.identity, first - 1).num_rows != 0
                or as_of(store, snapshot.identity, first).num_rows != table.num_rows
                or (store.root / "raw" / file_hash(path)).read_bytes() != path.read_bytes()):
            raise ValueError(f"Immutable/raw/availability round trip failed for {family}")
        reports[family] = {"rows": table.num_rows, "snapshot_id": snapshot.identity,
                           "capture_sha256": file_hash(path), "available_ns": first,
                           "raw_bytes_identical": True, "idempotent": True, "as_of_verified": True}
    # Resolve and search use the same schema, with each actual response preserved.
    search = captures / "search_hk.json"
    snapshot = ingest_provider(store, search, "instruments", "instruments", expected_head=store.head("instruments"))
    reports["instrument_search"] = {"rows": provider_table(search.read_bytes(), "instruments").num_rows,
                                     "snapshot_id": snapshot.identity, "capture_sha256": file_hash(search)}
    current = inventory(repo)
    if baseline != current:
        raise ValueError("Fixed provider source or catalog changed during acceptance")
    result = {"passed": True, "status": "passed", "real_orders_enabled": False,
              "fixed_provider_hashes_unchanged_during_run": True,
              "captures": str(captures.resolve()), "families": reports, "inventory": current,
              "implementation_sha256": file_hash(Path(__file__)),
              "scope": "typed supplied fields only; unsupported fields and unknown units explicitly retained as raw"}
    (output / "report.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    return result
