"""SELL feasibility and DUST rules, checked before any BUY."""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from ..types import OrderBookSnapshot, Side, floor_to_step

SHARE_STEP = Decimal("0.01")


@dataclass
class ExitFeasibility:
    allowed: bool
    can_full_exit: bool
    best_bid: Decimal | None
    cumulative_bid_depth: Decimal
    valid_sell_quantity: Decimal
    expected_exit_price: Decimal | None
    slippage_estimate: Decimal | None
    dust_risk: bool
    reasons: list[str] = field(default_factory=list)


def assess_exit_feasibility(
    book: OrderBookSnapshot | None,
    shares: Decimal,
    min_sell_size: Decimal,
    *,
    require_full_exit: bool = True,
) -> ExitFeasibility:
    if book is None or book.best_bid is None:
        return ExitFeasibility(
            False, False, None, Decimal(0), Decimal(0), None, None, False,
            ["no bid liquidity; losing-side exit may be impossible"],
        )
    depth = sum((l.size for l in book.bids), Decimal(0))
    valid_qty = floor_to_step(min(shares, depth), SHARE_STEP)
    if valid_qty < min_sell_size:
        return ExitFeasibility(
            False,
            False,
            book.best_bid,
            depth,
            valid_qty,
            None,
            None,
            True,
            [f"SELL quantity {valid_qty} below market minimum {min_sell_size}"],
        )
    avg, filled = book.avg_exec_price(Side.SELL, shares)
    can_full = filled >= shares
    slip = (book.best_bid - avg) if filled > 0 else None
    reasons = []
    if can_full:
        reasons.append("full SELL route visible at entry time")
    else:
        reasons.append(f"only {filled}/{shares} shares currently exit-visible")
    allowed = can_full if require_full_exit else filled > 0
    if not allowed:
        reasons.append("entry blocked: full exit not feasible and resolution-only exit not approved")
    return ExitFeasibility(
        allowed,
        can_full,
        book.best_bid,
        depth,
        valid_qty,
        avg if filled > 0 else None,
        slip,
        valid_qty < min_sell_size,
        reasons,
    )


def assess_dust(shares: Decimal, min_sell_size: Decimal) -> tuple[bool, str]:
    rounded = floor_to_step(shares, SHARE_STEP)
    if rounded <= 0:
        return True, "zero residual"
    if rounded < min_sell_size:
        return True, f"residual {rounded} below SELL minimum {min_sell_size}"
    return False, "valid SELL quantity"
