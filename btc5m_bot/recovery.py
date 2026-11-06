"""Startup recovery and reconciliation.

Principles: the exchange is the source of truth; an order or position in an
ambiguous state is never guessed into a terminal state. Anything that cannot be
verified blocks trading until a human resolves it.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Callable

from .clock import Clock
from .db.repo import Repository
from .exchange.base import ExchangeAdapter, ExchangeOpenOrder, ExchangePosition
from .types import Mode, OrderStatus, RecoveryState, as_utc

log = logging.getLogger(__name__)

SIZE_TOLERANCE = Decimal("0.001")


@dataclass
class RecoveryResult:
    ready: bool
    state: RecoveryState
    blocks: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    reconciled_orders: int = 0
    reconciled_positions: int = 0
    details: dict = field(default_factory=dict)

    def summary(self) -> str:
        if self.ready:
            return f"recovery READY ({self.reconciled_orders} orders, {self.reconciled_positions} positions)"
        return "recovery BLOCKED: " + "; ".join(self.blocks)


class RecoveryManager:
    def __init__(
        self,
        *,
        repo: Repository,
        adapter: ExchangeAdapter,
        clock: Clock,
        mode: Mode,
        integrity_check: Callable[[], str] | None = None,
    ) -> None:
        self.repo = repo
        self.adapter = adapter
        self.clock = clock
        self.mode = mode
        self.integrity_check = integrity_check

    async def run(self, *, now: datetime | None = None) -> RecoveryResult:
        now = as_utc(now or self.clock.now())
        self.repo.update_recovery_state(state=RecoveryState.RECOVERING.value, detail="startup reconciliation", last_check_at=now)
        blocks: list[str] = []
        notes: list[str] = []
        orders_done = 0
        positions_done = 0

        if self.integrity_check is not None:
            try:
                self.integrity_check()
            except Exception as exc:  # noqa: BLE001 - any integrity failure blocks trading
                blocks.append(f"database integrity check failed: {exc}")

        local_orders_by_id = {row.client_order_id: row for row in self.repo.open_orders(self.mode.value)}
        # FOK/FAK partial fills are terminal at the venue, but a crash can occur after the
        # order journal records the fill and before the position row is written. Reconcile
        # those rows too; treating them as ordinary closed orders can lose real exposure.
        for row in self.repo.list_orders(
            mode=self.mode.value, statuses=[OrderStatus.PARTIAL.value], limit=500
        ):
            if row.order_type.upper() in {"FOK", "FAK"}:
                local_orders_by_id[row.client_order_id] = row
        local_orders = list(local_orders_by_id.values())
        exchange_open: dict[str, ExchangeOpenOrder] = {}
        exchange_positions: dict[str, ExchangePosition] = {}

        if self.mode is Mode.LIVE:
            try:
                exchange_open = {o.order_id: o for o in await self.adapter.get_open_orders()}
                exchange_positions = {p.asset_id: p for p in await self.adapter.get_positions() if p.size > 0}
            except Exception as exc:  # noqa: BLE001 - unavailable exchange state must block
                blocks.append(f"exchange reconciliation unavailable: {exc}")

        for row in local_orders:
            if row.status == OrderStatus.INTENDED.value:
                # Journaled before submission; the crash happened before any network call.
                self.repo.update_order(
                    row.client_order_id,
                    status=OrderStatus.REJECTED.value,
                    error_code="RECOVERED_PRE_SUBMIT",
                    error_message="recovered before submission; reservation released",
                )
                self.repo.release_reservation(row.client_order_id, status="RELEASED")
                orders_done += 1
                continue

            if self.mode is not Mode.LIVE:
                blocks.append(
                    f"paper order {row.client_order_id} in state {row.status} cannot be verified after a restart"
                )
                continue

            outcome = await self._reconcile_live_order(
                row,
                exchange_open=exchange_open,
                exchange_positions=exchange_positions,
                now=now,
                notes=notes,
                blocks=blocks,
            )
            orders_done += int(outcome)

        if self.mode is Mode.LIVE:
            tracked_exchange_ids = {
                row.exchange_order_id
                for row in self.repo.list_orders(mode=self.mode.value, limit=500)
                if row.exchange_order_id
            }
            for order_id, order in exchange_open.items():
                if order_id not in tracked_exchange_ids:
                    blocks.append(f"untracked working order {order_id} on asset {order.asset_id}; reconcile manually")

            positions_done, position_blocks = self._reconcile_positions(
                exchange_positions, [r for r in self.repo.list_positions(mode=self.mode.value, limit=200) if r.status in {"OPEN", "DUST"}]
            )
            blocks.extend(position_blocks)

        ready = not blocks
        state = RecoveryState.READY if ready else RecoveryState.BLOCKED
        self.repo.update_recovery_state(
            state=state.value,
            detail=("recovery complete" if ready else "; ".join(blocks))[:2000],
            last_check_at=now,
            reconciled_orders=orders_done,
            reconciled_positions=positions_done,
        )
        result = RecoveryResult(
            ready=ready,
            state=state,
            blocks=blocks,
            notes=notes,
            reconciled_orders=orders_done,
            reconciled_positions=positions_done,
            details={"mode": self.mode.value},
        )
        log.info(result.summary())
        return result

    # --------------------------------------------------------------- helpers
    async def _reconcile_live_order(
        self,
        row,
        *,
        exchange_open: dict[str, ExchangeOpenOrder],
        exchange_positions: dict[str, ExchangePosition],
        now: datetime,
        notes: list[str],
        blocks: list[str],
    ) -> bool:
        if not row.exchange_order_id:
            if row.status == OrderStatus.SUBMITTING.value:
                blocks.append(f"order {row.client_order_id} was submitting with unknown exchange id; verify on the exchange")
            else:
                blocks.append(f"order {row.client_order_id} has no exchange id in state {row.status}")
            return False

        current = exchange_open.get(row.exchange_order_id)
        if current is None:
            try:
                current = await self.adapter.get_order(row.exchange_order_id)
            except Exception as exc:  # noqa: BLE001
                blocks.append(f"order {row.client_order_id} lookup failed: {exc}")
                return False

        position = exchange_positions.get(row.asset_id)
        if current is not None:
            status = str(current.status or "").lower()
            matched = Decimal(current.size_matched or 0)
            terminal_cancel = status in {"cancelled", "canceled", "expired"}
            if matched <= 0 and terminal_cancel:
                self.repo.update_order(
                    row.client_order_id,
                    status=OrderStatus.CANCELLED.value,
                    filled_shares=Decimal(0),
                    cancelled_at=now,
                    error_code="RECOVERED_CANCELLED",
                )
                self.repo.release_reservation(row.client_order_id, status="RELEASED")
                notes.append(f"order {row.client_order_id} confirmed {status}")
                return True
            if matched > 0:
                if row.side.upper() != "BUY":
                    blocks.append(
                        f"exit order {row.client_order_id} has recovered fills but trade-level fill price/fee reconciliation is unavailable"
                    )
                    return False
                if position is None or Decimal(position.avg_price or 0) <= 0:
                    blocks.append(
                        f"buy order {row.client_order_id} has {matched} matched shares but the exchange position/average fill price is not yet visible"
                    )
                    return False
                if abs(Decimal(position.size) - matched) > SIZE_TOLERANCE:
                    blocks.append(
                        f"buy order {row.client_order_id} matched {matched} shares but exchange position is {position.size}; manual reconciliation required"
                    )
                    return False
                price = Decimal(position.avg_price)
                status_value = (
                    OrderStatus.FILLED.value
                    if matched >= Decimal(row.shares) - SIZE_TOLERANCE
                    else OrderStatus.PARTIAL.value
                )
                self.repo.update_order(
                    row.client_order_id,
                    status=status_value,
                    filled_shares=matched,
                    avg_price=price,
                    acked_at=row.acked_at or now,
                    cancelled_at=now if terminal_cancel else row.cancelled_at,
                    error_code="RECOVERED_PARTIAL_CANCELLED" if terminal_cancel else row.error_code,
                )
                if status_value == OrderStatus.FILLED.value or terminal_cancel or (
                    status_value == OrderStatus.PARTIAL.value and row.order_type.upper() in {"FOK", "FAK"}
                ):
                    self.repo.release_reservation(row.client_order_id, status="CONSUMED")
                self._adopt_position(row, matched, price, Decimal(row.fee_usd or 0), notes)
                notes.append(f"order {row.client_order_id} reconciled as {status_value} ({matched} shares)")
                return True
            # Still working with no fill: cancel it during recovery and verify.
            if row.exchange_order_id in exchange_open:
                cancelled = await self.adapter.cancel_order(row.exchange_order_id)
                if not cancelled:
                    blocks.append(f"could not cancel orphan order {row.exchange_order_id}")
                    return False
                try:
                    check = await self.adapter.get_order(row.exchange_order_id)
                except Exception as exc:  # noqa: BLE001
                    blocks.append(f"cancellation of {row.exchange_order_id} could not be verified: {exc}")
                    return False
                if check is not None and str(check.status).lower() not in {"cancelled", "canceled", "expired"}:
                    blocks.append(f"cancellation unverified for {row.exchange_order_id}")
                    return False
                self.repo.update_order(
                    row.client_order_id,
                    status=OrderStatus.CANCELLED.value,
                    cancelled_at=now,
                    error_code="RECOVERY_CANCELLED",
                )
                self.repo.release_reservation(row.client_order_id, status="RELEASED")
                notes.append(f"orphan order {row.exchange_order_id} cancelled and verified")
                return True
            return False

        # Order not found on the exchange. If the matching position proves a fill, adopt it.
        if position is not None and Decimal(position.size) > 0:
            if row.side.upper() != "BUY":
                blocks.append(
                    f"exit order {row.client_order_id} is absent from open orders but its position changed; trade-level fill reconciliation is required"
                )
                return False
            if Decimal(position.avg_price or 0) <= 0 or Decimal(position.size) > Decimal(row.shares) + SIZE_TOLERANCE:
                blocks.append(
                    f"exchange position for {row.client_order_id} cannot be safely attributed to this order"
                )
                return False
            matched = Decimal(position.size)
            price = Decimal(position.avg_price)
            status_value = (
                OrderStatus.FILLED.value
                if matched >= Decimal(row.shares) - SIZE_TOLERANCE
                else OrderStatus.PARTIAL.value
            )
            self.repo.update_order(
                row.client_order_id,
                status=status_value,
                filled_shares=matched,
                avg_price=price,
                filled_at=now,
                error_code="RECOVERED_FROM_POSITION",
            )
            self.repo.release_reservation(row.client_order_id, status="CONSUMED")
            self._adopt_position(row, matched, price, Decimal(row.fee_usd or 0), notes)
            notes.append(f"order {row.client_order_id} inferred {status_value} from exchange position")
            return True

        blocks.append(
            f"order {row.client_order_id} ({row.exchange_order_id}) is neither open nor provably filled; manual verification required"
        )
        return False

    def _adopt_position(self, row, shares: Decimal, price: Decimal, entry_fee: Decimal, notes: list[str]) -> None:
        if row.side.upper() != "BUY":
            notes.append(f"exit order {row.client_order_id} reconciled; position accounting requires review")
            return
        existing = self.repo.get_position(row.market_id, self.mode.value)
        if existing is not None:
            return
        self.repo.create_position(
            market_id=row.market_id,
            condition_id=row.condition_id,
            asset_id=row.asset_id,
            outcome=row.outcome,
            shares=shares,
            avg_price=price,
            entry_fee=entry_fee,
            mode=self.mode.value,
        )
        self.repo.add_protection_event(
            "POSITION_ADOPTED",
            "WARN",
            market_id=row.market_id,
            shares=str(shares),
            price=str(price),
            note="reconciled from exchange after restart",
        )
        notes.append(f"adopted position {row.market_id} {row.outcome} {shares}@{price}")

    def _reconcile_positions(
        self,
        exchange_positions: dict[str, ExchangePosition],
        local_positions: list,
    ) -> tuple[int, list[str]]:
        blocks: list[str] = []
        done = 0
        local_by_asset = {p.asset_id: p for p in local_positions}
        for asset_id, position in exchange_positions.items():
            local = local_by_asset.pop(asset_id, None)
            if local is None:
                blocks.append(
                    f"untracked exchange position {asset_id} size {position.size}; no local record - manual reconciliation required"
                )
                continue
            if abs(Decimal(position.size) - Decimal(local.shares)) > SIZE_TOLERANCE:
                blocks.append(
                    f"position mismatch {asset_id}: exchange {position.size} vs local {local.shares}"
                )
                continue
            done += 1
        for asset_id, local in local_by_asset.items():
            if local.status == "DUST" and local.shares < Decimal("0.01"):
                continue
            blocks.append(f"local position {asset_id} ({local.shares} shares) not present on the exchange")
        return done, blocks
