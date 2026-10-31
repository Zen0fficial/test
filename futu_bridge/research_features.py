"""Point-in-time-aware unstructured and options feature preparation.

This module intentionally separates raw event records from numeric Qlib
features. Futu publication timestamps are not treated as first-availability
timestamps unless the caller supplies an explicit ``available_at`` value.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import math
import os
import re
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

SCHEMA_VERSION = 1
SOURCE_TYPES = {
    "news_headline",
    "earnings_transcript",
    "report_pdf_page",
    "futu_ai_summary",
}
TERM_GROUPS = {
    "uncertainty": (
        "uncertain", "uncertainty", "risk", "risks", "may", "could", "可能", "风险", "不确定",
    ),
    "guidance": (
        "guidance", "outlook", "forecast", "expect", "expects", "预计", "展望", "指引", "预期",
    ),
    "catalyst": (
        "launch", "growth", "record", "approval", "contract", "增长", "创新高", "获批", "订单",
    ),
    "pressure": (
        "decline", "loss", "weak", "cut", "layoff", "下滑", "亏损", "疲软", "裁员", "减值",
    ),
}


class ResearchFeatureError(ValueError):
    """Raised when a source cannot be represented without inventing provenance."""


def _response_parts(response: Any) -> tuple[Mapping[str, Any], Mapping[str, Any], list[str]]:
    data = getattr(response, "data", None)
    if isinstance(data, Mapping):
        return data, dict(getattr(response, "source", {}) or {}), list(getattr(response, "warnings", []) or [])
    if isinstance(response, Mapping):
        nested = response.get("data")
        if isinstance(nested, Mapping):
            return nested, dict(response.get("source", {}) or {}), list(response.get("warnings", []) or [])
        return response, {}, []
    raise ResearchFeatureError("Expected a MarketDataResponse or mapping.")


def _iso(value: Any) -> Optional[str]:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        if not math.isfinite(number):
            return None
        if number > 10_000_000_000:
            number /= 1000
        return dt.datetime.fromtimestamp(number, dt.timezone.utc).isoformat().replace("+00:00", "Z")
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _event_date(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    return value[:10] if re.fullmatch(r"\d{4}-\d{2}-\d{2}T.*", value) else None


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _event_id(source_type: str, source_id: str, text: str, suffix: str = "") -> str:
    return hashlib.sha256((source_type + "\0" + source_id + "\0" + suffix + "\0" + _hash_text(text)).encode("utf-8")).hexdigest()


def make_event(*, instrument: str, source_type: str, source_id: str, text: str,
               event_time: Any = None, available_at: Any = None,
               language: Optional[str] = None, publisher: Optional[str] = None,
               issuer_verified: bool = False, ai_generated: bool = False,
               metadata: Optional[Mapping[str, Any]] = None, suffix: str = "") -> dict[str, Any]:
    if not isinstance(instrument, str) or not instrument.strip():
        raise ResearchFeatureError("instrument must be nonempty.")
    if source_type not in SOURCE_TYPES:
        raise ResearchFeatureError("Unsupported unstructured source type: " + str(source_type))
    if not isinstance(source_id, str) or not source_id.strip():
        raise ResearchFeatureError("source_id must be nonempty.")
    if not isinstance(text, str):
        raise ResearchFeatureError("event text must be a string.")
    normalized = text.strip()
    event_time_iso = _iso(event_time)
    available_iso = _iso(available_at)
    return {
        "schema_version": SCHEMA_VERSION,
        "event_id": _event_id(source_type, source_id, normalized, suffix),
        "instrument": instrument.strip(),
        "source_type": source_type,
        "source_id": source_id,
        "event_time": event_time_iso,
        "available_at": available_iso,
        "point_in_time_safe": available_iso is not None,
        "language": language,
        "publisher": publisher,
        "issuer_verified": bool(issuer_verified),
        "ai_generated": bool(ai_generated),
        "text": normalized,
        "text_sha256": _hash_text(normalized),
        "metadata": dict(metadata or {}),
    }


def news_events(response: Any, *, available_at: Any = None) -> list[dict[str, Any]]:
    """Convert Futu headline metadata into events without fabricating article bodies."""
    data, source, warnings = _response_parts(response)
    symbol = data.get("symbol")
    items = data.get("items")
    if not isinstance(symbol, str) or not isinstance(items, list):
        raise ResearchFeatureError("News response must contain symbol and items.")
    events = []
    for item in items:
        headline = item.get("headline")
        news_id = str(item.get("news_id", ""))
        if not isinstance(headline, str) or not news_id:
            raise ResearchFeatureError("News item lacks headline or news_id.")
        events.append(make_event(
            instrument=symbol,
            source_type="news_headline",
            source_id=news_id,
            text=headline,
            event_time=item.get("published_at"),
            available_at=available_at,
            language=item.get("language"),
            publisher=item.get("publisher"),
            metadata={"url": item.get("url"), "provider_source": source,
                      "provider_warnings": warnings},
        ))
    return events


def earnings_transcript_events(response: Any, *, available_at: Any = None) -> list[dict[str, Any]]:
    """Convert page or subtitle transcript text into events.

    Futu marks the page transcript as AI-generated and unverified. Subtitle
    cues retain offsets in metadata; they are not mistaken for issuer timing.
    """
    data, source, warnings = _response_parts(response)
    symbol = data.get("symbol")
    call = data.get("call") or {}
    transcript = data.get("transcript")
    if not isinstance(symbol, str) or not isinstance(transcript, Mapping):
        return []
    feed_id = str(call.get("feed_id") or call.get("call_id") or data.get("fiscal_period") or "call")
    published = _iso(call.get("published_timestamp"))
    events = []
    paragraphs = transcript.get("paragraphs")
    if isinstance(paragraphs, list):
        for index, text in enumerate(paragraphs):
            if isinstance(text, str) and text.strip():
                events.append(make_event(
                    instrument=symbol,
                    source_type="earnings_transcript",
                    source_id=feed_id,
                    suffix=str(index),
                    text=text,
                    event_time=published,
                    available_at=available_at,
                    language=transcript.get("language"),
                    ai_generated=bool(transcript.get("machine_generated_or_translated", True)),
                    issuer_verified=bool(transcript.get("issuer_verified", False)),
                    metadata={"paragraph_index": index, "fiscal_period": data.get("fiscal_period"),
                              "timestamped": False, "source": source, "warnings": warnings},
                ))
        return events
    cues = transcript.get("cues")
    if isinstance(cues, list):
        for index, cue in enumerate(cues):
            text = cue.get("text") if isinstance(cue, Mapping) else None
            if isinstance(text, str) and text.strip():
                events.append(make_event(
                    instrument=symbol,
                    source_type="earnings_transcript",
                    source_id=feed_id,
                    suffix=str(index),
                    text=text,
                    event_time=published,
                    available_at=available_at,
                    language=transcript.get("language"),
                    ai_generated=bool(transcript.get("machine_generated_or_translated", True)),
                    issuer_verified=bool(transcript.get("issuer_verified", False)),
                    metadata={"cue_index": index, "start_ms": cue.get("start_ms"),
                              "end_ms": cue.get("end_ms"), "fiscal_period": data.get("fiscal_period"),
                              "timestamped": True, "source": source, "warnings": warnings},
                ))
    return events


def pdf_page_events(path: Path | str, *, instrument: str, source_id: str,
                    event_time: Any = None, available_at: Any = None,
                    issuer_verified: bool = False, metadata: Optional[Mapping[str, Any]] = None) -> list[dict[str, Any]]:
    """Extract bounded page text from a downloaded PDF using optional pypdf."""
    document = Path(path).expanduser().resolve()
    if not document.is_file() or document.suffix.lower() != ".pdf":
        raise ResearchFeatureError("PDF path must point to an existing .pdf file.")
    try:
        import pypdf
    except ImportError as exc:
        raise ResearchFeatureError("Install the optional PDF dependency with `pip install pypdf`.") from exc
    raw = document.read_bytes()
    pdf_sha256 = hashlib.sha256(raw).hexdigest()
    reader = pypdf.PdfReader(str(document))
    events = []
    for page_number, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        if not text.strip():
            continue
        events.append(make_event(
            instrument=instrument,
            source_type="report_pdf_page",
            source_id=source_id,
            suffix=str(page_number),
            text=text,
            event_time=event_time,
            available_at=available_at,
            issuer_verified=issuer_verified,
            metadata={**dict(metadata or {}), "page_number": page_number,
                      "page_count": len(reader.pages), "pdf_path": str(document),
                      "pdf_sha256": pdf_sha256, "extraction_engine": "pypdf"},
        ))
    return events


def futu_ai_summary_event(*, instrument: str, source_id: str, summary: str,
                          event_time: Any = None, available_at: Any = None,
                          language: Optional[str] = None,
                          metadata: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    """Normalize a verified Futu AI-summary response when its schema is known.

    The current bridge has only static evidence of Futu's report-card service;
    this function accepts a future decoded response without claiming that the
    native summary endpoint is implemented today.
    """
    return make_event(instrument=instrument, source_type="futu_ai_summary", source_id=source_id,
                      text=summary, event_time=event_time, available_at=available_at,
                      language=language, ai_generated=True, issuer_verified=False,
                      metadata={**dict(metadata or {}), "native_schema_verified": False})


def _term_count(text: str, terms: Sequence[str]) -> int:
    lowered = text.lower()
    return sum(lowered.count(term.lower()) for term in terms)


def aggregate_daily_features(events: Iterable[Mapping[str, Any]], *, date_basis: str = "event_time") -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Aggregate text events into Qlib-ready daily feature rows.

    ``date_basis='available_at'`` is the safe default for live research once
    explicit availability timestamps exist. Event-time aggregation is useful
    for exploratory analysis but is marked as not point-in-time safe.
    """
    if date_basis not in ("event_time", "available_at"):
        raise ResearchFeatureError("date_basis must be event_time or available_at.")
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    skipped = 0
    for event in events:
        instrument = event.get("instrument")
        date = _event_date(event.get(date_basis))
        if not isinstance(instrument, str) or not date:
            skipped += 1
            continue
        key = (instrument, date)
        row = grouped.setdefault(key, {
            "date": date, "symbol": instrument, "pit_safe": int(date_basis == "available_at" and event.get("point_in_time_safe") is True),
            "text_event_count": 0, "news_headline_count": 0, "earnings_transcript_count": 0,
            "report_pdf_page_count": 0, "futu_ai_summary_count": 0, "text_char_count": 0,
            "publisher_count": set(), "ai_generated_count": 0, "issuer_verified_count": 0,
            "uncertainty_term_count": 0, "guidance_term_count": 0,
            "catalyst_term_count": 0, "pressure_term_count": 0,
        })
        row["text_event_count"] += 1
        source_type = event.get("source_type")
        if source_type == "news_headline":
            row["news_headline_count"] += 1
        elif source_type == "earnings_transcript":
            row["earnings_transcript_count"] += 1
        elif source_type == "report_pdf_page":
            row["report_pdf_page_count"] += 1
        elif source_type == "futu_ai_summary":
            row["futu_ai_summary_count"] += 1
        text = event.get("text") if isinstance(event.get("text"), str) else ""
        row["text_char_count"] += len(text)
        publisher = event.get("publisher")
        if isinstance(publisher, str) and publisher:
            row["publisher_count"].add(publisher)
        row["ai_generated_count"] += int(event.get("ai_generated") is True)
        row["issuer_verified_count"] += int(event.get("issuer_verified") is True)
        row["uncertainty_term_count"] += _term_count(text, TERM_GROUPS["uncertainty"])
        row["guidance_term_count"] += _term_count(text, TERM_GROUPS["guidance"])
        row["catalyst_term_count"] += _term_count(text, TERM_GROUPS["catalyst"])
        row["pressure_term_count"] += _term_count(text, TERM_GROUPS["pressure"])
        if date_basis == "available_at" and event.get("point_in_time_safe") is not True:
            row["pit_safe"] = 0
    rows = []
    for key in sorted(grouped):
        row = grouped[key]
        row["publisher_count"] = len(row["publisher_count"])
        rows.append(row)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "date_basis": date_basis,
        "point_in_time_safe": date_basis == "available_at" and skipped == 0 and all(row["pit_safe"] for row in rows),
        "skipped_events_without_date": skipped,
        "warnings": [
            "Futu publication timestamps do not prove first availability to a strategy.",
            "AI summaries and transcripts are not issuer-verified unless their source metadata says otherwise.",
            "Lexical counts are research features, not sentiment labels or investment recommendations.",
        ],
    }
    return rows, manifest


def write_events_jsonl(events: Iterable[Mapping[str, Any]], path: Path | str) -> Path:
    destination = Path(path).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        for event in events:
            handle.write(json.dumps(dict(event), ensure_ascii=False, sort_keys=True) + "\n")
    os.replace(temporary, destination)
    return destination


def write_feature_csv(rows: Iterable[Mapping[str, Any]], path: Path | str) -> Path:
    rows = [dict(row) for row in rows]
    destination = Path(path).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        raise ResearchFeatureError("Cannot write an empty feature CSV.")
    fields = list(rows[0])
    temporary = destination.with_name(destination.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temporary, destination)
    return destination


def merge_features_into_qlib_source(source_dir: Path | str, rows: Iterable[Mapping[str, Any]], *,
                                     overwrite: bool = False) -> dict[str, Any]:
    """Merge daily research features into existing per-instrument Qlib CSVs.

    The source directory is expected to contain the files produced by
    ``futu_bridge.qlib_export``. Existing OHLCV/factor fields are protected;
    feature names may be added or replaced only with ``overwrite=True``.
    """
    import csv

    destination = Path(source_dir).expanduser().resolve()
    if not destination.is_dir():
        raise ResearchFeatureError("Qlib source directory does not exist.")
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        symbol = row.get("symbol")
        date = row.get("date")
        if not isinstance(symbol, str) or not symbol or not isinstance(date, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            raise ResearchFeatureError("Feature rows require symbol and YYYY-MM-DD date.")
        grouped.setdefault(symbol, []).append(dict(row))
    updated = []
    for symbol, feature_rows in grouped.items():
        instrument = symbol
        if "." in instrument:
            prefix, code = instrument.split(".", 1)
            instrument = code if prefix.upper() == "US" else prefix.upper() + "_" + code
        instrument = re.sub(r"[^A-Za-z0-9_]+", "_", instrument).strip("_").upper()
        path = destination / (instrument + ".csv")
        if not path.is_file():
            raise ResearchFeatureError(f"No Qlib price CSV exists for feature symbol {symbol!r}: {path.name}")
        with path.open("r", encoding="utf-8", newline="") as handle:
            existing = list(csv.DictReader(handle))
        if not existing:
            raise ResearchFeatureError(f"Qlib price CSV is empty: {path.name}")
        by_date = {row.get("date"): row for row in feature_rows}
        if len(by_date) != len(feature_rows):
            raise ResearchFeatureError(f"Duplicate feature dates for {symbol!r}.")
        feature_fields = sorted({key for row in feature_rows for key in row if key not in ("symbol", "date")})
        for field in feature_fields:
            if field in {"open", "close", "high", "low", "volume", "factor"}:
                raise ResearchFeatureError(f"Research feature cannot overwrite protected Qlib field {field!r}.")
            if field in existing[0] and not overwrite:
                raise ResearchFeatureError(f"Feature field already exists in {path.name}: {field!r}")
        missing_dates = sorted(set(by_date) - {row.get("date") for row in existing})
        if missing_dates:
            raise ResearchFeatureError(f"Feature dates are absent from {path.name}: {missing_dates[:3]}")
        fieldnames = list(existing[0])
        for field in feature_fields:
            if field not in fieldnames:
                fieldnames.append(field)
        for row in existing:
            feature = by_date.get(row.get("date"))
            if feature is None:
                for field in feature_fields:
                    row.setdefault(field, "")
            else:
                for field in feature_fields:
                    row[field] = feature.get(field, "")
        temporary = path.with_name(path.name + ".tmp")
        with temporary.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
            writer.writeheader()
            writer.writerows(existing)
        os.replace(temporary, path)
        updated.append({"symbol": symbol, "instrument": instrument, "path": str(path),
                        "rows": len(existing), "fields": feature_fields})
    return {"source_dir": str(destination), "updated": updated, "feature_count": len(grouped)}
