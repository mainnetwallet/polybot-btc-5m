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
        starting_cash: Decimal,
        clock: Clock,
        *,
        taker_delay_ms: int = 250,
        miss_probability: float = 0.08,
        partial_probability: float = 0.05,
        maker_fill_probability: float = 0.30,
        seed: int = 42,
    ) -> None:
        self.cash = starting_cash
        self.clock = clock
        self.taker_delay_ms = taker_delay_ms
        self.miss_probability = miss_probability
        self.partial_probability = partial_probability
        self.maker_fill_probability = maker_fill_probability
        self.rng = random.Random(seed)
        self.positions: dict[str, PaperPosition] = {}
        self.fills: list[OrderResult] = []
        self.has_history = False

    @classmethod
    def restore_from_repository(cls, starting_cash: Decimal, clock: Clock, repo, **kwargs) -> "PaperBroker":
        """Rebuild PAPER cash and open positions from the durable SQL ledger."""
        broker = cls(starting_cash, clock, **kwargs)
        trades = repo.list_trades(mode="PAPER", limit=1_000_000)
        if trades:
            broker.has_history = True
            for trade in reversed(trades):
                price = Decimal(str(trade.price))
                shares = Decimal(str(trade.shares))
                fee = Decimal(str(trade.fee))
                kind = str(trade.kind).upper()
                side = str(trade.side).upper()
                if kind == "ENTRY" or side == "BUY":
                    broker.cash -= price * shares + fee
                elif kind == "EXIT" or side == "SELL":
                    broker.cash += price * shares - fee
                elif kind == "SETTLEMENT" or side == "SETTLE":
                    broker.cash += price * shares - fee

        for row in repo.list_positions(mode="PAPER", limit=1_000_000):
            if row.status not in {"OPEN", "DUST"} or Decimal(str(row.shares)) <= 0:
                continue
            broker.positions[row.asset_id] = PaperPosition(
                market_id=row.market_id,
                condition_id=row.condition_id,
                asset_id=row.asset_id,
                outcome=Outcome(str(row.outcome).upper()),
                shares=Decimal(str(row.shares)),
                avg_price=Decimal(str(row.avg_price)),
                entry_fee=Decimal(str(row.entry_fee)),
                cost_basis=Decimal(str(row.cost_basis)),
                opened_at=row.opened_at,
            )
            broker.has_history = True
        return broker

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
            if intent.usdc_amount is None or intent.usdc_amount <= 0:
                return OrderResult(intent.client_order_id, OrderStatus.REJECTED, error_code="NO_USDC_AMOUNT", error_message="BUY requires a USDC amount")
            # USDC-denominated BUY: spend exactly the amount, shares come out of the price.
            filled, notional = self._walk_usdc(book, intent.usdc_amount, intent.limit_price)
            short = notional < intent.usdc_amount - Decimal("0.01")
            if filled <= 0:
                return OrderResult(intent.client_order_id, OrderStatus.CANCELLED, raw_status="no_depth_at_limit")
            if intent.order_type.upper() == "FOK" and short:
                return OrderResult(intent.client_order_id, OrderStatus.CANCELLED, raw_status="fok_not_filled")
            # (rng is still drawn so seeded runs keep the same random sequence.)
            self.rng.random()
            avg = notional / filled
            fee = taker_fee(filled, avg, schedule)
            cost = notional + fee
            if cost > self.cash:
                return OrderResult(intent.client_order_id, OrderStatus.REJECTED, error_code="INSUFFICIENT_BALANCE", error_message="paper cash insufficient")
            existing = self.position_for(intent.market_id)
            if existing is not None:
                return OrderResult(intent.client_order_id, OrderStatus.REJECTED, error_code="DUPLICATE_ENTRY", error_message="position already open")
            self.cash -= cost
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
            )
            status = OrderStatus.PARTIAL if short else OrderStatus.FILLED
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
            self.has_history = True
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
        self.has_history = True
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
