"""Decode version-pinned desktop quote pushes without inventing continuity.

Protocol 6212 wraps SecurityQuote/BitQuote just like FetchQuoteRsp. Its bit35
payload is AccumulateData.Tick, not the TickRsp used by the pull endpoint.
Book bits3/4 and price bit0 are partial updates. State belongs to one local
subscription and must be reset whenever its source session/epoch changes.

Provenance: coverage_audit/push_test/push_schemas.json and push_decode_abi.json.
The recovered schemas provide no verified exchange sequence or replay cursor.
"""

from copy import deepcopy

from .feed_wire import _book_levels, _raw, _signed32, parse_trades
from .wire import fields, signed


_MISSING = (1 << 63) - 1


def _integers(parts):
    result = {}
    for number, wire, value in parts:
        if wire == 0:
            if number in result:
                raise ValueError("Duplicate scalar in native quote push")
            result[number] = value
    return result


def _varint(value):
    out = bytearray()
    while value >= 128:
        out.append((value & 127) | 128)
        value >>= 7
    out.append(value)
    return bytes(out)


def _integer(number, value):
    return _varint(number << 3) + _varint(value)


def _message(number, body):
    return _varint((number << 3) | 2) + _varint(len(body)) + body


def _ticks(stock_id, broker_id, payload):
    """Reuse the verified TickItem decoder, mapping only the push envelope."""
    parts = fields(payload)
    values = _integers(parts)
    rows = [body for n, w, body in parts if n == 3 and w == 2]
    converted = _integer(1, int(stock_id)) + _integer(2, 0) + _integer(6, len(rows))
    for source, target in ((1, 3), (4, 8), (5, 9)):
        if source in values:
            converted += _integer(target, values[source])
    if broker_id is not None:
        converted += _integer(11, broker_id & ((1 << 64) - 1))
    for row in rows:
        converted += _message(7, row)
    result = parse_trades(converted, stock_id)
    result.update(result=None, exists=bool(rows), source_kind="desktop_native_public_trades_push",
                  result_semantics="push_has_no_response_status", raw_header={str(k): v for k, v in values.items()},
                  raw_push_fields=_raw(parts), source_sequence=None,
                  server_send_time_ms=str(signed(values[2])) if 2 in values else None)
    return result


class PushDecoder:
    """Maintain price/book patches; emit independent trade batches.

    ``decode`` returns dictionaries with ``kind``, ``stock_id`` and ``raw``.
    Raw records fit native.normalize_quote/book/trades. ``state_complete`` for
    books means that both sides have been observed since reset, not that every
    exchange event, venue, price level or order has been observed.
    """

    def __init__(self, depth=10, max_states=1024):
        if type(depth) is not int or not 1 <= depth <= 50:
            raise ValueError("Expected book depth 1..50")
        if type(max_states) is not int or not 1 <= max_states <= 10000:
            raise ValueError("Expected max_states 1..10000")
        self.depth = depth
        self.max_states = max_states
        self._states = {}

    def reset(self):
        self._states.clear()

    def _state(self, key):
        if key not in self._states:
            if len(self._states) >= self.max_states:
                raise ValueError("Native push state limit exceeded")
            self._states[key] = {}
        return self._states[key]

    def decode(self, payload, allowed_stock_ids=None):
        """Decode an entire packet atomically, optionally filtering securities.

        Unrequested securities are skipped before decoding their market fields.
        Malformed requested data raises ValueError and leaves old state intact.
        Unknown market bits are ignored; raw extensions of supported bits remain
        available in the result. No trade-ID arithmetic is used for gap inference.
        """
        allowed = None if allowed_stock_ids is None else {str(value) for value in allowed_stock_ids}
        previous = deepcopy(self._states)
        try:
            return self._decode(payload, allowed)
        except (TypeError, ValueError, OverflowError):
            self._states = previous
            raise

    def _decode(self, payload, allowed):
        output = []
        outer = fields(payload)
        for n, w, security_body in outer:
            if n != 1 or w != 2:
                continue
            security = fields(security_body)
            header = _integers(security)
            if not header.get(1):
                raise ValueError("Native quote push lacks a security identity")
            stock_id = str(header[1])
            if allowed is not None and stock_id not in allowed:
                continue
            broker = _signed32(header[3]) if 3 in header else None
            for number, wire, bit_body in security:
                if number != 2 or wire != 2:
                    continue
                bit_parts = fields(bit_body)
                bit_header = _integers(bit_parts)
                bit = bit_header.get(1)
                if bit is None:
                    raise ValueError("Native quote push lacks a bit identity")
                if bit not in (0, 3, 4, 35):
                    continue
                bodies = [v for n, w, v in bit_parts if n == 2 and w == 2]
                if len(bodies) != 1:
                    raise ValueError("Native quote push must have one bit payload")
                prob = signed(bit_header[3]) if 3 in bit_header else None
                key = (stock_id, broker, bit, prob)
                metadata = {"stock_id": stock_id, "bit": bit, "prob": prob, "broker_id": broker,
                            "source_sequence": None, "initial_snapshot": None,
                            "raw_security_header": {str(k): v for k, v in header.items()},
                            "raw_bit_header": {str(k): v for k, v in bit_header.items()},
                            "raw_response_header": {str(n): v for n, w, v in outer if w == 0}}
                if bit == 35:
                    raw = _ticks(stock_id, broker, bodies[0])
                    output.append(dict(metadata, kind="trades", raw=raw,
                                       update_kind="trade_batch", state_complete=False))
                elif bit == 0:
                    output.append(self._price(key, bodies[0], metadata))
                else:
                    output.append(self._book(key, bodies[0], metadata))
        return output

    def _price(self, key, payload, metadata):
        parts = fields(payload)
        values = _integers(parts)
        state = self._state(key)
        names = {1: "PriceNominal", 2: "PriceLastClose", 3: "ServerSendTimeMs",
                 4: "ExchangeTime", 5: "ServerRecvTimeMs", 6: "NotCalDelay"}
        changed = []
        for number, name in names.items():
            if number in values:
                state[name] = str(signed(values[number])) if values[number] != _MISSING else None
                changed.append(name)
        complete = state.get("PriceNominal") is not None
        raw = {"stock_id": metadata["stock_id"], "exists": complete, "raw": dict(state),
               "source_kind": "desktop_native_quote_push", "raw_push_fields": _raw(parts)}
        return dict(metadata, kind="quote", raw=raw, update_kind="field_patch",
                    updated_fields=changed, state_complete=complete)

    def _book(self, key, payload, metadata):
        parts = fields(payload)
        values = _integers(parts)
        state = self._state(key)
        bit = metadata["bit"]
        precision_field = 10 if bit == 3 else 6
        if precision_field in values:
            state["volume_precision"] = _signed32(values[precision_field])
        precision = state.get("volume_precision")
        changed = []
        flags = {}
        if bit == 3:
            # Native decoder0x121a44/0x1221c5 ignores absent/zero side flags.
            # Nonzero flags allocate a fresh side vector of exactly bid/ask_size.
            for side, side_field, flag_field in (("bids", 1, 5), ("asks", 2, 6)):
                flag = _signed32(values[flag_field]) if flag_field in values else None
                flags[side] = flag
                if flag is not None and flag != 0:
                    state[side] = _book_levels(parts, side_field, precision, self.depth)
                    changed.append(side)
            names = {3: "SvrSendToClientTime", 4: "ExchangeTime", 7: "SvrRecvBidTime", 8: "SvrRecvAskTime"}
            times = state.setdefault("raw_times", {})
            times.update({name: str(signed(values[n])) for n, name in names.items() if n in values})
        else:
            # Simple-book price and size fields may arrive independently.
            scalars = state.setdefault("scalars", {})
            scalars.update({n: values[n] for n in (1, 2, 3, 4) if n in values})
            for side, price_field, volume_field in (("bids", 1, 3), ("asks", 2, 4)):
                if price_field not in values and volume_field not in values:
                    continue
                changed.append(side)
                if price_field not in scalars or volume_field not in scalars:
                    continue
                price, volume = signed(scalars[price_field]), signed(scalars[volume_field])
                if price == _MISSING or volume == _MISSING or volume < 0:
                    state[side] = []
                else:
                    state[side] = [{"level": 1, "price_raw": str(price), "volume_raw": str(volume),
                                    "volume_precision": precision, "order_count": None}]
        # Precision is security/book metadata, including unchanged levels.
        for side in ("bids", "asks"):
            for level in state.get(side, []):
                level["volume_precision"] = precision
        complete = "bids" in state and "asks" in state
        raw = {"stock_id": metadata["stock_id"], "exists": bool(changed) or complete,
               "bids": deepcopy(state.get("bids", [])), "asks": deepcopy(state.get("asks", [])),
               "book_kind": "top_of_book" if bit == 4 or metadata["prob"] in (4, 8, 16) else "depth",
               "native_book_prob": metadata["prob"],
               "broker_id": metadata["broker_id"], "volume_precision": precision,
               "raw_times": dict(state.get("raw_times", {})), "raw_push_fields": _raw(parts),
               "side_flags": flags, "updated_sides": changed, "state_complete": complete,
               "source_kind": "desktop_native_order_book_push"}
        return dict(metadata, kind="orderbook", raw=raw, updated_sides=changed,
                    update_kind="side_replacement" if bit == 3 else "field_patch", state_complete=complete)
