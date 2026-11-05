"""Fee math. Rates always come from live market metadata; nothing is hardcoded here."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

FEE_QUANT = Decimal("0.00001")


@dataclass(frozen=True)
class FeeSchedule:
    rate: Decimal = Decimal(0)
    exponent: int = 1
    taker_only: bool = True
    rebate_rate: Decimal = Decimal(0)
    enabled: bool = False

    @classmethod
    def from_market(cls, market) -> "FeeSchedule":  # type: ignore[no-untyped-def]
        return cls(
            rate=Decimal(str(market.fee_rate)),
            exponent=int(market.fee_exponent),
            taker_only=bool(market.taker_only),
            rebate_rate=Decimal(str(market.rebate_rate)),
            enabled=bool(market.fees_enabled),
        )


def taker_fee(shares: Decimal, price: Decimal, schedule: FeeSchedule) -> Decimal:
    """Polymarket crypto fee formula: shares * rate * (p*(1-p))^exponent.

    Rounded to 5 decimals, matching the published fee schedule. Returns 0 when fees
    are disabled. This is deliberately applied to both entry and taker exits; holding
    to resolution incurs no exit fee.
    """
    if not schedule.enabled or schedule.rate <= 0 or shares <= 0:
        return Decimal("0.00000")
    p = min(max(price, Decimal(0)), Decimal(1))
    raw = shares * schedule.rate * ((p * (Decimal(1) - p)) ** schedule.exponent)
    return raw.quantize(FEE_QUANT, rounding=ROUND_HALF_UP)


def maker_fee(shares: Decimal, price: Decimal, schedule: FeeSchedule) -> Decimal:
    return Decimal("0.00000") if schedule.taker_only else taker_fee(shares, price, schedule)


def fee_per_share(shares: Decimal, price: Decimal, schedule: FeeSchedule) -> Decimal:
    if shares <= 0:
        return Decimal(0)
    return taker_fee(shares, price, schedule) / shares
