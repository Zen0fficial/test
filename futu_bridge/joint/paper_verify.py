"""Independent Decimal audit of native fills against immutable source minute opens.

Price rounding, charges, schedule and cash arithmetic are intentionally written
independently of the adapters. A completed process or matching final balances
alone is insufficient: every fill must agree, in order and at the correct time.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP

from .qlib_rl_data import ExecutionData


def verify(data: ExecutionData, result: dict[str, object], *,
           expected_commands: list[dict[str, object]] | None = None) -> dict[str, object]:
    """Recompute the supported funded execution contract without adapter helpers."""
    if (result.get("real_orders_enabled") is not False or result.get("failures")
            or result.get("unconsumed_commands", 0)
            or (result["schedule"] not in ("split", "immediate") and expected_commands is None)
            or result["bps"] not in (0, 5, 10)):
        raise ValueError("Incomplete or unsupported native simulation")
    times = [("10:00:00", "buy", 100), ("13:00:00", "buy", 100), ("15:30:00", "sell", 200)]
    if result["schedule"] == "immediate":
        times = [("09:30:00", "buy", 200), ("15:30:00", "sell", 200)]
    expected = [(day + " " + time, side, qty) for day in result["days"] for time, side, qty in times]
    if expected_commands is not None:
        expected = [(c["time"], c["side"], int(c["shares"])) for c in expected_commands if c["shares"] > 0]
        if any(c["shares"] < 0 or c["shares"] % 100 for c in expected_commands):
            raise ValueError("Policy commands must be nonnegative whole HK lots")
    fills = result["fills"]
    if len(fills) != len(expected) or result.get("order_count", len(fills)) != len(fills):
        raise ValueError("Missing, duplicate or extra native orders/fills")
    cash, holding, total_fees = Decimal(100000), 0, Decimal(0)
    for fill, (time, side, shares) in zip(fills, expected):
        if fill["time"] != time or fill["side"] != side or Decimal(str(fill["shares"])) != shares:
            raise ValueError("Native fill chronology, side or quantity mismatch")
        raw = Decimal(str(data.minutes.loc[time, "open"]))
        multiplier = 1 + Decimal(result["bps"]) / 10000 * (1 if side == "buy" else -1)
        units = raw * multiplier / Decimal("0.2")
        price = units.to_integral_value(rounding=ROUND_CEILING if side == "buy" else ROUND_FLOOR) * Decimal(".2")
        notional = price * shares
        commission = max(Decimal(18), notional / 2000)
        charges = notional * Decimal(".000127")
        stamp = (notional / 1000).to_integral_value(rounding=ROUND_CEILING)
        fee = (commission + charges + stamp).quantize(Decimal(".01"), rounding=ROUND_HALF_UP)
        for name, value in (("price", price), ("notional", notional), ("fee", fee)):
            if abs(Decimal(str(fill[name])) - value) > Decimal("0.000001"):
                raise ValueError(f"Native {name} disagrees with independent source calculation")
        cash += (-notional if side == "buy" else notional) - fee
        holding += shares if side == "buy" else -shares
        total_fees += fee
        if cash < 0 or holding < 0:
            raise ValueError("Native simulated account exceeded its cash or inventory")
        if "cash_after" in fill and abs(Decimal(str(fill["cash_after"])) - cash) > Decimal(".000001"):
            raise ValueError("Native intermediate cash mismatch")
        if "holding_after" in fill and Decimal(str(fill["holding_after"])) != holding:
            raise ValueError("Native intermediate inventory mismatch")
    if abs(Decimal(str(result["ending_cash"])) - cash) > Decimal(".000001") or result["ending_holding"] != holding:
        raise ValueError("Native final account mismatch")
    return {"passed": True, "fills_verified": len(fills), "ending_cash": str(cash),
            "ending_holding": holding, "total_fees": str(total_fees)}
