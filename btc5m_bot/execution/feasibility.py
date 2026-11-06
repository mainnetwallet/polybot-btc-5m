"""SELL feasibility, checked before any BUY.

Exits are market (FOK) orders, which the exchange accepts at any share size: the 5-share minimum
applies to LIMIT orders only. So there is no minimum SELL size and no "dust" rule here.
"""
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
    reasons: list[str] = field(default_factory=list)
    # Worst (lowest) bid price that must be hit to sell the full size. A FOK SELL needs its
    # min_price at or below this, otherwise the lower levels cannot match and the order fails.
    worst_price: Decimal | None = None


def assess_exit_feasibility(
    book: OrderBookSnapshot | None,
    shares: Decimal,
    *,
    require_full_exit: bool = True,
) -> ExitFeasibility:
    if book is None or book.best_bid is None:
        return ExitFeasibility(
            False, False, None, Decimal(0), Decimal(0), None, None,
            ["no bid liquidity; losing-side exit may be impossible"],
        )
    depth = sum((l.size for l in book.bids), Decimal(0))
    valid_qty = floor_to_step(min(shares, depth), SHARE_STEP)
    filled, notional, worst = book.walk(Side.SELL, shares)
    avg = notional / filled if filled > 0 else Decimal(0)
    can_full = filled >= shares
    slip = (book.best_bid - avg) if filled > 0 else None
    reasons = []
    if can_full:
        reasons.append("full SELL route visible at entry time")
    else:
        reasons.append(f"only {filled}/{shares} shares currently exit-visible")
    allowed = can_full if require_full_exit else filled > 0
    if not allowed:
        reasons.append("entry blocked: full exit not feasible")
    return ExitFeasibility(
        allowed,
        can_full,
        book.best_bid,
        depth,
        valid_qty,
        avg if filled > 0 else None,
        slip,
        reasons,
        worst_price=worst if filled > 0 else None,
    )
