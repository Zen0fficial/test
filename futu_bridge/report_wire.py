"""Verified report page metadata decoder; never exposes personalized URL values."""
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .calendar_wire import _single, _text
from .wire import fields, signed


def public_report_url(value):
    if not isinstance(value, str) or len(value) > 8192 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("Invalid report URL")
    parts = urlsplit(value)
    if parts.scheme != "https" or parts.username or parts.password or parts.port not in (None, 443):
        raise ValueError("Unsupported report URL")
    if (parts.hostname, parts.path) not in (("earningshub.futunn.com", "/company"),
                                          ("www.moomoo.com", "/earningshub/company")):
        raise ValueError("Unverified report host or route")
    pairs = [(k, v) for k, v in parse_qsl(parts.query, strict_parsing=True, max_num_fields=100)
             if k in ("id", "period", "stock_id")]
    query = dict(pairs)
    if len(pairs) != len(query) or set(query) != {"id", "period", "stock_id"}:
        raise ValueError("Report URL lacks unique public identity fields")
    if not re.fullmatch(r"[1-9]\d{0,24}", query["id"]) or not re.fullmatch(r"[1-9]\d{0,19}", query["stock_id"]):
        raise ValueError("Report URL has invalid identities")
    if not re.fullmatch(r"\d{4}Q[1-4]", query["period"]):
        raise ValueError("Invalid report URL period")
    url = urlunsplit(("https", parts.hostname, parts.path, urlencode(pairs), ""))
    return {"url": url, "url_period": query["period"], "url_stock_id": query["stock_id"], "report_id": query["id"]}


def parse_report_link(payload):
    outer = fields(payload)
    body = _single(outer, 1, 2)
    if body is None:
        raise ValueError("Report response lacks batch body")
    batch = fields(body)
    result = _single(batch, 1, 0)
    if result is None:
        raise ValueError("Report response lacks result")
    parsed = {"result": signed(result), "message": _text(_single(batch, 2, 2, b"")), "items": []}
    if parsed["result"] != 0:
        return parsed
    for number, wire, value in batch:
        if number != 3:
            continue
        if wire != 2 or parsed["items"]:
            raise ValueError("Report response exceeds one requested item")
        item = fields(value)
        stock = _single(item, 1, 0)
        if stock is None or stock <= 0:
            raise ValueError("Invalid report stock identity")
        period = _text(_single(item, 2, 2), 16)
        if not re.fullmatch(r"\d{4}Q[1-4]", period):
            raise ValueError("Invalid report fiscal period")
        raw_url = _text(_single(item, 3, 2, b""), 8192)
        normalized = public_report_url(raw_url) if raw_url else {"url": None, "url_period": None, "url_stock_id": None, "report_id": None}
        if normalized["url_stock_id"] not in (None, str(stock)):
            raise ValueError("Report URL stock identity differs from response")
        parsed["items"].append(dict(normalized, stock_id=str(stock), fiscal_period=period))
    return parsed
