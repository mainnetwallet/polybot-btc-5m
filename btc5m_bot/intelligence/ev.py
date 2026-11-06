"""Fee-, spread- and slippage-aware expected value for binary contracts."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from ..exchange.fees import FeeSchedule, taker_fee
from ..types import OrderBookSnapshot, ReasonCode, Side


@dataclass
class EdgeEstimate:
    outcome: str
    model_probability: float
    requested_shares: Decimal
    filled_shares: Decimal
    best_price: Decimal
    executable_price: Decimal
    slippage: Decimal
    fee_per_share: Decimal
    exit_buffer: Decimal
    cost_per_share: Decimal
    net_edge: Decimal
    ev_per_share: Decimal
    ev_total: Decimal
    roi_if_win: Decimal | None
    maker_price: Decimal | None
    maker_fee_per_share: Decimal
    maker_net_edge: Decimal | None
    sufficient_depth: bool
    failure: str = ""
    # Machine-readable cause of ``failure``: "DEPTH" (no book / no liquidity / too thin) or "SLIPPAGE".
    failure_kind: str = ""

    @property
    def positive(self) -> bool:
        return self.failure == "" and self.net_edge > 0 and self.ev_total > 0


def evaluate_edge(
    *,
    probability: float,
    book: OrderBookSnapshot | None,
    outcome: str,
    shares: Decimal,
    schedule: FeeSchedule,
    max_slippage: Decimal = Decimal("0.01"),
    fee_buffer: Decimal = Decimal("0.002"),
) -> EdgeEstimate:
    if book is None:
        return EdgeEstimate(outcome, probability, shares, Decimal(0), Decimal(0), Decimal(0), Decimal(0),
                            Decimal(0), fee_buffer, Decimal(0), Decimal(0), Decimal(0), Decimal(0), None,
                            None, Decimal(0), None, False, "no order book", "DEPTH")
    best = book.best_ask
    if best is None:
        return EdgeEstimate(outcome, probability, shares, Decimal(0), Decimal(0), Decimal(0), Decimal(0),
                            Decimal(0), fee_buffer, Decimal(0), Decimal(0), Decimal(0), Decimal(0), None,
                            None, Decimal(0), None, False, "no ask liquidity", "DEPTH")
    avg, filled = book.avg_exec_price(Side.BUY, shares)
    if filled <= 0:
        return EdgeEstimate(outcome, probability, shares, Decimal(0), best, Decimal(0), Decimal(0),
                            Decimal(0), fee_buffer, Decimal(0), Decimal(0), Decimal(0), Decimal(0), None,
                            None, Decimal(0), None, False, "no fillable depth", "DEPTH")
    slippage = avg - best
    entry_fee = taker_fee(filled, avg, schedule)
    fee_ps = entry_fee / filled if filled > 0 else Decimal(0)
    cost = avg + fee_ps + fee_buffer
    net_edge = Decimal(str(probability)) - cost
    ev_ps = Decimal(str(probability)) - cost
    ev_total = ev_ps * filled
    roi = (Decimal(1) - cost) / cost if cost > 0 else None

    maker_price = None
    maker_fee_ps = Decimal(0)
    maker_edge = None
    if book.best_bid is not None:
        maker_price = min(book.best_bid + book.tick_size, best - book.tick_size)
        if maker_price > 0:
            maker_fee_ps = taker_fee(filled, maker_price, schedule) / filled
            maker_edge = Decimal(str(probability)) - (maker_price + maker_fee_ps + fee_buffer)

    failure = ""
    failure_kind = ""
    if filled < shares:
        failure = f"insufficient depth: {filled}/{shares}"
        failure_kind = "DEPTH"
    elif slippage > max_slippage:
        failure = f"slippage {slippage} > {max_slippage}"
        failure_kind = "SLIPPAGE"

    return EdgeEstimate(
        outcome=outcome,
        model_probability=probability,
        requested_shares=shares,
        filled_shares=filled,
        best_price=best,
        executable_price=avg,
        slippage=slippage,
        fee_per_share=fee_ps,
        exit_buffer=fee_buffer,
        cost_per_share=cost,
        net_edge=net_edge,
        ev_per_share=ev_ps,
        ev_total=ev_total,
        roi_if_win=roi,
        maker_price=maker_price,
        maker_fee_per_share=maker_fee_ps,
        maker_net_edge=maker_edge,
        sufficient_depth=filled >= shares,
        failure=failure,
        failure_kind=failure_kind,
    )


def rejection_reason(edge: EdgeEstimate) -> ReasonCode:
    """Why an entry was refused on edge grounds, as an accurate reason code.

    Depth and slippage problems say nothing about EV, so they must not be reported as NEGATIVE_EV.
    NEGATIVE_EV means the trade loses money in expectation; INSUFFICIENT_EDGE means it is positive
    but below the configured minimum net edge.
    """
    if edge.failure_kind == "DEPTH":
        return ReasonCode.INSUFFICIENT_DEPTH
    if edge.failure_kind == "SLIPPAGE":
        return ReasonCode.SLIPPAGE_TOO_HIGH
    if edge.net_edge <= 0 or edge.ev_total <= 0:
        return ReasonCode.NEGATIVE_EV
    return ReasonCode.INSUFFICIENT_EDGE
