"""Fixed-dollar position sizing. Venue minimums never justify buying more than the trade size."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .exchange.fees import FeeSchedule, taker_fee
from .types import OrderBookSnapshot, ReasonCode, floor_to_step

SHARE_STEP = Decimal("0.01")


@dataclass
class SizingResult:
    allowed: bool
    reason: ReasonCode
    detail: str
    shares: Decimal = Decimal(0)
    notional: Decimal = Decimal(0)
    avg_price: Decimal = Decimal(0)
    fee: Decimal = Decimal(0)
    worst_case_loss: Decimal = Decimal(0)
    order_cost: Decimal = Decimal(0)
    max_by_balance: Decimal = Decimal(0)
    max_by_depth: Decimal = Decimal(0)
    min_order_cost: Decimal = Decimal(0)


def _walk_limited(book: OrderBookSnapshot, shares: Decimal, max_price: Decimal | None) -> tuple[Decimal, Decimal]:
    remaining = shares
    filled = Decimal(0)
    notional = Decimal(0)
    for level in book.asks:
        if remaining <= 0:
            break
        if max_price is not None and level.price > max_price:
            continue
        if level.size <= 0:
            continue
        take = min(level.size, remaining)
        filled += take
        notional += take * level.price
        remaining -= take
    return filled, notional


def _size_fixed(
    *,
    target: Decimal,
    available: Decimal,
    book: OrderBookSnapshot,
    min_order_size: Decimal,
    schedule: FeeSchedule,
    fee_buffer: Decimal,
    max_price: Decimal | None,
    share_step: Decimal,
    min_notional_usd: Decimal | None,
) -> SizingResult:
    """Buy a fixed dollar target without breaching the venue limits or the available balance.

    The target is never rounded UP to satisfy the venue's minimum share quantity: a target below
    the venue minimum is refused, not upsized.
    """
    best = book.best_ask
    assert best is not None
    floor_shares = min_order_size

    # Never round the requested amount up to meet the venue's minimum share quantity.
    shares = ((target / best) / share_step).to_integral_value(rounding="ROUND_CEILING") * share_step
    if shares < floor_shares:
        return SizingResult(
            False,
            ReasonCode.BELOW_MIN_ORDER,
            f"fixed trade ${target} buys {shares} shares, below the {floor_shares}-share venue minimum",
            min_order_cost=floor_shares * best,
        )
    filled, notional = _walk_limited(book, shares, max_price)
    if filled < shares:
        return SizingResult(
            False,
            ReasonCode.INSUFFICIENT_DEPTH,
            f"book can only fill {filled} of {shares} shares needed for the fixed ${target} trade",
            max_by_depth=floor_to_step(filled, share_step),
        )
    for _ in range(200):
        smaller = shares - share_step
        if smaller < floor_shares:
            break
        f2, n2 = _walk_limited(book, smaller, max_price)
        if f2 < smaller or n2 < target:
            break
        shares, notional = smaller, n2

    if min_notional_usd is not None and notional < min_notional_usd:
        return SizingResult(
            False,
            ReasonCode.BELOW_MIN_ORDER,
            f"fixed trade ${target} is below the ${min_notional_usd} minimum order; trade_size_usd must be >= ${min_notional_usd}",
            min_order_cost=min_notional_usd,
        )
    if min_notional_usd is None and shares < min_order_size:
        return SizingResult(
            False,
            ReasonCode.BELOW_MIN_ORDER,
            f"fixed trade ${target} buys {shares} shares, below the {min_order_size}-share minimum",
        )

    avg = notional / shares
    fee = taker_fee(shares, avg, schedule)
    cost = notional + fee + fee_buffer * shares
    if cost > available:
        return SizingResult(
            False,
            ReasonCode.INSUFFICIENT_BALANCE,
            f"fixed trade costs ${cost:.4f} but only ${available:.4f} is available",
            order_cost=cost,
        )
    return SizingResult(
        True,
        ReasonCode.OK,
        f"fixed ${target} trade",
        shares=shares,
        notional=notional,
        avg_price=avg,
        fee=fee,
        worst_case_loss=cost,
        order_cost=cost,
        max_by_balance=floor_to_step(available / (best + fee / shares + fee_buffer), share_step),
        max_by_depth=floor_to_step(sum((l.size for l in book.asks if max_price is None or l.price <= max_price), Decimal(0)), share_step),
        min_order_cost=min_notional_usd if min_notional_usd is not None else min_order_size * (best + fee_buffer),
    )


def size_position(
    *,
    trade_size_usd: Decimal,
    balance: Decimal | None,
    reserved: Decimal = Decimal(0),
    book: OrderBookSnapshot | None,
    min_order_size: Decimal,
    schedule: FeeSchedule,
    fee_buffer: Decimal = Decimal("0.002"),
    max_price: Decimal | None = None,
    share_step: Decimal = SHARE_STEP,
    min_notional_usd: Decimal | None = None,
) -> SizingResult:
    """Size a BUY of ``trade_size_usd`` dollars.

    The venue's share minimum and the optional ``min_notional_usd`` floor still apply. Neither is
    rounded up to; if the trade size cannot satisfy them the order is refused with BELOW_MIN_ORDER.
    """
    if balance is None:
        return SizingResult(False, ReasonCode.INSUFFICIENT_BALANCE, "balance unavailable")
    if book is None or book.best_ask is None:
        return SizingResult(False, ReasonCode.INSUFFICIENT_DEPTH, "no ask liquidity")

    available = max(Decimal(0), balance - max(Decimal(0), reserved))
    if available <= 0:
        return SizingResult(False, ReasonCode.INSUFFICIENT_BALANCE, "no available balance")
    return _size_fixed(
        target=trade_size_usd,
        available=available,
        book=book,
        min_order_size=min_order_size,
        schedule=schedule,
        fee_buffer=fee_buffer,
        max_price=max_price,
        share_step=share_step,
        min_notional_usd=min_notional_usd,
    )


ADVICE_REFERENCE_PRICE = Decimal("0.60")


def trade_size_advice(
    *,
    trade_size_usd: Decimal,
    min_order_size: Decimal = Decimal("5"),
    fee_buffer: Decimal = Decimal("0.002"),
    reference_price: Decimal = ADVICE_REFERENCE_PRICE,
) -> dict[str, str | bool | None]:
    """Plain-language check that the Trade size can ever produce an order.

    The venue's share minimum (default 5 shares) means one order costs roughly
    ``min_order_size * price``. A trade size below that is refused, never upsized. Without this
    check a too-small trade size just shows up as endless NO_TRADE / BELOW_MIN_ORDER rows.
    """
    min_notional = min_order_size * reference_price
    min_order = min_order_size * (reference_price + fee_buffer)

    message: str | None = None
    if trade_size_usd < min_notional:
        message = (
            f"Trade size ${trade_size_usd:f} buys fewer than the exchange minimum of {min_order_size:f} shares "
            f"unless the share price is {trade_size_usd / min_order_size:.2f} or lower, so most orders will be "
            f"refused. About ${min_notional:.2f} fits a typical {reference_price:f} price."
        )
    return {
        "ok": message is None,
        "message": message,
        "min_order_usd": f"{min_order:.2f}",
    }
