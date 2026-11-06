"""Deterministic in-memory exchange used by tests and offline simulations."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from ..types import (
    MarketInfo,
    OrderBookSnapshot,
    OrderIntent,
    OrderResult,
    OrderStatus,
    Side,
    as_utc,
)
from .base import (
    Balance,
    ExchangeAdapter,
    ExchangeOpenOrder,
    ExchangePosition,
    ExchangeTradeFill,
    GeoResult,
    HeartbeatResult,
    NotConfiguredError,
)
from .fees import FeeSchedule, taker_fee


class FakeExchangeAdapter(ExchangeAdapter):
    name = "fake"
    supports_trading = True

    def __init__(self) -> None:
        self.markets: dict[str, MarketInfo] = {}
        self.books: dict[str, OrderBookSnapshot] = {}
        self.balance = Balance(available=Decimal("1000"), total=Decimal("1000"), allowance=Decimal("1000"))
        self.positions: list[ExchangePosition] = []
        self.open_orders: dict[str, ExchangeOpenOrder] = {}
        self.resolutions: dict[str, str] = {}
        self.order_fills: dict[str, list[ExchangeTradeFill]] = {}
        self.geo = GeoResult(blocked=False, country="BD", ip="127.0.0.1", detail="fake")
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.heartbeat_ids: list[str] = []
        self.placed: list[OrderIntent] = []
        self.fail_place = False
        self.unknown_place = False
        self.partial_fill_ratio = Decimal(1)
        self.cancel_ok = True
        self.closed = False

    async def discover_markets(self, now: datetime) -> list[MarketInfo]:
        return list(self.markets.values())

    def set_book(self, asset_id: str, bids: list[tuple[str, str]], asks: list[tuple[str, str]], **kwargs: Any) -> None:
        from ..types import BookLevel

        self.books[asset_id] = OrderBookSnapshot(
            asset_id=asset_id,
            timestamp=kwargs.pop("timestamp", self.now),
            bids=tuple(BookLevel(Decimal(p), Decimal(s)) for p, s in bids),
            asks=tuple(BookLevel(Decimal(p), Decimal(s)) for p, s in asks),
            tick_size=Decimal(kwargs.pop("tick_size", "0.01")),
            min_order_size=Decimal(kwargs.pop("min_order_size", "5")),
            source="fake",
            **kwargs,
        )

    async def get_order_book(self, asset_id: str) -> OrderBookSnapshot:
        book = self.books.get(asset_id)
        if book is None:
            raise KeyError(asset_id)
        return book

    async def get_balance(self) -> Balance:
        return self.balance

    async def get_positions(self) -> list[ExchangePosition]:
        return list(self.positions)

    async def place_order(self, intent: OrderIntent, market: MarketInfo) -> OrderResult:
        self.placed.append(intent)
        if self.fail_place:
            return OrderResult(
                client_order_id=intent.client_order_id,
                status=OrderStatus.REJECTED,
                error_code="FAKE_REJECT",
                error_message="forced rejection",
            )
        if self.unknown_place:
            return OrderResult(
                client_order_id=intent.client_order_id,
                status=OrderStatus.UNKNOWN,
                error_code="UNKNOWN",
                error_message="forced unknown",
            )
        try:
            book = self.books[intent.asset_id]
        except KeyError:
            return OrderResult(
                client_order_id=intent.client_order_id,
                status=OrderStatus.REJECTED,
                error_code="NO_BOOK",
                error_message="no book",
            )
        wanted = intent.shares * self.partial_fill_ratio
        avg, filled = book.avg_exec_price(intent.side, wanted)
        if filled <= 0:
            return OrderResult(
                client_order_id=intent.client_order_id,
                status=OrderStatus.CANCELLED,
                exchange_order_id="fake-" + intent.client_order_id[:8],
                raw_status="unfilled",
            )
        if intent.side is Side.BUY and avg > intent.limit_price:
            # FOK at limit would not fill
            return OrderResult(
                client_order_id=intent.client_order_id,
                status=OrderStatus.CANCELLED,
                exchange_order_id="fake-" + intent.client_order_id[:8],
                raw_status="limit_not_met",
            )
        if intent.side is Side.SELL and wanted > 0:
            # Real CLOB semantics: min_price is a per-level floor, so only bids at or above it match.
            matchable = sum((l.size for l in book.bids if l.price >= intent.limit_price), Decimal(0))
            if matchable < wanted:
                return OrderResult(
                    client_order_id=intent.client_order_id,
                    status=OrderStatus.CANCELLED,
                    exchange_order_id="fake-" + intent.client_order_id[:8],
                    raw_status="limit_not_met",
                )
        fee = taker_fee(filled, avg, FeeSchedule.from_market(market))
        order_id = "fake-" + intent.client_order_id[:12]
        self.open_orders.pop(order_id, None)
        return OrderResult(
            client_order_id=intent.client_order_id,
            status=OrderStatus.FILLED if filled >= intent.shares else OrderStatus.PARTIAL,
            exchange_order_id=order_id,
            filled_shares=filled,
            avg_price=avg,
            fee_usd=fee,
            raw_status="matched",
        )

    async def cancel_order(self, order_id: str) -> bool:
        if not self.cancel_ok:
            return False
        return self.open_orders.pop(order_id, None) is not None

    async def cancel_all(self) -> int:
        count = len(self.open_orders)
        self.open_orders.clear()
        return count

    async def get_open_orders(self, market_id: str | None = None) -> list[ExchangeOpenOrder]:
        orders = list(self.open_orders.values())
        if market_id:
            orders = [o for o in orders if o.condition_id == market_id]
        return orders

    async def get_order(self, order_id: str) -> ExchangeOpenOrder | None:
        return self.open_orders.get(order_id)

    async def get_order_fills(
        self, *, order_id: str, asset_id: str, start: datetime, end: datetime
    ) -> list[ExchangeTradeFill]:
        return [
            fill for fill in self.order_fills.get(order_id, [])
            if fill.asset_id == asset_id and start <= fill.matched_at <= end
        ]

    async def get_resolution(self, condition_id: str, outcomes: tuple[str, ...] | None = None) -> str | None:
        return self.resolutions.get(condition_id)

    async def geoblock(self) -> GeoResult:
        return self.geo

    async def send_heartbeat(self, heartbeat_id: str) -> HeartbeatResult:
        new_id = heartbeat_id or "hb-0001"
        if heartbeat_id:
            new_id = f"hb-{len(self.heartbeat_ids) + 2:04d}"
        self.heartbeat_ids.append(new_id)
        return HeartbeatResult(heartbeat_id=new_id, armed=True, raw={})

    async def redeem(self, *, condition_id: str | None = None, position_id: str | None = None) -> str:
        if not condition_id and not position_id:
            raise NotConfiguredError("condition_id or position_id required")
        return "0xfake-redeem"

    async def server_time(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)
