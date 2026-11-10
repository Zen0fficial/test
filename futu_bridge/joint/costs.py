"""IBKR Pro SEHK Tier-I costs, verified against official schedules 2026-11-09."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal

SOURCES = (
    "https://www.interactivebrokers.com/en/pricing/commissions-stocks.php",
    "https://www.interactivebrokers.com/en/accounts/fees/SEHK-StkFees.php",
    "https://www.interactivebrokers.com.hk/en/index.php?f=1316",
)


@dataclass(frozen=True, slots=True)
class FeeBreakdown:
    """One filled order, denominated in HKD; buy and sell use the same rates."""

    commission: Decimal
    exchange: Decimal
    clearing: Decimal
    sfc: Decimal
    frc: Decimal
    stamp: Decimal

    @property
    def total(self) -> Decimal:
        """Round the summed fee to cents, preserving component precision first."""
        return sum(asdict(self).values(), Decimal(0)).quantize(Decimal(".01"), rounding=ROUND_HALF_UP)

    def json(self) -> dict[str, str]:
        """Serialize exact components and the debit without binary rounding."""
        return {**{name: str(value) for name, value in asdict(self).items()}, "total": str(self.total)}


def tiered_fee(notional: Decimal) -> FeeBreakdown:
    """Charge Tier I for ordinary SEHK stocks, not warrants/Stock Connect.

    Uses the current schedule for every simulated date: a current-cost scenario,
    not reconstruction of historical tariffs. Monthly turnover must remain at
    or below HKD 15m; the simulator explicitly checks that limit.
    """
    if not isinstance(notional, Decimal) or not notional.is_finite() or notional <= 0:
        raise ValueError("Order notional must be a positive finite Decimal")
    return FeeBreakdown(
        max(Decimal(18), notional * Decimal(".0005")),
        notional * Decimal(".0000565"), notional * Decimal(".000042"),
        notional * Decimal(".000027"), notional * Decimal(".0000015"),
        (notional * Decimal(".001")).quantize(Decimal(1), rounding=ROUND_CEILING),
    )
