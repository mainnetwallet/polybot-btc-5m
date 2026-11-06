"""Risk-first position sizing. Minimums never justify increasing risk."""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from .exchange.fees import FeeSchedule, taker_fee
from .types import OrderBookSnapshot, ReasonCode, Side, floor_to_step

SHARE_STEP = Decimal("0.01")
CAPITAL_PRICE_GRID = (Decimal("0.40"), Decimal("0.45"), Decimal("0.50"), Decimal("0.55"), Decimal("0.60"), Decimal("0.65"), Decimal("0.70"))


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
    capital_required: Decimal = Decimal(0)
    risk_budget: Decimal = Decimal(0)
    max_by_risk: Decimal = Decimal(0)
    max_by_balance: Decimal = Decimal(0)
    max_by_depth: Decimal = Decimal(0)
    min_order_cost: Decimal = Decimal(0)


@dataclass
class CompatibilityRow:
    price: Decimal
    min_order_shares: Decimal
    order_cost: Decimal
    entry_fee: Decimal
    buffer: Decimal
    worst_case_loss: Decimal
    min_capital: Decimal


@dataclass
class CompatibilityReport:
    rows: list[CompatibilityRow]
    minimum_capital: Decimal
    risk_pct: Decimal
    minimum_order_size: Decimal
    notes: list[str] = field(default_factory=list)


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
    risk_budget: Decimal,
) -> SizingResult:
    """Buy a fixed dollar target without exceeding the universal risk or venue limits.

    The target is never rounded UP to satisfy the venue's minimum share quantity. Worst-case cost
    (including fees and buffer) must fit both the 2%-of-capital risk budget and available balance.
    A target below the venue minimum is refused, not upsized.
    """
    best = book.best_ask
    assert best is not None
    floor_shares = min_order_size

    def _info(**kw: Decimal) -> dict[str, Decimal]:
        return dict(risk_budget=risk_budget, **kw)

    # Never round the requested amount up to meet the venue's minimum share quantity.
    shares = ((target / best) / share_step).to_integral_value(rounding="ROUND_CEILING") * share_step
    if shares < floor_shares:
        return SizingResult(
            False,
            ReasonCode.BELOW_MIN_ORDER,
            f"fixed trade ${target} buys {shares} shares, below the {floor_shares}-share venue minimum",
            **_info(min_order_cost=floor_shares * best),
        )
    filled, notional = _walk_limited(book, shares, max_price)
    if filled < shares:
        return SizingResult(
            False,
            ReasonCode.INSUFFICIENT_DEPTH,
            f"book can only fill {filled} of {shares} shares needed for the fixed ${target} trade",
            **_info(max_by_depth=floor_to_step(filled, share_step)),
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
            **_info(min_order_cost=min_notional_usd),
        )
    if min_notional_usd is None and shares < min_order_size:
        return SizingResult(
            False,
            ReasonCode.BELOW_MIN_ORDER,
            f"fixed trade ${target} buys {shares} shares, below the {min_order_size}-share minimum",
            **_info(),
        )

    avg = notional / shares
    fee = taker_fee(shares, avg, schedule)
    cost = notional + fee + fee_buffer * shares
    if cost > risk_budget:
        return SizingResult(
            False,
            ReasonCode.RISK_LIMIT,
            f"fixed trade worst-case cost ${cost:.4f} exceeds the 2% risk budget ${risk_budget:.4f}",
            **_info(capital_required=cost),
        )
    if cost > available:
        return SizingResult(
            False,
            ReasonCode.INSUFFICIENT_BALANCE,
            f"fixed trade costs ${cost:.4f} but only ${available:.4f} is available",
            **_info(capital_required=cost),
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
        capital_required=cost,
        risk_budget=risk_budget,
        max_by_risk=shares,
        max_by_balance=floor_to_step(available / (best + fee / shares + fee_buffer), share_step),
        max_by_depth=floor_to_step(sum((l.size for l in book.asks if max_price is None or l.price <= max_price), Decimal(0)), share_step),
        min_order_cost=min_notional_usd if min_notional_usd is not None else min_order_size * (best + fee_buffer),
    )


def size_position(
    *,
    capital: Decimal | None,
    risk_pct: Decimal,
    balance: Decimal | None,
    reserved: Decimal = Decimal(0),
    book: OrderBookSnapshot | None,
    min_order_size: Decimal,
    schedule: FeeSchedule,
    fee_buffer: Decimal = Decimal("0.002"),
    max_price: Decimal | None = None,
    share_step: Decimal = SHARE_STEP,
    extra_risk_cap: Decimal | None = None,
    min_notional_usd: Decimal | None = None,
    fixed_notional_usd: Decimal | None = None,
) -> SizingResult:
    """Size a BUY inside the risk budget.

    ``fixed_notional_usd`` sets the target amount, but the same risk-percent budget and venue
    minimums still apply to it.

    ``min_notional_usd`` adds a collateral-notional floor on top of the venue share minimum.
    Neither minimum is rounded up to; if either cannot fit inside the risk budget, the order is
    refused with BELOW_MIN_ORDER.
    """
    if capital is None or capital <= 0:
        return SizingResult(False, ReasonCode.CAPITAL_UNSET, "allocated capital is not configured")
    if balance is None:
        return SizingResult(False, ReasonCode.INSUFFICIENT_BALANCE, "balance unavailable")
    if book is None or book.best_ask is None:
        return SizingResult(False, ReasonCode.INSUFFICIENT_DEPTH, "no ask liquidity")

    risk_budget = capital * risk_pct
    if extra_risk_cap is not None:
        risk_budget = min(risk_budget, extra_risk_cap)
    available = max(Decimal(0), balance - max(Decimal(0), reserved))
    if available <= 0:
        return SizingResult(False, ReasonCode.INSUFFICIENT_BALANCE, "no available balance", risk_budget=risk_budget)
    if fixed_notional_usd is not None:
        return _size_fixed(
            target=fixed_notional_usd,
            available=available,
            book=book,
            min_order_size=min_order_size,
            schedule=schedule,
            fee_buffer=fee_buffer,
            max_price=max_price,
            share_step=share_step,
            min_notional_usd=min_notional_usd,
            risk_budget=risk_budget,
        )
    max_cost = min(risk_budget, available)

    # Conservative per-share cost at the best ask (fees scale with price; re-evaluated below).
    best = book.best_ask
    fee_ps_best = taker_fee(Decimal(1), best, schedule)
    unit_cost = best + fee_ps_best + fee_buffer
    if unit_cost <= 0:
        return SizingResult(False, ReasonCode.RISK_LIMIT, "invalid unit cost", risk_budget=risk_budget)

    max_by_risk = floor_to_step(max_cost / unit_cost, share_step)
    max_by_balance = floor_to_step(available / unit_cost, share_step)
    depth_levels = [l for l in book.asks if (max_price is None or l.price <= max_price)]
    depth_shares = sum((l.size for l in depth_levels), Decimal(0))
    max_by_depth = floor_to_step(depth_shares, share_step)

    # Dollar minimum is an additional constraint; it never replaces the venue share minimum.
    floor_shares = min_order_size

    shares = min(max_by_risk, max_by_balance, max_by_depth)
    if shares < floor_shares:
        return SizingResult(
            False,
            ReasonCode.BELOW_MIN_ORDER,
            f"risk-valid size {shares} below market minimum {floor_shares}; risk is never raised to satisfy minimums",
            risk_budget=risk_budget,
            max_by_risk=max_by_risk,
            max_by_balance=max_by_balance,
            max_by_depth=max_by_depth,
            capital_required=max_cost,
        )

    # Walk the actual book and shrink until cost fits. Round DOWN only.
    for _ in range(200):
        shares = floor_to_step(shares, share_step)
        if shares < floor_shares:
            break
        filled, notional = _walk_limited(book, shares, max_price)
        shares = min(shares, floor_to_step(filled, share_step))
        if shares < floor_shares:
            break
        filled, notional = _walk_limited(book, shares, max_price)
        if filled < shares:
            shares = floor_to_step(filled, share_step)
            continue
        avg = notional / shares if shares > 0 else Decimal(0)
        fee = taker_fee(shares, avg, schedule)
        cost = notional + fee + fee_buffer * shares
        if cost <= max_cost:
            if min_notional_usd is not None and notional < min_notional_usd:
                return SizingResult(
                    False,
                    ReasonCode.BELOW_MIN_ORDER,
                    f"order ${notional:.4f} below ${min_notional_usd} minimum; "
                    f"risk budget ${risk_budget:.4f} is never raised to satisfy minimums",
                    risk_budget=risk_budget,
                    max_by_risk=max_by_risk,
                    max_by_balance=max_by_balance,
                    max_by_depth=max_by_depth,
                    capital_required=max_cost,
                    min_order_cost=min_notional_usd,
                )
            worst = cost
            return SizingResult(
                True,
                ReasonCode.OK,
                "sized within risk budget",
                shares=shares,
                notional=notional,
                avg_price=avg,
                fee=fee,
                worst_case_loss=worst,
                capital_required=worst,
                risk_budget=risk_budget,
                max_by_risk=max_by_risk,
                max_by_balance=max_by_balance,
                max_by_depth=max_by_depth,
                min_order_cost=min_order_size * unit_cost,
            )
        excess = cost - max_cost
        per_share = avg + fee / shares + fee_buffer
        reduce = floor_to_step(excess / per_share, share_step)
        if reduce <= 0:
            reduce = share_step
        shares -= reduce

    min_cost = (
        min_notional_usd
        if min_notional_usd is not None
        else min_order_size * (book.best_ask + taker_fee(Decimal(1), book.best_ask, schedule) + fee_buffer)
    )
    return SizingResult(
        False,
        ReasonCode.BELOW_MIN_ORDER,
        f"minimum valid order cost {min_cost:.4f} exceeds risk budget {risk_budget:.4f}",
        risk_budget=risk_budget,
        max_by_risk=max_by_risk,
        max_by_balance=max_by_balance,
        max_by_depth=max_by_depth,
        min_order_cost=min_cost,
    )


def capital_compatibility(
    *,
    capital: Decimal | None,
    risk_pct: Decimal,
    min_order_size: Decimal,
    schedule: FeeSchedule,
    fee_buffer: Decimal = Decimal("0.002"),
    prices: tuple[Decimal, ...] = CAPITAL_PRICE_GRID,
) -> CompatibilityReport:
    rows: list[CompatibilityRow] = []
    notes: list[str] = []
    for price in prices:
        shares = min_order_size
        notional = shares * price
        fee = taker_fee(shares, price, schedule)
        buffer = shares * fee_buffer
        worst = notional + fee + buffer
        min_capital = worst / risk_pct if risk_pct > 0 else Decimal(0)
        rows.append(CompatibilityRow(price, shares, notional, fee, buffer, worst, min_capital))
    minimum_capital = max((r.min_capital for r in rows), default=Decimal(0))
    if capital is None:
        notes.append("Allocated capital is not configured; verdict withheld.")
    elif capital < minimum_capital:
        notes.append(f"Capital {capital:.2f} is below {minimum_capital:.2f}; minimum-sized orders would breach the 2% risk cap.")
    else:
        notes.append(f"Capital {capital:.2f} supports at least one minimum-sized order under the configured risk cap.")
    notes.append("Never round a position up to meet an exchange minimum.")
    return CompatibilityReport(rows, minimum_capital, risk_pct, min_order_size, notes)


ADVICE_REFERENCE_PRICE = Decimal("0.60")


def capital_advice(
    *,
    capital: Decimal | None,
    risk_pct: Decimal,
    trade_size_usd: Decimal | None,
    min_order_size: Decimal = Decimal("5"),
    fee_buffer: Decimal = Decimal("0.002"),
    reference_price: Decimal = ADVICE_REFERENCE_PRICE,
) -> dict[str, str | bool | None]:
    """Plain-language check that Capital, Trade size and the 2% risk cap can ever produce an order.

    The venue's share minimum (default 5 shares) means one order costs roughly
    ``min_order_size * price``. The risk cap (``capital * risk_pct``) must cover that, and the
    risk cap is never raised to fit a minimum. Without this check a too-small capital just shows
    up as endless NO_TRADE / BELOW_MIN_ORDER rows. Fees are not included, so the figures are
    approximate (fees only raise the minimum slightly).
    """
    min_notional = min_order_size * reference_price
    min_order = min_order_size * (reference_price + fee_buffer)
    min_capital = (min_order / risk_pct).to_integral_value(rounding="ROUND_CEILING") if risk_pct > 0 else None
    max_trade = capital * risk_pct if capital is not None and capital > 0 else None
    pct = f"{(risk_pct * 100).normalize():f}%"

    message: str | None = None
    if capital is None or capital <= 0:
        message = "Set Capital first: the bot does not trade without it."
    elif max_trade is not None and max_trade < min_order:
        message = (
            f"No trade is possible yet. Capital ${capital:f} with the {pct} risk cap allows at most "
            f"${max_trade:.2f} per trade, but the exchange minimum ({min_order_size:f} shares) is about "
            f"${min_order:.2f}. Raise Capital to about ${min_capital:.0f} or more."
        )
    elif trade_size_usd is not None and trade_size_usd < min_notional:
        message = (
            f"Trade size ${trade_size_usd:f} buys fewer than the exchange minimum of {min_order_size:f} shares "
            f"unless the share price is {trade_size_usd / min_order_size:.2f} or lower, so most orders will be "
            f"refused. About ${min_notional:.2f} fits a typical {reference_price:f} price."
        )
    elif trade_size_usd is not None and max_trade is not None and trade_size_usd > max_trade:
        message = (
            f"Trade size ${trade_size_usd:f} is above the {pct} risk cap (${max_trade:.2f} at this "
            f"Capital), so every order is refused. Lower it or raise Capital."
        )
    return {
        "ok": message is None,
        "message": message,
        "min_order_usd": f"{min_order:.2f}",
        "min_capital_usd": f"{min_capital:.0f}" if min_capital is not None else None,
        "max_trade_usd": f"{max_trade:.2f}" if max_trade is not None else None,
    }
