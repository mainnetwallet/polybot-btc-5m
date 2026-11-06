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
) -> SizingResult:
    """Size a BUY inside the risk budget.

    ``min_notional_usd`` switches the entry minimum from a share count to a dollar amount
    (marketable BUYs are denominated in collateral). The size is still never raised to meet it:
    if the risk budget cannot afford the dollar minimum the result is BELOW_MIN_ORDER.
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

    # Dollar-minimum mode: the share-count minimum no longer gates entry (only a positive size does).
    floor_shares = share_step if min_notional_usd is not None else min_order_size

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
