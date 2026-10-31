"""Decode recorded, version-pinned desktop book and public-tape responses.

The outputs preserve native integers for native.normalize_book/normalize_trades.
Unknown server extensions are retained rather than assigned guessed semantics.
Schema provenance: coverage_audit/evidence/native_feed_request_abi.json.
"""

from decimal import Decimal, InvalidOperation

from .wire import fields, signed


def _integers(parts):
    return {number: value for number, wire, value in parts if wire == 0}


def _signed32(value):
    value &= (1 << 32) - 1
    return value - (1 << 32) if value >= 1 << 31 else value


def _identity(actual, expected):
    actual = str(actual) if actual is not None else None
    if expected is not None and actual != str(expected):
        raise ValueError("Market-data reply security ID does not match the requested instrument")
    return actual


def _raw(parts):
    return [{"field": n, "wire": w, "value": v.hex() if isinstance(v, bytes) else v}
            for n, w, v in parts]


def _book_levels(parts, side_field, precision, depth):
    rows = []
    for number, wire, body in parts:
        if number != side_field or wire != 2:
            continue
        item_parts = fields(body)
        item = _integers(item_parts)
        if 1 not in item or 2 not in item:
            raise ValueError("Book level lacks required price or volume")
        price, volume = signed(item[1]), signed(item[2])
        if price == (1 << 63) - 1 or volume < 0 or volume == (1 << 63) - 1:
            continue
        rows.append({"level": len(rows) + 1, "price_raw": str(price),
                     "volume_raw": str(volume), "volume_precision": precision,
                     "order_count": item.get(3), "raw_fields": _raw(item_parts)})
        if len(rows) >= depth:
            break
    return rows


def parse_book(payload, expected_stock_id=None, depth=10):
    """Decode a single-security FetchQuoteRsp, preserving simple and full books.

    The modern response contains an additional top-level field2 absent from the
    recovered descriptor. It remains raw; its meaning is not asserted here.
    """
    if not isinstance(depth, int) or isinstance(depth, bool) or not 1 <= depth <= 50:
        raise ValueError("Expected book depth 1..50")
    outer = fields(payload)
    securities = [fields(v) for n, w, v in outer if n == 1 and w == 2]
    if len(securities) != 1:
        raise ValueError("Expected exactly one security in the order-book reply")
    security = securities[0]
    header = _integers(security)
    stock_id = _identity(header.get(1), expected_stock_id)
    if not stock_id or stock_id == "0":
        raise ValueError("Order-book reply lacks a security identity")
    output = {"stock_id": stock_id, "exists": False, "bids": [], "asks": [],
              "book_kind": None, "raw_times": {}, "broker_id": header.get(3),
              "raw_response_header": {str(n): v for n, w, v in outer if w == 0},
              "source_kind": "desktop_session_order_book_request", "books": []}
    for number, wire, body in security:
        if number != 2 or wire != 2:
            continue
        bit_parts = fields(body)
        bit_header = _integers(bit_parts)
        bit = bit_header.get(1)
        if bit not in (3, 4):
            continue
        for n, w, bit_body in bit_parts:
            if n != 2 or w != 2:
                continue
            parts = fields(bit_body)
            values = _integers(parts)
            book = {"bit": bit, "prob": bit_header.get(3), "bids": [], "asks": [],
                    "raw_fields": _raw(parts), "raw_times": {}}
            if bit == 3:
                precision = _signed32(values[10]) if 10 in values else None
                book.update(book_kind="depth", volume_precision=precision,
                            bids=_book_levels(parts, 1, precision, depth),
                            asks=_book_levels(parts, 2, precision, depth))
                names = {3: "SvrSendToClientTime", 4: "ExchangeTime", 7: "SvrRecvBidTime", 8: "SvrRecvAskTime"}
                book["raw_times"] = {name: str(signed(values[n])) for n, name in names.items() if n in values}
            else:
                # Current CorePB serializer: precision field6; older embedded
                # FTQuantServer descriptor only described the first four fields.
                precision = _signed32(values[6]) if 6 in values else None
                book.update(book_kind="top_of_book", volume_precision=precision)
                for side, price_field, volume_field in (("bids", 1, 3), ("asks", 2, 4)):
                    if price_field not in values or volume_field not in values:
                        continue
                    price, volume = signed(values[price_field]), signed(values[volume_field])
                    if price == (1 << 63) - 1 or volume < 0 or volume == (1 << 63) - 1:
                        continue
                    book[side] = [{"level": 1, "price_raw": str(price), "volume_raw": str(volume),
                                   "volume_precision": precision, "order_count": None}]
            output["books"].append(book)
    # Prefer a nonempty full book. Retain simple book independently for auditing.
    populated = [book for book in output["books"] if book["bids"] or book["asks"]]
    if populated:
        selected = next((book for book in populated if book["bit"] == 3), populated[0])
        output.update(exists=True, bids=selected["bids"], asks=selected["asks"],
                      raw_times=selected["raw_times"], book_kind=selected["book_kind"],
                      native_book_prob=selected["prob"], volume_precision=selected["volume_precision"])
    return output


def _volume_decimal(text, raw, precision):
    try:
        if text is not None and text != "":
            value = Decimal(text)
        elif raw is not None and precision is not None and 0 <= precision <= 12:
            value = Decimal(raw).scaleb(-precision)
        else:
            return None
        return format(value, "f") if value.is_finite() and value >= 0 else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def parse_trades(payload, expected_stock_id=None):
    """Decode FTCmdTick.TickRsp; result is signed ZigZag, prices are int64."""
    parts = fields(payload)
    header = _integers(parts)
    # A missing result must not silently become a successful response.
    result = (header[2] >> 1) ^ -(header[2] & 1) if 2 in header else None
    stock_id = _identity(header.get(1), expected_stock_id)
    precision = _signed32(header[9]) if 9 in header else None
    output = {"stock_id": stock_id, "result": result, "exists": False, "rows": [],
              "price_last_close_raw": str(signed(header[3])) if 3 in header else None,
              "date_time_s": header.get(4), "next_ticker_key": str(header[5]) if 5 in header else None,
              "ticker_num": header.get(6), "period": header.get(8),
              "periods": [v for n, w, v in parts if n == 10 and w == 0],
              "volume_precision": precision, "broker_id": _signed32(header[11]) if 11 in header else None,
              "period_name": "regular" if header.get(8, 0) == 0 else "native_" + str(header[8]),
              "total_cached": None, "skipped_indices": [], "complete_history": False,
              "source_kind": "desktop_session_public_trades_request", "order": "server_response_order",
              "raw_header": {str(n): v for n, w, v in parts if w == 0}}
    scalar_names = {1: "tick_key", 2: "type_raw", 3: "exchange_data_time_ms", 4: "price_raw",
                    5: "volume_raw", 7: "server_recv_from_exchange_time_ms", 8: "price_last_close_raw",
                    9: "ticker_type_raw", 11: "ticker_flag_raw", 13: "strategy_raw",
                    14: "order_type_raw", 16: "turnover_raw"}
    text_names = {6: "trade_type", 10: "exchange_name", 12: "cond_code", 22: "hp_volume", 23: "hp_turnover"}
    string_numbers = {1, 3, 4, 5, 7, 8, 14, 16}
    signed_numbers = {4, 7, 8, 16}
    for number, wire, body in parts:
        if number != 7 or wire != 2:
            continue
        item_parts = fields(body)
        values = _integers(item_parts)
        if any(n not in values for n in (1, 3, 4)):
            raise ValueError("Public trade lacks required identity, timestamp, or price")
        row = {"volume_precision": precision, "raw_fields": _raw(item_parts)}
        for n, name in scalar_names.items():
            if n not in values:
                continue
            value = signed(values[n]) if n in signed_numbers else _signed32(values[n]) if n == 2 else values[n]
            row[name] = str(value) if n in string_numbers else value
        for n, w, text in item_parts:
            if w == 2 and n in text_names:
                row[text_names[n]] = text.decode("utf-8", "replace")
        if values[4] == (1 << 63) - 1:
            row["price_raw"] = None
        row["normalized_volume_decimal"] = _volume_decimal(row.get("hp_volume"), row.get("volume_raw"), precision)
        output["rows"].append(row)
    if output["ticker_num"] is not None and output["ticker_num"] != len(output["rows"]):
        raise ValueError("Public-trade response count disagrees with its records")
    output["exists"] = result == 0 and bool(output["rows"])
    return output
