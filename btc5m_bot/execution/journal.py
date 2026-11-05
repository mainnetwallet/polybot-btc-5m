"""Write-ahead order journal. The intent is persisted before any network call."""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime
from decimal import Decimal

from ..db.repo import DuplicateOrderError, Repository
from ..types import MarketInfo, OrderIntent, OrderResult, OrderStatus, Outcome, Side, utcnow


class OrderJournal:
    def __init__(self, repo: Repository) -> None:
        self.repo = repo

    def new_intent(
        self,
        *,
        market: MarketInfo,
        outcome: Outcome,
        side: Side,
        shares: Decimal,
        limit_price: Decimal,
        order_type: str = "FOK",
        mode: str,
        strategy_version: str = "BOT_BASELINE",
        reason: str = "",
        now: datetime | None = None,
    ) -> OrderIntent:
        client_order_id = uuid.uuid4().hex
        material = f"{market.market_id}:{side.value}:{outcome.value}:{shares}:{limit_price}:{order_type}"
        idempotency_key = hashlib.sha256(material.encode()).hexdigest()
        return OrderIntent(
            client_order_id=client_order_id,
            idempotency_key=idempotency_key,
            market_id=market.market_id,
            condition_id=market.condition_id,
            asset_id=market.asset_id(outcome),
            outcome=outcome,
            side=side,
            shares=shares,
            limit_price=limit_price,
            order_type=order_type,
            strategy_version=strategy_version,
            created_at=now or utcnow(),
            reason=reason,
        )

    def persist_intent(self, intent: OrderIntent, *, reserve_amount: Decimal) -> None:
        self.repo.create_order_intent(intent, status=OrderStatus.INTENDED)
        self.repo.reserve(intent.market_id, intent.client_order_id, reserve_amount, note=intent.outcome.value)

    def mark_submitting(self, intent: OrderIntent) -> None:
        self.repo.update_order(intent.client_order_id, status=OrderStatus.SUBMITTING.value, submitted_at=utcnow())

    def mark_result(self, intent: OrderIntent, result: OrderResult) -> None:
        status = result.status
        fields: dict = {
            "status": status.value,
            "exchange_order_id": result.exchange_order_id,
            "filled_shares": result.filled_shares,
            "avg_price": result.avg_price,
            "fee_usd": result.fee_usd,
            "latency_ms": result.latency_ms,
            "raw_response": {"status": result.raw_status, "transactions": list(result.transactions)},
            "error_code": result.error_code,
            "error_message": result.error_message,
        }
        now = utcnow()
        if status in {OrderStatus.ACKED, OrderStatus.PENDING, OrderStatus.PARTIAL, OrderStatus.FILLED}:
            fields["acked_at"] = now
        if status is OrderStatus.FILLED:
            fields["filled_at"] = now
            self.repo.release_reservation(intent.client_order_id, status="CONSUMED")
        elif status is OrderStatus.PARTIAL and intent.order_type.upper() in {"FOK", "FAK"}:
            # A market order's unfilled remainder is cancelled by the exchange, so a
            # partial is terminal. Releasing the reservation prevents double-counting
            # exposure alongside the position cost basis.
            self.repo.release_reservation(
                intent.client_order_id,
                status="CONSUMED" if result.filled_shares > 0 else "RELEASED",
            )
        elif status in {OrderStatus.REJECTED, OrderStatus.CANCELLED}:
            fields["filled_at"] = now if status is OrderStatus.CANCELLED else None
            self.repo.release_reservation(intent.client_order_id, status="RELEASED")
        # UNKNOWN keeps the reservation ACTIVE until reconciled.
        self.repo.update_order(intent.client_order_id, **fields)

    def mark_cancel_pending(self, client_order_id: str) -> None:
        self.repo.update_order(client_order_id, status=OrderStatus.CANCEL_PENDING.value, cancel_requested_at=utcnow())

    def mark_cancelled(self, client_order_id: str, *, verified: bool) -> None:
        if not verified:
            # Cancellation is never assumed; keep CANCEL_PENDING/UNKNOWN for reconciliation.
            self.repo.update_order(client_order_id, status=OrderStatus.UNKNOWN.value, error_message="cancel unverified")
            return
        self.repo.update_order(client_order_id, status=OrderStatus.CANCELLED.value, cancelled_at=utcnow())
        self.repo.release_reservation(client_order_id, status="RELEASED")
