"""Shared offline execution contract over captured HK.00700 minute data.

The execution reference is an observed minute open, not a reconstructed quote
or a claim of achievable fills. Adverse slippage is rounded to the HKD 0.20
tick for the supported HKD (200, 500] price band. Providers are never mutated.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path

import pandas as pd

from .qlib_rl_data import ExecutionData, load_data

TICK = Decimal(".2")
CAPITAL = Decimal(100000)
SYMBOL = "HK_00700"


def execution_price(reference: Decimal, side: str, bps: int = 5) -> Decimal:
    """Apply adverse, tick-aligned slippage within the verified price band."""
    if side not in ("buy", "sell") or bps not in (0, 5, 10) or not 200 < reference <= 500:
        raise ValueError("Unsupported paper price, side or slippage scenario")
    spread = Decimal(bps) / 10000
    value = reference * (1 + spread if side == "buy" else 1 - spread)
    result = (value / TICK).to_integral_value(rounding=ROUND_CEILING if side == "buy" else ROUND_FLOOR) * TICK
    if not 200 < result <= 500:
        raise ValueError("Slippage crosses the supported HK tick band")
    return result


def capture(response: Path) -> ExecutionData:
    """Validate real captured minutes and the supported instrument tick band."""
    data = load_data(response)
    for value in data.minutes.open:
        price = Decimal(str(value))
        if not 200 < price <= 500 or price % TICK:
            raise ValueError("Captured opens do not satisfy the supported HK tick contract")
    return data


def schedule(days: list[str], variant: str = "split") -> list[dict[str, object]]:
    """Return prescribed orders; selection never reads later market prices."""
    if variant not in ("split", "immediate"):
        raise ValueError("Unknown offline schedule")
    events = [("10:00:00", "buy", 100), ("13:00:00", "buy", 100), ("15:30:00", "sell", 200)]
    if variant == "immediate":
        events = [("09:30:00", "buy", 200), ("15:30:00", "sell", 200)]
    return [{"time": f"{day} {time}", "side": side, "shares": shares}
            for day in days for time, side, shares in events]


def utc_ns(time: str | pd.Timestamp) -> int:
    """Translate a captured Hong Kong wall-clock instant to UTC nanoseconds."""
    return int(pd.Timestamp(time).tz_localize("Asia/Hong_Kong").tz_convert("UTC").value)
