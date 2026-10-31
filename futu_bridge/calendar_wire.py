"""Metadata decoder for the verified desktop financial-calendar list (20024).

The reported date and fiscal label are provider data. Unmapped native flags are
preserved as raw values, never interpreted as confirmation or estimate status.
"""
import datetime as dt
import re

from .wire import fields, signed


def _single(parts, number, wire, default=None):
    values = [(w, v) for n, w, v in parts if n == number]
    if len(values) > 1 or (values and values[0][0] != wire):
        raise ValueError("Duplicate or invalid calendar scalar field")
    return values[0][1] if values else default


def _text(value, maximum=1024):
    if not isinstance(value, bytes) or len(value) > maximum:
        raise ValueError("Invalid calendar text")
    return value.decode("utf-8")


def _date(value):
    raw = _text(value, 8)
    if not re.fullmatch(r"\d{8}", raw):
        raise ValueError("Invalid native calendar date")
    return dt.datetime.strptime(raw, "%Y%m%d").date().isoformat()


def event_key(item):
    return (item["date"], item["stock_id"], item["fiscal_period_label"],
            item["native_type_raw"], item["native_subtype_raw"], item["provider_timestamp_unix"])


def _item(payload, date):
    parts = fields(payload)
    stock_id = _single(parts, 1, 0)
    if stock_id is None or stock_id <= 0:
        raise ValueError("Calendar item lacks a valid security identity")
    timestamp = _single(parts, 11, 0)
    if timestamp is not None and not 0 <= timestamp <= 4102444800:
        raise ValueError("Calendar timestamp outside supported range")
    flag = _single(parts, 30, 0)
    if flag is not None and flag not in (0, 1):
        raise ValueError("Calendar boolean flag is invalid")
    period = _single(parts, 14, 2)
    period = fields(period) if period is not None else []
    label = _single(parts, 7, 2)
    return {
        "date": date, "stock_id": str(stock_id),
        "fiscal_period_label": _text(label, 128) if label is not None else None,
        "fiscal_year": _single(period, 1, 0), "fiscal_period_raw": _single(period, 2, 0),
        "provider_timestamp_unix": timestamp,
        "provider_timestamp_utc": dt.datetime.fromtimestamp(timestamp, dt.timezone.utc).isoformat().replace("+00:00", "Z") if timestamp is not None else None,
        "provider_timestamp_meaning": "unverified",
        "native_type_raw": _single(parts, 2, 0), "native_subtype_raw": _single(parts, 3, 0),
        "native_flag_30": bool(flag) if flag is not None else None,
        "confirmed": None, "estimated": None,
    }


def parse_earnings_calendar(payload, *, protocol=20024):
    """Decode one list page; group totals apply across all pages, not this page."""
    if protocol not in (20024, 20025):
        raise ValueError("Unsupported calendar protocol")
    parts = fields(payload)
    result = _single(parts, 1, 0)
    if result is None:
        raise ValueError("Calendar response lacks a result code")
    decoded = {"result": signed(result), "message": _text(_single(parts, 2, 2, b"")), "groups": []}
    if decoded["result"] != 0:
        return decoded
    group_field = 4 if protocol == 20024 else 3
    dates, seen, count = set(), set(), 0
    for number, wire, body in parts:
        if number != group_field:
            continue
        if wire != 2:
            raise ValueError("Invalid calendar group wire type")
        group = fields(body)
        date = _date(_single(group, 3, 2))
        if date in dates or len(dates) >= 31:
            raise ValueError("Duplicate or excessive calendar date groups")
        dates.add(date)
        total = _single(group, 2, 0)
        if total is None or not 0 <= total <= 1000000:
            raise ValueError("Calendar group lacks a valid total")
        items = []
        for n, w, item_body in group:
            if n != 1:
                continue
            if w != 2:
                raise ValueError("Invalid calendar item wire type")
            item = _item(item_body, date)
            key = event_key(item)
            if key in seen:
                raise ValueError("Duplicate calendar event within one page")
            seen.add(key)
            items.append(item)
            count += 1
            if count > (1000 if protocol == 20024 else 186):
                raise ValueError("Calendar response exceeds requested record bounds")
        if len(items) > total or (protocol == 20025 and len(items) > 6):
            raise ValueError("Calendar item count exceeds its declared bounds")
        decoded["groups"].append({"date": date, "total_events": total, "items": items})
    decoded["returned_count"] = count
    return decoded
