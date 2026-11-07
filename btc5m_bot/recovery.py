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
from .exchange.fees import FeeSchedule, taker_fee
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
                    return await self._reconcile_sell_order(
                        row, position=position, current=current, now=now, notes=notes, blocks=blocks
                    )
                if position is None or Decimal(position.avg_price or 0) <= 0:
                    blocks.append(
                        f"buy order {row.client_order_id} has {matched} matched shares but the exchange position/average fill price is not yet visible"
                    )
                    return False
                # A top-up adds to a position that is already open, so the exchange size is the
                # total of both, not just this order's fill.
                local = self._open_local_position(row)
                already_merged = topup = False
                if local is None:
                    if abs(Decimal(position.size) - matched) > SIZE_TOLERANCE:
                        blocks.append(
                            f"buy order {row.client_order_id} matched {matched} shares but exchange position is {position.size}; manual reconciliation required"
                        )
                        return False
                else:
                    gap = Decimal(position.size) - Decimal(local.shares)
                    if abs(gap) <= SIZE_TOLERANCE:
                        already_merged = True  # crashed after the merge, before the order row was closed
                    elif abs(gap - matched) <= SIZE_TOLERANCE:
                        topup = True
                    else:
                        blocks.append(
                            f"buy order {row.client_order_id} matched {matched} shares but exchange position is {position.size} "
                            f"against {local.shares} local; manual reconciliation required"
                        )
                        return False
                price = Decimal(position.avg_price)
                if topup:
                    price = self._topup_price(position, local, matched)
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
                if topup:
                    self._merge_topup(row, local, matched, price, Decimal(row.fee_usd or 0), notes)
                elif not already_merged:
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
                return await self._reconcile_sell_order(
                    row, position=position, current=None, now=now, notes=notes, blocks=blocks
                )
            local = self._open_local_position(row)
            topup = local is not None
            # With a position already open, only the growth over it can belong to this order. If
            # the size did not change, the fill is unproven (it may have been merged, or never
            # have happened), so that is left to a person.
            matched = Decimal(position.size) - (Decimal(local.shares) if local is not None else Decimal(0))
            if (
                Decimal(position.avg_price or 0) <= 0
                or matched <= SIZE_TOLERANCE
                or matched > Decimal(row.shares) + SIZE_TOLERANCE
            ):
                blocks.append(
                    f"exchange position for {row.client_order_id} cannot be safely attributed to this order"
                )
                return False
            price = self._topup_price(position, local, matched) if topup else Decimal(position.avg_price)
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
            if topup:
                self._merge_topup(row, local, matched, price, Decimal(row.fee_usd or 0), notes)
            else:
                self._adopt_position(row, matched, price, Decimal(row.fee_usd or 0), notes)
            notes.append(f"order {row.client_order_id} inferred {status_value} from exchange position")
            return True

        blocks.append(
            f"order {row.client_order_id} ({row.exchange_order_id}) is neither open nor provably filled; manual verification required"
        )
        return False

    async def _reconcile_sell_order(
        self, row, *, position: ExchangePosition | None, current: ExchangeOpenOrder | None,
        now: datetime, notes: list[str], blocks: list[str],
    ) -> bool:
        if row.order_type.upper() not in {"FOK", "FAK"}:
            blocks.append(f"SELL order {row.client_order_id} is not an immediate-or-cancel order; refusing to infer terminal state")
            return False
        if current is not None:
            current_status = str(current.status or "").lower()
            if current_status not in {"matched", "filled", "cancelled", "canceled", "expired"}:
                cancelled = await self.adapter.cancel_order(row.exchange_order_id)
                if not cancelled:
                    blocks.append(f"SELL order {row.client_order_id} could not be cancelled before fill reconciliation")
                    return False
                try:
                    current = await self.adapter.get_order(row.exchange_order_id)
                except Exception as exc:
                    blocks.append(f"SELL order {row.client_order_id} cancellation status unavailable: {exc}")
                    return False
                if current is not None and str(current.status or "").lower() not in {"matched", "filled", "cancelled", "canceled", "expired"}:
                    blocks.append(f"SELL order {row.client_order_id} cancellation is not terminal")
                    return False
        try:
            fills = await self.adapter.get_order_fills(
                order_id=row.exchange_order_id, asset_id=row.asset_id,
                start=row.created_at, end=now,
            )
        except Exception as exc:
            blocks.append(f"SELL order {row.client_order_id} trade history unavailable: {exc}")
            return False
        if not fills:
            blocks.append(f"SELL order {row.client_order_id} has no confirmed order-attributed fills")
            return False
        if any(
            f.order_id != row.exchange_order_id or f.asset_id != row.asset_id
            or f.condition_id != row.condition_id or f.side.upper() != "SELL"
            or f.trader_side.upper() != "TAKER" or f.status.upper() != "CONFIRMED"
            or not f.transaction_hash
            for f in fills
        ):
            blocks.append(f"SELL order {row.client_order_id} has unconfirmed or inconsistent trade records")
            return False
        market = self.repo.get_market(row.market_id)
        if market is None:
            blocks.append(f"SELL order {row.client_order_id} has no persisted fee schedule")
            return False
        schedule = FeeSchedule(
            rate=Decimal(market.fee_rate), exponent=int(market.fee_exponent),
            taker_only=bool(market.taker_only), rebate_rate=Decimal(market.rebate_rate),
            enabled=bool(market.fees_enabled),
        )
        if not schedule.enabled and any(f.fee_rate_bps != 0 for f in fills):
            blocks.append(f"SELL order {row.client_order_id} fee schedule disagrees with venue trade records")
            return False
        expected_bps = schedule.rate * Decimal(10000) if schedule.enabled else Decimal(0)
        if any(abs(f.fee_rate_bps - expected_bps) > Decimal("1") for f in fills):
            blocks.append(f"SELL order {row.client_order_id} venue fee rate differs from persisted market schedule")
            return False
        total_shares = sum((f.shares for f in fills), Decimal(0))
        total_gross = sum((f.shares * f.price for f in fills), Decimal(0))
        total_fee = sum((taker_fee(f.shares, f.price, schedule) for f in fills), Decimal(0))
        prior_shares = Decimal(row.filled_shares or 0)
        prior_fee = Decimal(row.fee_usd or 0)
        prior_gross = prior_shares * Decimal(row.avg_price or 0)
        if total_shares < prior_shares - SIZE_TOLERANCE or total_shares > Decimal(row.shares) + SIZE_TOLERANCE:
            blocks.append(f"SELL order {row.client_order_id} recovered fill quantity is inconsistent")
            return False
        delta_shares = max(Decimal(0), total_shares - prior_shares)
        delta_gross = total_gross - prior_gross
        delta_fee = max(Decimal(0), total_fee - prior_fee)
        if any(f.shares <= 0 or f.price <= 0 or f.price >= 1 for f in fills):
            blocks.append(f"SELL order {row.client_order_id} contains invalid fill quantities or prices")
            return False
        if delta_gross < 0 or delta_fee < 0:
            blocks.append(f"SELL order {row.client_order_id} cumulative fills/fees moved backwards")
            return False
        local_position = self.repo.get_position(row.market_id, self.mode.value)
        if local_position is None or local_position.shares <= 0:
            blocks.append(f"SELL order {row.client_order_id} has no local open position to reconcile")
            return False
        if delta_shares > local_position.shares + SIZE_TOLERANCE:
            blocks.append(f"SELL order {row.client_order_id} recovered shares exceed the remaining local position")
            return False
        remaining = max(Decimal(0), local_position.shares - delta_shares)
        if position is not None and abs(Decimal(position.size) - remaining) > SIZE_TOLERANCE:
            blocks.append(f"SELL order {row.client_order_id} exchange position {position.size} does not match expected {remaining}")
            return False
        if position is None and remaining > SIZE_TOLERANCE:
            blocks.append(f"SELL order {row.client_order_id} position is missing but expected {remaining} shares to remain")
            return False
        if delta_shares <= 0:
            delta_avg = Decimal(0)
            realized = Decimal(0)
            allocated_cost = Decimal(0)
            remaining_fee_basis = local_position.entry_fee
        else:
            delta_avg = delta_gross / delta_shares
            allocated_cost = local_position.cost_basis * (delta_shares / local_position.shares)
            realized = delta_gross - delta_fee - allocated_cost
            remaining_fee_basis = local_position.entry_fee * (remaining / local_position.shares)
        status = (
            OrderStatus.FILLED.value
            if total_shares >= Decimal(row.shares) - SIZE_TOLERANCE
            else OrderStatus.PARTIAL.value
        )
        if status == OrderStatus.PARTIAL.value and current is None and row.order_type.upper() == "FOK":
            blocks.append(f"SELL FOK order {row.client_order_id} has a partial fill without terminal order evidence")
            return False
        try:
            changed = self.repo.reconcile_sell_fill(
                client_order_id=row.client_order_id, position_id=local_position.id,
                status=status, total_filled=total_shares,
                total_avg_price=(total_gross / total_shares),
                total_fee=total_fee, delta_filled=delta_shares,
                delta_avg_price=delta_avg, delta_fee=delta_fee,
                delta_realized_pnl=realized, remaining_shares=remaining,
                remaining_cost_basis=max(Decimal(0), local_position.cost_basis - allocated_cost),
                remaining_entry_fee=remaining_fee_basis, filled_at=now,
                cancelled_at=now if status == OrderStatus.PARTIAL.value else None,
            )
        except Exception as exc:
            blocks.append(f"SELL order {row.client_order_id} atomic accounting failed: {exc}")
            return False
        if changed:
            notes.append(f"SELL order {row.client_order_id} reconciled from {len(fills)} confirmed venue fills ({total_shares} shares)")
        return True

    def _open_local_position(self, row):
        """The open local position this BUY order would add to, or None."""
        local = self.repo.get_position(row.market_id, self.mode.value)
        if local is None or local.status not in {"OPEN", "DUST"} or Decimal(local.shares) <= 0:
            return None
        return local if local.asset_id == row.asset_id else None

    @staticmethod
    def _topup_price(position: ExchangePosition, local, matched: Decimal) -> Decimal:
        """Average price of the added shares, from the exchange's blended average."""
        total_cost = Decimal(position.avg_price) * Decimal(position.size)
        added_cost = total_cost - Decimal(local.avg_price) * Decimal(local.shares)
        price = added_cost / matched if matched > 0 else Decimal(0)
        if price <= 0 or price > 1:
            return Decimal(position.avg_price)
        return price

    def _merge_topup(self, row, local, shares: Decimal, price: Decimal, entry_fee: Decimal, notes: list[str]) -> None:
        new_shares = Decimal(local.shares) + shares
        self.repo.update_position(
            local.id,
            shares=new_shares,
            avg_price=(Decimal(local.shares) * Decimal(local.avg_price) + shares * price) / new_shares,
            entry_fee=Decimal(local.entry_fee or 0) + entry_fee,
            cost_basis=Decimal(local.cost_basis) + shares * price + entry_fee,
        )
        self.repo.add_trade(
            market_id=row.market_id,
            client_order_id=row.client_order_id,
            side=row.side,
            outcome=row.outcome,
            price=price,
            shares=shares,
            fee=entry_fee,
            kind="ENTRY_ADD",
            mode=self.mode.value,
        )
        self.repo.add_protection_event(
            "POSITION_TOPUP_ADOPTED",
            "WARN",
            market_id=row.market_id,
            shares=str(shares),
            price=str(price),
            note="top-up fill reconciled from exchange after restart",
        )
        notes.append(f"merged top-up {row.market_id} {row.outcome} +{shares}@{price}")

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
