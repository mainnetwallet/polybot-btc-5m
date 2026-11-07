"""USDC-denominated BUY sizing.

Every BUY is a market order for a fixed amount of collateral (``trade_size_usd``). The number of
shares is whatever that amount buys at the current asks; it is only an *estimate* used for the fee,
edge and depth checks. Nothing here is sized in shares, and an unset trade size means no trade.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .exchange.fees import FeeSchedule, taker_fee
from .types import OrderBookSnapshot, ReasonCode, floor_to_step

# Resolution of the estimated share count (display / fee estimate only, never an order size).
_ESTIMATE_STEP = Decimal("0.0001")
DEFAULT_MIN_NOTIONAL_USD = Decimal("1.00")


@dataclass
class SizingResult:
    allowed: bool
    reason: ReasonCode
    detail: str
    shares: Decimal = Decimal(0)  # estimate: what ``trade_size_usd`` buys at the current asks
    notional: Decimal = Decimal(0)
    avg_price: Decimal = Decimal(0)
    fee: Decimal = Decimal(0)
    worst_case_loss: Decimal = Decimal(0)
    order_cost: Decimal = Decimal(0)
    max_by_balance: Decimal = Decimal(0)
    max_by_depth: Decimal = Decimal(0)
    min_order_cost: Decimal = Decimal(0)


def _spend(book: OrderBookSnapshot, usdc: Decimal, max_price: Decimal | None) -> tuple[Decimal, Decimal]:
    """Spend ``usdc`` on the asks, cheapest first, up to ``max_price``: returns (shares, spent)."""
    remaining = usdc
    shares = Decimal(0)
    spent = Decimal(0)
    for level in book.asks:
        if remaining <= 0:
            break
        if level.size <= 0 or (max_price is not None and level.price > max_price):
            continue
        level_cost = level.size * level.price
        if remaining >= level_cost:
            take, cost = level.size, level_cost
        else:
            take, cost = remaining / level.price, remaining
        shares += take
        spent += cost
        remaining -= cost
    return floor_to_step(shares, _ESTIMATE_STEP), spent


def size_position(
    *,
    trade_size_usd: Decimal | None,
    balance: Decimal | None,
    reserved: Decimal = Decimal(0),
    book: OrderBookSnapshot | None,
    schedule: FeeSchedule,
    fee_buffer: Decimal = Decimal("0.002"),
    max_price: Decimal | None = None,
    min_notional_usd: Decimal = DEFAULT_MIN_NOTIONAL_USD,
) -> SizingResult:
    """Size a BUY of exactly ``trade_size_usd`` USDC.

    No trade unless the amount is set. The amount is never rounded up to reach a minimum: below
    ``min_notional_usd`` the order is refused with BELOW_MIN_ORDER.
    """
    if trade_size_usd is None or trade_size_usd <= 0:
        return SizingResult(
            False,
            ReasonCode.TRADE_SIZE_NOT_SET,
            "trade size (USDC) is not set; set it in Settings to allow trades",
        )
    if balance is None:
        return SizingResult(False, ReasonCode.INSUFFICIENT_BALANCE, "balance unavailable")
    if book is None or book.best_ask is None:
        return SizingResult(False, ReasonCode.INSUFFICIENT_DEPTH, "no ask liquidity")
    available = max(Decimal(0), balance - max(Decimal(0), reserved))
    if available <= 0:
        return SizingResult(False, ReasonCode.INSUFFICIENT_BALANCE, "no available balance")
    if trade_size_usd < min_notional_usd:
        return SizingResult(
            False,
            ReasonCode.BELOW_MIN_ORDER,
            f"trade size ${trade_size_usd} is below the ${min_notional_usd} minimum order",
            min_order_cost=min_notional_usd,
        )

    shares, spent = _spend(book, trade_size_usd, max_price)
    max_by_depth = floor_to_step(
        sum((l.size for l in book.asks if max_price is None or l.price <= max_price), Decimal(0)),
        _ESTIMATE_STEP,
    )
    # A marketable FOK buy is all-or-nothing: the asks up to the price cap must absorb the full amount.
    if shares <= 0 or spent < trade_size_usd - Decimal("0.01"):
        return SizingResult(
            False,
            ReasonCode.INSUFFICIENT_DEPTH,
            f"asks within the price limit only absorb ${spent:.2f} of the ${trade_size_usd} trade",
            max_by_depth=max_by_depth,
            min_order_cost=min_notional_usd,
        )

    avg = spent / shares
    fee = taker_fee(shares, avg, schedule)
    cost = spent + fee + fee_buffer * shares
    if cost > available:
        return SizingResult(
            False,
            ReasonCode.INSUFFICIENT_BALANCE,
            f"trade costs ${cost:.4f} but only ${available:.4f} is available",
            order_cost=cost,
            min_order_cost=min_notional_usd,
        )
    return SizingResult(
        True,
        ReasonCode.OK,
        f"buy ${trade_size_usd} USDC",
        shares=shares,
        notional=spent,
        avg_price=avg,
        fee=fee,
        worst_case_loss=cost,
        order_cost=cost,
        max_by_balance=floor_to_step(available / (avg + fee / shares + fee_buffer), _ESTIMATE_STEP),
        max_by_depth=max_by_depth,
        min_order_cost=min_notional_usd,
    )


def trade_size_advice(
    *,
    trade_size_usd: Decimal | None,
    min_notional_usd: Decimal = DEFAULT_MIN_NOTIONAL_USD,
) -> dict[str, str | bool | None]:
    """Plain-language check that the trade size can ever produce an order."""
    message: str | None = None
    if trade_size_usd is None:
        message = f"Trade size is not set, so no orders will be placed. Enter a USDC amount of ${min_notional_usd:f} or more."
    elif trade_size_usd < min_notional_usd:
        message = (
            f"Trade size ${trade_size_usd:f} is below the ${min_notional_usd:f} minimum order, "
            f"so every order will be refused. Use ${min_notional_usd:f} or more."
        )
    return {"ok": message is None, "message": message, "min_order_usd": f"{min_notional_usd:.2f}"}
