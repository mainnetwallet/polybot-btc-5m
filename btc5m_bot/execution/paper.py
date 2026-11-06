"""Realistic PAPER execution: delay, queue/partial fills, slippage, fees, misses."""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from ..clock import Clock
from ..exchange.fees import FeeSchedule, taker_fee
from ..types import (
    MarketInfo,
    OrderBookSnapshot,
    OrderIntent,
    OrderResult,
    OrderStatus,
    Outcome,
    Side,
    floor_to_step,
)
from .feasibility import SHARE_STEP


@dataclass
class PaperPosition:
    market_id: str
    condition_id: str
    asset_id: str
    outcome: Outcome
    shares: Decimal
    avg_price: Decimal
    entry_fee: Decimal
    cost_basis: Decimal
    opened_at: datetime
    dust: bool = False


@dataclass
class PaperSettlement:
    market_id: str
    outcome: Outcome
    shares: Decimal
    payout: Decimal
    realized_pnl: Decimal
    won: bool


class PaperBroker:
    def __init__(
        self,
        capital: Decimal,
        clock: Clock,
        *,
        taker_delay_ms: int = 250,
        miss_probability: float = 0.08,
        partial_probability: float = 0.05,
        maker_fill_probability: float = 0.30,
        seed: int = 42,
    ) -> None:
        self.cash = capital
        self.clock = clock
        self.taker_delay_ms = taker_delay_ms
        self.miss_probability = miss_probability
        self.partial_probability = partial_probability
        self.maker_fill_probability = maker_fill_probability
        self.rng = random.Random(seed)
        self.positions: dict[str, PaperPosition] = {}
        self.fills: list[OrderResult] = []

    @property
    def balance(self) -> Decimal:
        return self.cash

    def position_for(self, market_id: str) -> PaperPosition | None:
        for pos in self.positions.values():
            if pos.market_id == market_id:
                return pos
        return None

    def equity(self, books: dict[str, OrderBookSnapshot]) -> Decimal:
        value = self.cash
        for pos in self.positions.values():
            book = books.get(pos.asset_id)
            price = book.best_bid if book and book.best_bid is not None else Decimal(0)
            value += pos.shares * price
        return value

    def _walk_limit(self, book: OrderBookSnapshot, shares: Decimal, limit: Decimal) -> tuple[Decimal, Decimal]:
        remaining = shares
        filled = Decimal(0)
        notional = Decimal(0)
        for level in book.asks:
            if remaining <= 0:
                break
            if level.price > limit:
                continue
            take = min(level.size, remaining)
            filled += take
            notional += take * level.price
            remaining -= take
        return filled, notional

    def _walk_usdc(self, book: OrderBookSnapshot, usdc: Decimal, limit: Decimal) -> tuple[Decimal, Decimal]:
        """Spend ``usdc`` on the asks (cheapest first, up to ``limit``): returns (shares, notional).

        BUYs are denominated in USDC, never in shares: shares are whatever the amount buys.
        """
        remaining = usdc
        shares = Decimal(0)
        notional = Decimal(0)
        for level in book.asks:
            if remaining <= 0:
                break
            if level.price > limit or level.size <= 0:
                continue
            level_cost = level.size * level.price
            if remaining >= level_cost:
                take, cost = level.size, level_cost
            else:
                take, cost = remaining / level.price, remaining
            shares += take
            notional += cost
            remaining -= cost
        shares = floor_to_step(shares, Decimal("0.0001"))
        return shares, notional

    async def place_order(self, intent: OrderIntent, market: MarketInfo, book: OrderBookSnapshot | None) -> OrderResult:
        await self.clock.sleep(self.taker_delay_ms / 1000.0)
        if book is None:
            return OrderResult(intent.client_order_id, OrderStatus.REJECTED, error_code="NO_BOOK", error_message="no order book")
        schedule = FeeSchedule.from_market(market)
        is_maker = intent.order_type.upper().endswith("POST_ONLY")
        if is_maker and self.rng.random() > self.maker_fill_probability:
            return OrderResult(intent.client_order_id, OrderStatus.CANCELLED, raw_status="maker_unfilled")
        if self.rng.random() < self.miss_probability:
            return OrderResult(intent.client_order_id, OrderStatus.CANCELLED, raw_status="missed_due_to_delay")

        if intent.side is Side.BUY:
            by_usdc = intent.usdc_amount is not None
            if by_usdc:
                # USDC-denominated BUY: spend exactly the amount, shares come out of the price.
                filled, notional = self._walk_usdc(book, intent.usdc_amount, intent.limit_price)
                short = notional < intent.usdc_amount - Decimal("0.01")
            else:
                filled, notional = self._walk_limit(book, intent.shares, intent.limit_price)
                short = filled < intent.shares
            if filled <= 0:
                return OrderResult(intent.client_order_id, OrderStatus.CANCELLED, raw_status="no_depth_at_limit")
            if intent.order_type.upper() == "FOK" and short:
                return OrderResult(intent.client_order_id, OrderStatus.CANCELLED, raw_status="fok_not_filled")
            # FOK is all-or-nothing on the real exchange: it can never come back partially filled.
            # (rng is still drawn first so seeded runs keep the same random sequence.)
            partial_roll = self.rng.random() < self.partial_probability
            if partial_roll and intent.order_type.upper() != "FOK" and not by_usdc:
                fraction = Decimal(str(self.rng.uniform(0.2, 0.9)))
                target = floor_to_step(filled * fraction, SHARE_STEP)
                if target > 0:
                    avg_partial, _ = book.avg_exec_price(Side.BUY, target)
                    filled = target
                    notional = avg_partial * filled
            avg = notional / filled
            fee = taker_fee(filled, avg, schedule)
            cost = notional + fee
            if cost > self.cash:
                return OrderResult(intent.client_order_id, OrderStatus.REJECTED, error_code="INSUFFICIENT_BALANCE", error_message="paper cash insufficient")
            existing = self.position_for(intent.market_id)
            if existing is not None:
                return OrderResult(intent.client_order_id, OrderStatus.REJECTED, error_code="DUPLICATE_ENTRY", error_message="position already open")
            self.cash -= cost
            dust = filled < market.min_order_size
            self.positions[intent.asset_id] = PaperPosition(
                market_id=intent.market_id,
                condition_id=intent.condition_id,
                asset_id=intent.asset_id,
                outcome=intent.outcome,
                shares=filled,
                avg_price=avg,
                entry_fee=fee,
                cost_basis=cost,
                opened_at=self.clock.now(),
                dust=dust,
            )
            status = OrderStatus.PARTIAL if short or (not by_usdc and filled < intent.shares) else OrderStatus.FILLED
            result = OrderResult(
                client_order_id=intent.client_order_id,
                status=status,
                exchange_order_id="paper-" + intent.client_order_id[:12],
                filled_shares=filled,
                avg_price=avg,
                fee_usd=fee,
                raw_status="paper_partial" if status is OrderStatus.PARTIAL else "paper_filled",
            )
            self.fills.append(result)
            return result

        # SELL
        pos = self.position_for(intent.market_id)
        if pos is None or pos.shares <= 0:
            return OrderResult(intent.client_order_id, OrderStatus.REJECTED, error_code="NO_POSITION", error_message="no position to sell")
        sellable = min(intent.shares, pos.shares)
        filled = Decimal(0)
        notional = Decimal(0)
        for level in book.bids:
            if filled >= sellable:
                break
            if level.price < intent.limit_price:
                continue
            take = min(level.size, sellable - filled)
            filled += take
            notional += take * level.price
        if filled <= 0:
            return OrderResult(intent.client_order_id, OrderStatus.CANCELLED, raw_status="no_bid_at_limit")
        if intent.order_type.upper() == "FOK" and filled < sellable:
            return OrderResult(intent.client_order_id, OrderStatus.CANCELLED, raw_status="fok_sell_not_filled")
        avg = notional / filled
        fee = taker_fee(filled, avg, schedule)
        proceeds = notional - fee
        self.cash += proceeds
        allocated_cost = pos.cost_basis * (filled / pos.shares)
        realized = proceeds - allocated_cost
        pos.shares -= filled
        pos.cost_basis -= allocated_cost
        if pos.shares <= 0:
            self.positions.pop(pos.asset_id, None)
        elif pos.shares < market.min_order_size:
            pos.dust = True
        status = OrderStatus.FILLED if filled >= sellable else OrderStatus.PARTIAL
        result = OrderResult(
            client_order_id=intent.client_order_id,
            status=status,
            exchange_order_id="paper-" + intent.client_order_id[:12],
            filled_shares=filled,
            avg_price=avg,
            fee_usd=fee,
            raw_status="paper_sell",
        )
        self.fills.append(result)
        return result

    def settle(self, market: MarketInfo, actual_outcome: str) -> list[PaperSettlement]:
        results: list[PaperSettlement] = []
        for asset_id, pos in list(self.positions.items()):
            if pos.market_id != market.market_id:
                continue
            won = pos.outcome.value == actual_outcome.upper()
            payout = pos.shares if won else Decimal(0)
            realized = payout - pos.cost_basis
            self.cash += payout
            results.append(PaperSettlement(pos.market_id, pos.outcome, pos.shares, payout, realized, won))
            self.positions.pop(asset_id, None)
        return results
