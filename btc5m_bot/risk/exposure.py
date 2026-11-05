"""Exposure ledger: reservations, open positions and used entries."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from ..db.models import PositionRow
from ..db.repo import Repository
from ..types import OrderStatus, Side


@dataclass(frozen=True)
class ExposureSummary:
    reserved: Decimal
    open_position_cost: Decimal
    open_positions: int
    open_orders: int
    available_balance: Decimal | None

    @property
    def committed(self) -> Decimal:
        return self.reserved + self.open_position_cost


class ExposureLedger:
    def __init__(self, repo: Repository) -> None:
        self.repo = repo

    def open_positions(self, mode: str) -> list[PositionRow]:
        return [
            row
            for row in self.repo.list_positions(mode=mode, limit=50)
            if row.status in {"OPEN", "DUST"} and row.shares > 0
        ]

    def summary(self, mode: str, balance: Decimal | None) -> ExposureSummary:
        positions = self.open_positions(mode)
        cost = sum((p.cost_basis for p in positions), Decimal(0))
        reserved = self.repo.active_reserved_total()
        open_orders = len(self.repo.open_orders(mode=mode))
        available = None if balance is None else max(Decimal(0), balance - reserved)
        return ExposureSummary(
            reserved=reserved,
            open_position_cost=cost,
            open_positions=len(positions),
            open_orders=open_orders,
            available_balance=available,
        )

    def entries_used(self, market_id: str, mode: str) -> int:
        """An entry is consumed once an order for the market was actually submitted.

        Pre-submit rejections (REJECTED) do not consume the one-entry allowance; any
        order that reached SUBMITTING or beyond does.
        """
        orders = self.repo.list_orders(market_id=market_id, mode=mode, limit=200)
        return sum(
            1
            for order in orders
            if order.side == Side.BUY.value and order.status != OrderStatus.REJECTED.value
        )

    def has_active_position(self, mode: str) -> bool:
        return bool(self.open_positions(mode))

    def active_position(self, mode: str) -> PositionRow | None:
        rows = self.open_positions(mode)
        return rows[0] if rows else None
