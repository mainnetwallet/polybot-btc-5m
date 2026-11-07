"""Order execution, journaling, latency measurement and position accounting."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Callable

from ..clock import Clock
from ..db.models import PositionRow
from ..db.repo import Repository
from ..exchange.base import ExchangeAdapter
from ..exchange.fees import FeeSchedule, taker_fee
from ..types import (
    Mode,
    MarketInfo,
    OrderBookSnapshot,
    OrderIntent,
    OrderResult,
    OrderStatus,
    PositionStatus,
    Side,
    as_utc,
    utcnow,
)
from .journal import OrderJournal
from .paper import PaperBroker


class LatencyMonitor:
    def __init__(self, repo: Repository) -> None:
        self.repo = repo

    def record(self, kind: str, ms: int, market_id: str | None = None) -> None:
        self.repo.add_latency(kind, max(0, int(ms)), market_id)

    def stats(self, kind: str | None = None) -> dict:
        return self.repo.latency_stats(kind)

    def p95_ok(self, kind: str, max_p95_ms: int) -> tuple[bool, dict]:
        stats = self.repo.latency_stats(kind)
        p95 = stats.get("p95")
        if p95 is None or stats.get("count", 0) < 5:
            return True, stats
        return float(p95) <= max_p95_ms, stats


@dataclass
class ExecutionOutcome:
    result: OrderResult
    position: PositionRow | None = None
    realized_pnl: Decimal = Decimal(0)
    verified: bool = False
    note: str = ""


class ExecutionEngine:
    def __init__(
        self,
        *,
        repo: Repository,
        journal: OrderJournal,
        adapter: ExchangeAdapter,
        paper: PaperBroker | None,
        clock: Clock,
        mode_provider: Callable[[], Mode],
        taker_delay_ms: int = 250,
    ) -> None:
        self.repo = repo
        self.journal = journal
        self.adapter = adapter
        self.paper = paper
        self.clock = clock
        self.mode_provider = mode_provider
        self.taker_delay_ms = taker_delay_ms
        self.latency = LatencyMonitor(repo)

    async def execute_entry(
        self,
        intent: OrderIntent,
        market: MarketInfo,
        book: OrderBookSnapshot | None,
        *,
        signal_ts: datetime | None = None,
    ) -> ExecutionOutcome:
        mode = self.mode_provider()
        schedule = FeeSchedule.from_market(market)
        if intent.usdc_amount is None or intent.usdc_amount <= 0:
            # BUYs are USDC orders. Never fall back to shares x price.
            return ExecutionOutcome(
                OrderResult(intent.client_order_id, OrderStatus.REJECTED, error_code="NO_USDC_AMOUNT", error_message="BUY requires a USDC amount"),
                note="no trade: USDC amount not set",
            )
        reserve_amount = intent.usdc_amount + taker_fee(intent.shares, intent.limit_price, schedule)
        try:
            self.journal.persist_intent(intent, reserve_amount=reserve_amount)
        except Exception as exc:
            from ..db.repo import DuplicateOrderError

            if isinstance(exc, DuplicateOrderError):
                return ExecutionOutcome(
                    OrderResult(intent.client_order_id, OrderStatus.REJECTED, error_code="DUPLICATE_ORDER", error_message=str(exc)),
                    note="duplicate order prevented",
                )
            raise

        if signal_ts is not None:
            self.latency.record("signal_to_order", int((self.clock.now() - as_utc(signal_ts)).total_seconds() * 1000), market.market_id)
        self.journal.mark_submitting(intent)
        started = self.clock.monotonic()

        if mode is Mode.PAPER:
            if self.paper is None:
                result = OrderResult(intent.client_order_id, OrderStatus.REJECTED, error_code="PAPER_UNAVAILABLE", error_message="paper broker not initialised")
            else:
                result = await self.paper.place_order(intent, market, book)
        else:
            result = await self.adapter.place_order(intent, market)

        total_ms = int((self.clock.monotonic() - started) * 1000)
        result.latency_ms = max(result.latency_ms, total_ms)
        self.latency.record("order_to_result", total_ms, market.market_id)
        self.journal.mark_result(intent, result)

        position: PositionRow | None = None
        if result.filled_shares > 0:
            existing = self.repo.get_position(market.market_id, mode.value)
            if existing is None:
                position = self.repo.create_position(
                    market_id=market.market_id,
                    condition_id=market.condition_id,
                    asset_id=intent.asset_id,
                    outcome=intent.outcome.value,
                    shares=result.filled_shares,
                    avg_price=result.avg_price,
                    entry_fee=result.fee_usd,
                    mode=mode.value,
                )
                self.repo.add_trade(
                    market_id=market.market_id,
                    client_order_id=intent.client_order_id,
                    side=intent.side.value,
                    outcome=intent.outcome.value,
                    price=result.avg_price,
                    shares=result.filled_shares,
                    fee=result.fee_usd,
                    kind="ENTRY",
                    mode=mode.value,
                )
                position = self.repo.get_position(market.market_id, mode.value)
            else:
                position = existing
            if mode is Mode.PAPER and result.status is OrderStatus.PARTIAL:
                # Paper partial is terminal for this attempt; never top up.
                self.repo.release_reservation(intent.client_order_id, status="RELEASED")

        if result.status in {OrderStatus.UNKNOWN, OrderStatus.PENDING, OrderStatus.ACKED}:
            self.repo.set_state(
                "recovery_required",
                {
                    "client_order_id": intent.client_order_id,
                    "market_id": market.market_id,
                    "at": utcnow().isoformat(),
                    "status": result.status.value,
                },
            )
            self.repo.add_protection_event(
                "ORDER_UNRESOLVED",
                "WARN",
                market_id=market.market_id,
                client_order_id=intent.client_order_id,
                exchange_order_id=result.exchange_order_id,
                status=result.status.value,
                note="entry outcome is not proven; trading is blocked until reconciliation",
            )
        return ExecutionOutcome(result=result, position=position, note=result.raw_status or "")

    async def execute_exit(
        self,
        intent: OrderIntent,
        market: MarketInfo,
        book: OrderBookSnapshot | None,
        position: PositionRow,
    ) -> ExecutionOutcome:
        mode = self.mode_provider()
        self.journal.persist_intent(intent, reserve_amount=Decimal(0))
        self.journal.mark_submitting(intent)
        started = self.clock.monotonic()
        if mode is Mode.PAPER:
            result = await self.paper.place_order(intent, market, book) if self.paper else OrderResult(
                intent.client_order_id, OrderStatus.REJECTED, error_code="PAPER_UNAVAILABLE", error_message="paper broker not initialised"
            )
        else:
            result = await self.adapter.place_order(intent, market)
        total_ms = int((self.clock.monotonic() - started) * 1000)
        result.latency_ms = max(result.latency_ms, total_ms)
        self.latency.record("exit_order_to_result", total_ms, market.market_id)
        self.journal.mark_result(intent, result)

        realized = Decimal(0)
        if result.filled_shares > 0:
            allocated_cost = position.cost_basis * (result.filled_shares / position.shares) if position.shares > 0 else Decimal(0)
            proceeds = result.filled_shares * result.avg_price - result.fee_usd
            realized = proceeds - allocated_cost
            remaining = max(Decimal(0), position.shares - result.filled_shares)
            status = PositionStatus.CLOSED.value if remaining <= 0 else position.status
            self.repo.update_position(
                position.id,
                shares=remaining,
                cost_basis=max(Decimal(0), position.cost_basis - allocated_cost),
                exit_price=result.avg_price,
                exit_fee=position.exit_fee + result.fee_usd,
                realized_pnl=position.realized_pnl + realized,
                status=status,
                closed_at=utcnow() if remaining <= 0 else position.closed_at,
            )
            self.repo.add_trade(
                market_id=market.market_id,
                client_order_id=intent.client_order_id,
                side=intent.side.value,
                outcome=intent.outcome.value,
                price=result.avg_price,
                shares=result.filled_shares,
                fee=result.fee_usd,
                realized_pnl=realized,
                kind="EXIT",
                mode=mode.value,
            )
        if result.status in {OrderStatus.UNKNOWN, OrderStatus.PENDING, OrderStatus.ACKED}:
            self.repo.set_state(
                "recovery_required",
                {
                    "client_order_id": intent.client_order_id,
                    "market_id": market.market_id,
                    "at": utcnow().isoformat(),
                    "kind": "exit",
                    "status": result.status.value,
                },
            )
            self.repo.add_protection_event(
                "ORDER_UNRESOLVED",
                "WARN",
                market_id=market.market_id,
                client_order_id=intent.client_order_id,
                exchange_order_id=result.exchange_order_id,
                kind="exit",
                status=result.status.value,
                note="exit outcome is not proven; no further SELL will be sent until reconciliation",
            )
        return ExecutionOutcome(result=result, position=self.repo.get_position(market.market_id, mode.value), realized_pnl=realized)

    async def cancel_verified(self, client_order_id: str, exchange_order_id: str | None) -> bool:
        if not exchange_order_id:
            self.journal.mark_cancelled(client_order_id, verified=False)
            return False
        self.journal.mark_cancel_pending(client_order_id)
        ok = await self.adapter.cancel_order(exchange_order_id)
        if not ok:
            self.journal.mark_cancelled(client_order_id, verified=False)
            return False
        open_orders = await self.adapter.get_open_orders()
        still_open = any(o.order_id == exchange_order_id for o in open_orders)
        verified = not still_open
        self.journal.mark_cancelled(client_order_id, verified=verified)
        return verified

    def settle_position(self, position: PositionRow, actual_outcome: str) -> Decimal:
        won = position.outcome.upper() == actual_outcome.upper()
        payout = position.shares if won else Decimal(0)
        realized = payout - position.cost_basis
        self.repo.update_position(
            position.id,
            status=PositionStatus.SETTLED.value,
            resolution_outcome=actual_outcome.upper(),
            realized_pnl=position.realized_pnl + realized,
            exit_price=Decimal(1) if won else Decimal(0),
            closed_at=utcnow(),
        )
        self.repo.add_trade(
            market_id=position.market_id,
            client_order_id=f"settle-{position.id}",
            side="SETTLE",
            outcome=position.outcome,
            price=Decimal(1) if won else Decimal(0),
            shares=position.shares,
            fee=Decimal(0),
            realized_pnl=realized,
            kind="SETTLEMENT",
            mode=position.mode,
        )
        return realized
