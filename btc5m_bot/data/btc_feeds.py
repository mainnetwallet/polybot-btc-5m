"""Binance and Coinbase public trade websockets feeding one BtcSeries each.

Parsers are strict and never raise: foreign, malformed or non-finite messages yield ``None``.
Feeds reconnect with exponential backoff and force a reconnect if the socket goes silent.
Nothing here ever substitutes one provider for another (docs/SPEC.md section 10).
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

import websockets

from ..clock import Clock, RealClock
from ..types import as_utc
from .series import BtcSeries, BtcTick

log = logging.getLogger(__name__)

BINANCE_URL = "wss://stream.binance.com:9443/ws/btcusdt@trade"
COINBASE_URL = "wss://ws-feed.exchange.coinbase.com"
COINBASE_PRODUCT = "BTC-USD"


# --------------------------------------------------------------------- parsing
def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _epoch_ms(value: Any) -> datetime | None:
    number = _decimal(value)
    if number is None or number <= 0:
        return None
    try:
        return datetime.fromtimestamp(float(number) / 1000.0, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return as_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def parse_binance(message: Any, recv_ts: datetime) -> BtcTick | None:
    """Parse a Binance ``trade`` event. Sequence is the trade id (``t``), contiguous per symbol."""
    if not isinstance(message, dict):
        return None
    if isinstance(message.get("data"), dict):  # combined-stream wrapper
        message = message["data"]
    if message.get("e") != "trade":
        return None
    price = _decimal(message.get("p"))
    if price is None or price <= 0:
        return None
    qty = _decimal(message.get("q")) if message.get("q") is not None else Decimal(0)
    if qty is None or qty < 0:
        return None
    ts = _epoch_ms(message.get("T")) or _epoch_ms(message.get("E")) or as_utc(recv_ts)
    return BtcTick(
        ts=ts, price=price, qty=qty, source="binance",
        sequence=_int(message.get("t")), recv_ts=as_utc(recv_ts),
    )


def parse_coinbase(message: Any, recv_ts: datetime) -> BtcTick | None:
    """Parse a Coinbase ``match`` / ``last_match`` message.

    The tick sequence is ``trade_id`` when present (contiguous per product, so gaps mean dropped
    trades) and falls back to the message ``sequence``. The raw message ``sequence`` is not
    contiguous on the public ``matches`` channel, so it would cause false gap alarms.
    """
    if not isinstance(message, dict):
        return None
    if message.get("type") not in {"match", "last_match"}:
        return None
    product = message.get("product_id")
    if product is not None and product != COINBASE_PRODUCT:
        return None
    price = _decimal(message.get("price"))
    if price is None or price <= 0:
        return None
    qty = _decimal(message.get("size")) if message.get("size") is not None else Decimal(0)
    if qty is None or qty < 0:
        return None
    ts = _iso(message.get("time")) or as_utc(recv_ts)
    sequence = _int(message.get("trade_id"))
    if sequence is None:
        sequence = _int(message.get("sequence"))
    return BtcTick(
        ts=ts, price=price, qty=qty, source="coinbase",
        sequence=sequence, recv_ts=as_utc(recv_ts),
    )


# ------------------------------------------------------------------------ feeds
class WebsocketBtcFeed:
    """Base class: connect, subscribe, parse, add to ``series``, reconnect on failure."""

    name = "feed"
    default_url = ""

    def __init__(
        self,
        url: str | None = None,
        *,
        series: BtcSeries | None = None,
        clock: Clock | None = None,
        idle_timeout_seconds: float = 15.0,
    ) -> None:
        self.url = url or self.default_url
        self.series = series if series is not None else BtcSeries(self.name)
        self.clock: Clock = clock or RealClock()
        self.idle_timeout_seconds = idle_timeout_seconds
        self.connected = False
        self.last_error: str | None = None
        self.last_message_at: datetime | None = None
        self.accepted = 0
        self.rejected = 0
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._backoff = 1.0

    # -- provider specifics
    def parse(self, message: Any, recv_ts: datetime) -> BtcTick | None:
        raise NotImplementedError

    def subscribe_messages(self) -> list[dict[str, Any]]:
        return []

    # -- lifecycle
    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self.run(), name=f"btc-feed-{self.name}")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        self.connected = False

    def handle_raw(self, raw: str | bytes) -> BtcTick | None:
        """Decode and ingest one websocket frame. Returns the tick if it was accepted."""
        recv_ts = self.clock.now()
        try:
            message = json.loads(raw)
        except (TypeError, ValueError):
            self.rejected += 1
            return None
        self.last_message_at = recv_ts
        tick = self.parse(message, recv_ts)
        if tick is None:
            return None  # subscription acks, heartbeats and other non-trade frames
        if self.series.add(tick):
            self.accepted += 1
            return tick
        self.rejected += 1
        return None

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                async with websockets.connect(
                    self.url, ping_interval=20, ping_timeout=20, close_timeout=5, max_size=2**20
                ) as ws:
                    self.connected = True
                    self._backoff = 1.0
                    self.last_error = None
                    log.info("%s feed connected", self.name)
                    for message in self.subscribe_messages():
                        await ws.send(json.dumps(message))
                    while not self._stop.is_set():
                        raw = await asyncio.wait_for(ws.recv(), timeout=self.idle_timeout_seconds)
                        self.handle_raw(raw)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.connected = False
                self.last_error = str(exc) or exc.__class__.__name__
                log.warning("%s feed disconnected: %s", self.name, self.last_error)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self._backoff)
                except asyncio.TimeoutError:
                    pass
                self._backoff = min(self._backoff * 2, 30.0)
        self.connected = False


class BinanceFeed(WebsocketBtcFeed):
    name = "binance"
    default_url = BINANCE_URL

    def parse(self, message: Any, recv_ts: datetime) -> BtcTick | None:
        return parse_binance(message, recv_ts)


class CoinbaseFeed(WebsocketBtcFeed):
    name = "coinbase"
    default_url = COINBASE_URL

    def parse(self, message: Any, recv_ts: datetime) -> BtcTick | None:
        return parse_coinbase(message, recv_ts)

    def subscribe_messages(self) -> list[dict[str, Any]]:
        return [{"type": "subscribe", "product_ids": [COINBASE_PRODUCT], "channels": ["matches"]}]


_FEEDS: dict[str, Callable[..., WebsocketBtcFeed]] = {"binance": BinanceFeed, "coinbase": CoinbaseFeed}


def feed_from_name(name: str, url: str | None = None, **kwargs: Any) -> WebsocketBtcFeed:
    try:
        factory = _FEEDS[name.lower()]
    except KeyError:
        raise ValueError(f"unknown BTC feed {name!r}; expected one of {sorted(_FEEDS)}") from None
    return factory(url, **kwargs)
