"""Exchange boundary. Trading logic only ever talks to this interface."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from ..types import MarketInfo, OrderBookSnapshot, OrderIntent, OrderResult


class NotConfiguredError(RuntimeError):
    """Raised when a credentialed operation is attempted without credentials."""


class ExchangeError(RuntimeError):
    pass


@dataclass
class Balance:
    available: Decimal
    total: Decimal
    allowance: Decimal | None = None
    asset: str = "pUSD"
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExchangePosition:
    asset_id: str
    condition_id: str
    size: Decimal
    avg_price: Decimal
    current_value: Decimal = Decimal(0)
    redeemable: bool = False
    outcome: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExchangeOpenOrder:
    order_id: str
    asset_id: str
    condition_id: str
    side: str
    price: Decimal
    original_size: Decimal
    size_matched: Decimal
    status: str
    created_at: datetime | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class GeoResult:
    blocked: bool | None
    country: str | None = None
    region: str | None = None
    ip: str | None = None
    detail: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def verified_eligible(self) -> bool:
        return self.blocked is False


@dataclass
class HeartbeatResult:
    heartbeat_id: str
    armed: bool
    raw: dict[str, Any] = field(default_factory=dict)


class ExchangeAdapter(ABC):
    name: str = "base"
    supports_trading: bool = False

    @abstractmethod
    async def discover_markets(self, now: datetime) -> list[MarketInfo]: ...

    @abstractmethod
    async def get_order_book(self, asset_id: str) -> OrderBookSnapshot: ...

    @abstractmethod
    async def get_balance(self) -> Balance: ...

    @abstractmethod
    async def get_positions(self) -> list[ExchangePosition]: ...

    @abstractmethod
    async def place_order(self, intent: OrderIntent, market: MarketInfo) -> OrderResult: ...

    @abstractmethod
    async def cancel_order(self, order_id: str) -> bool: ...

    @abstractmethod
    async def cancel_all(self) -> int: ...

    @abstractmethod
    async def get_open_orders(self, market_id: str | None = None) -> list[ExchangeOpenOrder]: ...

    @abstractmethod
    async def get_order(self, order_id: str) -> ExchangeOpenOrder | None: ...

    @abstractmethod
    async def get_resolution(self, condition_id: str, outcomes: tuple[str, ...] | None = None) -> str | None: ...

    @abstractmethod
    async def geoblock(self) -> GeoResult: ...

    @abstractmethod
    async def send_heartbeat(self, heartbeat_id: str) -> HeartbeatResult: ...

    @abstractmethod
    async def redeem(self, *, condition_id: str | None = None, position_id: str | None = None) -> str: ...

    @abstractmethod
    async def server_time(self) -> datetime: ...

    async def close(self) -> None:
        return None
