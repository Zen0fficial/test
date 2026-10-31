"""Snapshot features for Futu option chains and verified option quotes.

The current Futu chain route is a current-chain snapshot, not a historical
option-universe archive. Features emitted here therefore carry ``pit_safe=0``
unless the caller supplies an explicit availability timestamp and historical
chain provenance.
"""

from __future__ import annotations

import datetime as dt
import math
from typing import Any, Mapping, Optional


class OptionFeatureError(ValueError):
    """Raised when an option snapshot lacks enough identity to aggregate."""


def _parts(response: Any) -> tuple[Mapping[str, Any], Mapping[str, Any], list[str]]:
    data = getattr(response, "data", None)
    if isinstance(data, Mapping):
        return data, dict(getattr(response, "source", {}) or {}), list(getattr(response, "warnings", []) or [])
    if isinstance(response, Mapping):
        nested = response.get("data")
        if isinstance(nested, Mapping):
            return nested, dict(response.get("source", {}) or {}), list(response.get("warnings", []) or [])
        return response, {}, []
    raise OptionFeatureError("Expected a MarketDataResponse or mapping.")


def _finite(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _days_to_expiry(expiry: Any, observed_at: Any) -> Optional[float]:
    if not isinstance(expiry, str) or not isinstance(observed_at, str):
        return None
    try:
        expiry_date = dt.date.fromisoformat(expiry[:10])
        observed = dt.datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=dt.timezone.utc)
        return (expiry_date - observed.astimezone(dt.timezone.utc).date()).days
    except (TypeError, ValueError):
        return None


def option_snapshot_features(underlying: str, chain_response: Any, quote_response: Any = None,
                             *, available_at: Optional[str] = None) -> dict[str, Any]:
    """Aggregate one verified Futu chain snapshot into numeric features."""
    chain, source, warnings = _parts(chain_response)
    contracts = chain.get("contracts")
    if not isinstance(underlying, str) or not underlying.strip() or not isinstance(contracts, list):
        raise OptionFeatureError("Option chain requires an underlying symbol and contracts list.")
    quotes_data = {}
    quote_source = {}
    if quote_response is not None:
        quotes_data, quote_source, _ = _parts(quote_response)
    quote_rows = quotes_data.get("quotes", []) if isinstance(quotes_data, Mapping) else []
    if not isinstance(quote_rows, list):
        quote_rows = []
    last_by_symbol = {row.get("symbol"): _finite(row.get("last")) for row in quote_rows if isinstance(row, Mapping)}

    labeled = [row for row in contracts if isinstance(row, Mapping) and row.get("symbol") and row.get("side") in ("call", "put")]
    calls = [row for row in labeled if row.get("side") == "call"]
    puts = [row for row in labeled if row.get("side") == "put"]
    strikes = [_finite(row.get("strike")) for row in labeled]
    strikes = [value for value in strikes if value is not None]
    expiries = sorted({row.get("expiry") for row in labeled if isinstance(row.get("expiry"), str)})
    last_calls = [last_by_symbol.get(row.get("symbol")) for row in calls if last_by_symbol.get(row.get("symbol")) is not None]
    last_puts = [last_by_symbol.get(row.get("symbol")) for row in puts if last_by_symbol.get(row.get("symbol")) is not None]
    observed_at = available_at or source.get("observed_at")
    days = [_days_to_expiry(expiry, observed_at) for expiry in expiries]
    days = [value for value in days if value is not None]
    return {
        "symbol": underlying,
        "date": observed_at[:10] if isinstance(observed_at, str) and len(observed_at) >= 10 else None,
        "option_contract_count": len(labeled),
        "option_server_id_count": len(contracts),
        "option_call_count": len(calls),
        "option_put_count": len(puts),
        "option_expiry_count": len(expiries),
        "option_min_strike": min(strikes) if strikes else None,
        "option_max_strike": max(strikes) if strikes else None,
        "option_min_days_to_expiry": min(days) if days else None,
        "option_max_days_to_expiry": max(days) if days else None,
        "option_quote_count": len([value for value in last_by_symbol.values() if value is not None]),
        "option_call_last_mean": sum(last_calls) / len(last_calls) if last_calls else None,
        "option_put_last_mean": sum(last_puts) / len(last_puts) if last_puts else None,
        "option_chain_expiry": chain.get("expiry"),
        "option_default_expiry_selected": bool(chain.get("default_expiry_selected")),
        "option_metadata_count": chain.get("metadata_count"),
        "option_pit_safe": int(available_at is not None),
        "option_source_provider": source.get("provider"),
        "option_quote_source_provider": quote_source.get("provider") if quote_response is not None else None,
        "option_warnings": list(dict.fromkeys(warnings + [
            "Futu option-chain data is a current-chain snapshot; historical option-universe completeness is not established.",
            "Only contracts with verified native symbols and call/put labels are included in labeled counts.",
        ])),
    }
