"""Bounded protobuf wire decoding for recovered, version-pinned quote schemas."""
import datetime as dt
import struct


def varint(data, offset):
    if type(offset) is not int or not 0 <= offset <= len(data):
        raise ValueError("Invalid protobuf integer offset")
    value = 0
    for shift in range(0, 70, 7):
        if offset >= len(data):
            raise ValueError("Truncated protobuf integer")
        byte = data[offset]
        offset += 1
        value |= (byte & 127) << shift
        if byte < 128:
            if value >= 1 << 64:
                raise ValueError("Protobuf integer exceeds 64 bits")
            return value, offset
    raise ValueError("Invalid protobuf integer")


def fields(data):
    if len(data) > 2 * 1024 * 1024:
        raise ValueError("Quote payload is too large")
    offset, result = 0, []
    while offset < len(data):
        tag, offset = varint(data, offset)
        number, wire = tag >> 3, tag & 7
        if not 1 <= number <= (1 << 29) - 1:
            raise ValueError("Invalid protobuf field number")
        if wire == 0:
            value, offset = varint(data, offset)
        elif wire in (1, 5):
            size = 8 if wire == 1 else 4
            if offset + size > len(data):
                raise ValueError("Truncated fixed-width protobuf field")
            value = struct.unpack_from("<Q" if size == 8 else "<I", data, offset)[0]
            offset += size
        elif wire == 2:
            size, offset = varint(data, offset)
            if offset + size > len(data):
                raise ValueError("Truncated protobuf payload")
            value = data[offset:offset + size]
            offset += size
        else:
            raise ValueError("Unsupported quote protobuf wire type")
        result.append((number, wire, value))
    return result


def signed(value):
    return value - (1 << 64) if value >= 1 << 63 else value


def _signed32(value):
    """Protobuf int32 decoding truncates to its low 32 bits before signing."""
    value &= (1 << 32) - 1
    return value - (1 << 32) if value >= 1 << 31 else value


def _singular_integers(parts, known, context):
    """Reject ambiguous critical values instead of silently choosing the last.

    Identical repetitions are legal protobuf and are harmless. Unknown fields
    remain available to the caller; only schema-verified scalar fields are
    checked for conflicting repetitions or an unexpected wire type here.
    """
    values = {}
    for number, wire, value in parts:
        if number in known:
            if wire != 0:
                raise ValueError(context + " has an invalid scalar wire type")
            if number in values and values[number] != value:
                raise ValueError(context + " has conflicting singular fields")
        if wire == 0:
            values[number] = value
    return values


def _length_delimited(parts, number, context, singular=False):
    values = []
    for n, wire, value in parts:
        if n != number:
            continue
        if wire != 2:
            raise ValueError(context + " has an invalid message/bytes wire type")
        if singular and values and values[0] != value:
            raise ValueError(context + " has conflicting singular bytes")
        values.append(value)
    return values


def parse_quote_snapshots(payload):
    result = []
    for body in _length_delimited(fields(payload), 1, "Quote response"):
        security = fields(body)
        head = _singular_integers(security, {1, 3}, "Quote security")
        item = {"stock_id": str(head.get(1, 0)), "exists": False, "raw": {},
                "origin": "desktop_session_quote_request"}
        for bit_body in _length_delimited(security, 2, "Quote security"):
            bit_fields = fields(bit_body)
            bit = _singular_integers(bit_fields, {1, 3}, "Quote bit header").get(1)
            for data in _length_delimited(bit_fields, 2, "Quote bit data", singular=True):
                values = _singular_integers(fields(data), set(range(1, 7)) if bit == 0 else set(),
                                            "Nominal quote")
                if bit == 0:
                    names = {1:"PriceNominal", 2:"PriceLastClose", 3:"ServerSendTimeMs",
                             4:"ExchangeTime", 5:"ServerRecvTimeMs", 6:"NotCalDelay"}
                    item["raw"] = {name:str(signed(values[n])) for n,name in names.items()
                                   if n in values and values[n] != (1 << 63) - 1}
                    item["exists"] = "PriceNominal" in item["raw"]
                elif bit == 1:
                    item["stock_state_raw"] = {str(n):v for n,v in values.items()}
        result.append(item)
    return result


def parse_bars(payload):
    wire = fields(payload)
    head = _singular_integers(wire, {1, 2, 3, 4, 5, 6, 8, 9, 10, 11, 12, 13}, "Historical response")
    result = {"result": _signed32(head.get(1, 0)), "stock_id": str(head.get(2, 0)),
              "kline_type": head.get(3), "adjustment_code": head.get(4),
              "range_type": head.get(6), "has_more": bool(head.get(8, 0)),
              "next_time": head.get(9), "has_older_item": bool(head.get(10, 0)),
              "volume_precision": head.get(12), "bars": []}
    result["kline_type_prob"] = head.get(11, 0)
    precision = result["volume_precision"]
    if precision is not None and not 0 <= precision <= 12:
        raise ValueError("Historical volume precision is outside the supported 0..12 range")
    for body in _length_delimited(wire, 7, "Historical bars collection"):
        parts = fields(body)
        item = _singular_integers(parts, set(range(1, 24)), "Historical bar")
        if any(n not in item for n in (1, 3, 4, 5, 6)):
            raise ValueError("Historical bar lacks required timestamp/OHLC fields")
        def price(n):
            value = signed(item[n])
            return None if value == (1 << 63) - 1 else value / 1_000_000_000
        timezone_offset = _signed32(item.get(2, 0))
        if not -14 <= timezone_offset <= 14:
            raise ValueError("Historical bar timezone is outside the supported -14..14 hour range")
        try:
            timestamp = dt.datetime.fromtimestamp(item[1], dt.timezone.utc)
            if 23 in item:
                date_text = str(item[23])
                if len(date_text) != 8:
                    raise ValueError("Historical bar market date must contain eight digits")
                date = dt.datetime.strptime(date_text, "%Y%m%d").date().isoformat()
            else:
                date = (timestamp + dt.timedelta(hours=timezone_offset)).date().isoformat()
        except (OverflowError, OSError, ValueError) as exc:
            raise ValueError("Historical bar timestamp or market date is invalid") from exc
        volume_raw = item.get(7)
        volume = volume_raw / (10 ** precision) if volume_raw is not None and precision is not None and 0 <= precision <= 12 else None
        row = {"timestamp": timestamp.isoformat().replace("+00:00", "Z"),
               "timestamp_seconds": item[1], "date": date, "timezone_offset_hours": timezone_offset,
               "open": price(4), "high": price(5), "low": price(6), "close": price(3),
               "volume": volume, "volume_raw": volume_raw, "volume_precision": precision,
               "turnover_raw": item.get(8), "previous_close": price(11) if 11 in item else None,
               "trade_section": _signed32(item[21]) if 21 in item else None,
               "point_type": _signed32(item[22]) if 22 in item else None,
               "raw_integer_fields": {str(n):v for n,v in item.items()},
               "raw_text_fields": {str(n):v.decode("utf-8", "replace") for n,w,v in parts if w == 2}}
        extensions = [{"field": n, "wire": w, "value": v.hex() if w == 2 else v}
                      for n, w, v in parts if n > 23]
        if extensions:
            # The installed KlineItem descriptor stops at field23. Preserve
            # newer extensions exactly, including duplicates and binary text,
            # without assigning inferred volume/turnover semantics to 24/25.
            row["raw_extension_fields"] = extensions
        values = [row[key] for key in ("open", "high", "low", "close")]
        if any(value is None for value in values) or not row["low"] <= min(row["open"], row["close"]) <= max(row["open"], row["close"]) <= row["high"]:
            raise ValueError("Historical bar fails OHLC bounds")
        result["bars"].append(row)
    result["bars"].sort(key=lambda row: row["timestamp_seconds"])
    return result
