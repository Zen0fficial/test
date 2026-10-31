"""Version-pinned, bounded decoding of actual desktop option responses.

Protocol6311 discovers expiry items;6736 returns combo rows with native option
IDs. Neither a missing symbol nor an unavailable call/put label is fabricated.
Contract names come only from a separate native metadata result for that ID.
"""
import datetime as dt
from decimal import Decimal
import re

from .wire import fields, signed

_OPTION_SYMBOL = re.compile(r"^(?:US\.)?(.+?)(\d{6})([CP])(\d+)$")


def _integers(parts):
    return {n: v for n, w, v in parts if w == 0}


def _ret_code(parts):
    value = _integers(parts).get(1)
    if value is None:
        raise ValueError("Option response lacks a return code")
    return signed(value)


def _date(timestamp):
    return dt.datetime.fromtimestamp(timestamp, dt.timezone.utc).date().isoformat()


def parse_option_expiries(payload, expected_underlying=None, option_market=7):
    """Decode actual server expiry items; preserve unverified attributes as raw."""
    top = fields(payload)
    result = {"ret_code": _ret_code(top), "expiries": [], "count": 0}
    if result["ret_code"] != 0:
        return result
    for number, wire, body in top:
        if number != 9 or wire != 2:
            continue
        parts = fields(body)
        values = _integers(parts)
        if 1 not in values or 9 not in values or values[1] <= 0 or values[9] <= 0:
            raise ValueError("Option expiry lacks an expiry timestamp or underlying ID")
        underlying = str(values[9])
        if expected_underlying is not None and underlying != str(expected_underlying):
            raise ValueError("Option expiry underlying identity mismatch")
        date = _date(values[1] + (8 * 3600 if option_market == 9 else 0))
        # The server also supplies an explicit YYYYMMDD field in this build.
        # Prefer its calendar date: a HK-midnight marker is the preceding UTC
        # day. The timestamp remains available without changing its meaning.
        if 12 in values:
            explicit = dt.datetime.strptime(str(values[12]), "%Y%m%d").date().isoformat()
            date = explicit
        texts = {str(n): v.decode("utf-8", "strict") for n, w, v in parts if w == 2}
        result["expiries"].append({
            "expiry": date, "expiry_timestamp": values[1],
            "underlying_stock_id": underlying,
            "underlying_symbol": texts.get("2"),
            "raw_integer_fields": {str(n): str(signed(v)) for n, v in values.items()},
            "raw_text_fields": texts,
            "source": "desktop_session_protocol6311",
        })
    result["count"] = len(result["expiries"])
    return result


def _metadata_map(metadata):
    if metadata is None:
        return {}
    if isinstance(metadata, dict):
        return {str(key): value for key, value in metadata.items()}
    result = {}
    for row in metadata:
        if row.get("exists") and row.get("metadata"):
            result[str(row["stock_id"])] = row["metadata"]
    return result


def _enrich(contract, meta, option_market):
    if str(meta.get("option_id")) != contract["stock_id"]:
        raise ValueError("Option metadata contract identity mismatch")
    if (contract["underlying_stock_id"] is not None and
            str(meta.get("underlying_stock_id")) != contract["underlying_stock_id"]):
        raise ValueError("Option metadata underlying identity mismatch")
    raw_strike = meta.get("strike_price_raw")
    if raw_strike is not None and int(raw_strike) != int(contract["strike_raw"]):
        raise ValueError("Option metadata strike differs from the chain")
    native_symbol = meta.get("futu_symbol") or meta.get("display_code")
    contract["native_metadata"] = dict(meta)
    contract["futu_symbol"] = meta.get("futu_symbol")
    contract["display_code"] = meta.get("display_code")
    contract["option_type_raw"] = meta.get("option_type_raw")
    contract["identity_status"] = "native_metadata_verified"
    # Parse an actual returned US contract symbol; this does not construct a
    # symbol from a guessed expiry/strike. Require raw-price agreement as well.
    match = _OPTION_SYMBOL.fullmatch(native_symbol or "") if option_market == 7 else None
    if match:
        root, date_code, side, strike_code = match.groups()
        expiry = dt.datetime.strptime(date_code, "%y%m%d").date().isoformat()
        if contract["expiry"] is not None and expiry != contract["expiry"]:
            raise ValueError("Native option symbol expiry differs from the requested chain")
        strike = Decimal(strike_code) / Decimal(1000)
        if Decimal(contract["strike_raw"]) != strike * Decimal(1_000_000_000):
            raise ValueError("Native option symbol and chain strike scale disagree")
        contract.update({"symbol": native_symbol if native_symbol.startswith("US.") else "US." + native_symbol,
                         "expiry": expiry, "side": "call" if side == "C" else "put",
                         "strike": float(strike), "strike_scale": 1_000_000_000,
                         "underlying_symbol": root,
                         "label_provenance": "Native metadata symbol, matched to ID, underlying, expiry and raw strike"})
    return contract


def parse_option_chain(payload, *, underlying_stock_id=None, expiry=None,
                       option_market=7, metadata=None):
    """Decode native IDs, optionally enriching with identity-matched metadata.

    ``expiry`` must be the actual selected server expiry used for the request.
    Without metadata, side and symbol remain null: combo field3/field5 group
    positions alone are not sufficient evidence of call/put identity.
    """
    if expiry is not None:
        dt.date.fromisoformat(expiry)
    top = fields(payload)
    result = {"ret_code": _ret_code(top), "contracts": [], "count": 0,
              "combo_count": 0, "underlying_stock_id": str(underlying_stock_id) if underlying_stock_id is not None else None,
              "expiry": expiry}
    if result["ret_code"] != 0:
        return result
    metadata_by_id = _metadata_map(metadata)
    seen = set()
    for number, wire, body in top:
        if number != 2 or wire != 2:
            continue
        combo = fields(body)
        combo_scalars = _integers(combo)
        result["combo_count"] += 1
        for group, kind, leg_body in combo:
            if group not in (3, 5) or kind != 2:
                continue
            leg = _integers(fields(leg_body))
            if not leg.get(1) or 2 not in leg:
                raise ValueError("Option chain leg lacks ID or strike")
            stock_id = str(leg[1])
            if stock_id in seen:
                raise ValueError("Duplicate option ID in single-strategy chain")
            seen.add(stock_id)
            raw_strike = signed(leg[2])
            if raw_strike < 0 or raw_strike == (1 << 63) - 1:
                raise ValueError("Option chain has an invalid strike")
            row = {"stock_id": stock_id, "symbol": None, "side": None, "strike": None,
                   "strike_raw": str(raw_strike), "strike_scale": None,
                   "expiry": expiry, "underlying_stock_id": result["underlying_stock_id"],
                   "combo_index": result["combo_count"] - 1, "combo_group_field": group,
                   "combo_raw_integer_fields": {str(n): str(signed(v)) for n, v in combo_scalars.items()},
                   "leg_raw_integer_fields": {str(n): str(signed(v)) for n, v in leg.items()},
                   "identity_status": "server_id_only", "source": "desktop_session_protocol6736"}
            if stock_id in metadata_by_id:
                row = _enrich(row, metadata_by_id[stock_id], option_market)
            result["contracts"].append(row)
    result["count"] = len(result["contracts"])
    result["metadata_count"] = sum(row["identity_status"] == "native_metadata_verified" for row in result["contracts"])
    return result
