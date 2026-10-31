"""Bounded metadata-only decoder for Futu desktop stock news protocol 7912.

The field mapping is recovered from FTNews.dll 16.33.17808 and checked against
an AAPL response. Article bodies, summaries, audio and tracking maps are omitted.
"""
import datetime as dt
from urllib.parse import urlsplit

from .wire import fields, signed


def _text(value, *, limit=8192):
    if not isinstance(value, bytes) or len(value) > limit:
        raise ValueError("Invalid bounded news text")
    return value.decode("utf-8")


def _url(value):
    text = _text(value, limit=8192)
    parsed = urlsplit(text)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("News URL must be an HTTP(S) link without credentials")
    return text


def parse_stock_news(payload):
    """Return news metadata and the server's opaque next-page cursor."""
    parts = fields(payload)
    head = {n: v for n, w, v in parts if w == 0}
    strings = {n: v for n, w, v in parts if w == 2 and n != 4}
    if 1 not in head:
        raise ValueError("News response lacks its result code")
    result = {"result": signed(head[1]), "message": _text(strings.get(2, b"")),
              "stock_id": str(head[3]) if 3 in head else None,
              "has_more": bool(head.get(5, 0)),
              "next_cursor": _text(strings[6], limit=2048) if strings.get(6) else None,
              "items": []}
    if result["result"] not in (0, 1):
        return result
    if result["stock_id"] is None:
        raise ValueError("Successful news response lacks its stock ID")
    if result["next_cursor"] and (not result["next_cursor"].strip() or
                                  any(ord(char) < 32 or ord(char) == 127 for char in result["next_cursor"])):
        raise ValueError("News continuation cursor contains control characters")
    if result["result"] == 1 and (result["has_more"] or any(n == 4 and w == 2 for n, w, _ in parts)):
        raise ValueError("End-of-pages news response contains conflicting data")
    for number, wire, body in parts:
        if number != 4 or wire != 2:
            continue
        if len(result["items"]) >= 200:
            raise ValueError("News response exceeds the bounded item count")
        item_fields = fields(body)
        integers = {n: v for n, w, v in item_fields if w == 0}
        texts = {n: v for n, w, v in item_fields if w == 2 and n in (3, 4, 5, 18, 25)}
        # Field25 is the server's string ID; preserve it without numeric coercion.
        identifier = _text(texts[25], limit=256) if texts.get(25) else str(integers.get(1, ""))
        if not identifier or not texts.get(3):
            raise ValueError("News item lacks an identifier or headline")
        timestamp = integers.get(2)
        if timestamp is not None and not 0 <= timestamp <= 4102444800:
            raise ValueError("News publication timestamp is outside the supported range")
        result["items"].append({
            "news_id": identifier,
            "headline": _text(texts[3]),
            "published_at": dt.datetime.fromtimestamp(timestamp, dt.timezone.utc).isoformat().replace("+00:00", "Z") if timestamp is not None else None,
            "published_at_unix": timestamp,
            "url": _url(texts[4]) if texts.get(4) else None,
            "publisher": _text(texts[5]) if texts.get(5) else None,
            "language": _text(texts[18], limit=64) if texts.get(18) else None,
        })
    return result
