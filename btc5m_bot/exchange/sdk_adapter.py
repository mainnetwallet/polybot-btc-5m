"""Polymarket SDK adapter (polymarket-client 0.16.0).

This is the only module importing the official SDK. It conservatively maps SDK
results into the bot's own types. Anything ambiguous becomes PENDING/UNKNOWN,
never an assumed fill.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable

import httpx

from ..config import Settings
from ..types import (
    BookLevel,
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
    ExchangeError,
    ExchangeOpenOrder,
    ExchangePosition,
    ExchangeTradeFill,
    GeoResult,
    HeartbeatResult,
    NotConfiguredError,
)
from .fees import FeeSchedule, taker_fee
from .gamma import GammaClient

log = logging.getLogger(__name__)

USDC_DECIMALS = Decimal(1_000_000)
FILL_TOLERANCE = Decimal("0.000001")


def _verify_wallet_identity(client: Any, settings: Settings) -> None:
    """Fail closed unless the SDK-detected wallet type and wallet match operator config."""
    signature_by_wallet_type = {
        "EOA": 0,
        "POLY_PROXY": 1,
        "GNOSIS_SAFE": 2,
        "DEPOSIT_WALLET": 3,
    }
    wallet_type = str(getattr(client, "wallet_type", "")).upper()
    detected_signature_type = signature_by_wallet_type.get(wallet_type)
    if detected_signature_type is None:
        raise ExchangeError(f"SDK returned an unknown wallet type: {wallet_type or 'missing'}")
    if detected_signature_type != int(settings.signature_type):
        raise ExchangeError(
            f"configured signature_type={settings.signature_type} does not match "
            f"SDK-detected {wallet_type} signature type {detected_signature_type}"
        )
    actual_wallet = str(getattr(client, "wallet", "")).strip()
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", actual_wallet):
        raise ExchangeError("SDK did not expose a valid authenticated wallet address")
    if settings.funder_address and actual_wallet.lower() != settings.funder_address.strip().lower():
        raise ExchangeError("SDK-authenticated wallet does not match configured funder_address")


def _to_decimal(value: Any, default: str = "0") -> Decimal:
    try:
        if value is None or value == "":
            return Decimal(default)
        return Decimal(str(value))
    except Exception:
        return Decimal(default)


def _raw_units(value: Any) -> Decimal:
    """Convert SDK integer base units to decimal collateral; tolerate decimal inputs."""
    if value is None:
        return Decimal(0)
    if isinstance(value, bool):
        return Decimal(0)
    if isinstance(value, int):
        return Decimal(value) / USDC_DECIMALS
    dec = _to_decimal(value)
    if dec > Decimal("1000000"):
        return dec / USDC_DECIMALS
    return dec


def map_accepted_order(
    intent: OrderIntent,
    result: Any,
    schedule: FeeSchedule,
    *,
    latency_ms: int = 0,
) -> OrderResult:
    """Map an accepted CLOB order into verified fill quantities.

    For BUY, the maker asset is collateral and the taker asset is shares; for SELL it is
    the reverse. ``matched`` is the only status that proves a fill at placement.
    ``delayed``/``live`` remain unresolved and must be reconciled, never assumed.
    """
    raw_status = str(getattr(result, "status", "")).lower()
    making = _to_decimal(getattr(result, "making_amount", None))
    taking = _to_decimal(getattr(result, "taking_amount", None))
    transactions = tuple(str(t) for t in (getattr(result, "transactions_hashes", ()) or ()))
    common = {
        "client_order_id": intent.client_order_id,
        "exchange_order_id": str(getattr(result, "order_id", "") or ""),
        "latency_ms": latency_ms,
        "raw_status": raw_status,
        "transactions": transactions,
    }
    if raw_status == "matched":
        if intent.side is Side.BUY:
            filled, quote = taking, making
        else:
            filled, quote = making, taking
        if filled > 0 and quote > 0:
            avg = quote / filled
            fee = taker_fee(filled, avg, schedule)
            status = OrderStatus.FILLED if filled >= intent.shares - FILL_TOLERANCE else OrderStatus.PARTIAL
            return OrderResult(
                **common,
                status=status,
                filled_shares=filled,
                avg_price=avg,
                fee_usd=fee,
            )
        # A "matched" response without amounts is not evidence of a fill.
        return OrderResult(**common, status=OrderStatus.PENDING)
    if raw_status == "live":
        return OrderResult(**common, status=OrderStatus.ACKED)
    return OrderResult(**common, status=OrderStatus.PENDING)


class PolymarketAdapter(ExchangeAdapter):
    name = "polymarket-sdk"
    supports_trading = True

    def __init__(
        self,
        settings: Settings,
        *,
        gamma: GammaClient | None = None,
        secure_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.settings = settings
        self._gamma = gamma or GammaClient(settings.gamma_url)
        self._public: Any = None
        self._secure: Any = None
        self._http = httpx.AsyncClient(timeout=20.0, headers={"User-Agent": "POLYBOT/1.0"})
        self._secure_factory = secure_factory
        self._connect_lock = asyncio.Lock()

    @property
    def configured(self) -> bool:
        return bool(self.settings.private_key and self.settings.private_key.get_secret_value())

    async def _public_client(self) -> Any:
        if self._public is None:
            from polymarket import AsyncPublicClient

            self._public = AsyncPublicClient()
        return self._public

    async def _secure_client(self) -> Any:
        if not self.configured:
            raise NotConfiguredError("POLYBOT_PRIVATE_KEY is not configured")
        async with self._connect_lock:
            if self._secure is None:
                if self._secure_factory is not None:
                    self._secure = self._secure_factory(self.settings)
                    if asyncio.iscoroutine(self._secure):
                        self._secure = await self._secure
                else:
                    from polymarket import AsyncSecureClient

                    kwargs: dict[str, Any] = {"private_key": self.settings.private_key.get_secret_value()}
                    if self.settings.funder_address:
                        kwargs["wallet"] = self.settings.funder_address
                    self._secure = await AsyncSecureClient.create(**kwargs)
            try:
                _verify_wallet_identity(self._secure, self.settings)
            except Exception:
                try:
                    await self._secure.close()
                finally:
                    self._secure = None
                raise
        return self._secure

    async def discover_markets(self, now: datetime) -> list[MarketInfo]:
        return await self._gamma.discover(now)

    async def get_order_book(self, asset_id: str) -> OrderBookSnapshot:
        client = await self._public_client()
        try:
            book = await client.get_order_book(token_id=asset_id)
        except Exception as exc:
            raise ExchangeError(f"order book fetch failed: {exc}") from exc
        if not book.timestamp:
            # Substituting the local clock would make every book look fresh and defeat the
            # max_book_age_ms check. Without a provider timestamp the age is unknown: refuse it.
            raise ExchangeError("order book response has no timestamp; age cannot be verified")
        return OrderBookSnapshot(
            asset_id=str(book.asset_id),
            timestamp=as_utc(book.timestamp),
            bids=tuple(BookLevel(price=_to_decimal(l.price), size=_to_decimal(l.size)) for l in book.bids),
            asks=tuple(BookLevel(price=_to_decimal(l.price), size=_to_decimal(l.size)) for l in book.asks),
            tick_size=_to_decimal(book.tick_size, "0.01"),
            min_order_size=_to_decimal(book.min_order_size, "5"),
            sequence=str(book.hash) if book.hash else None,
            source="clob",
        )

    async def get_balance(self) -> Balance:
        client = await self._secure_client()
        try:
            result = await client.get_balance_allowance(asset_type="COLLATERAL")
        except Exception as exc:
            raise ExchangeError(f"balance fetch failed: {exc}") from exc
        balance = _raw_units(result.balance)
        allowances = [_raw_units(v) for v in (result.allowances or {}).values()]
        return Balance(
            available=balance,
            total=balance,
            allowance=min(allowances) if allowances else None,
            raw={"balance_raw": str(result.balance), "allowances": {k: str(v) for k, v in (result.allowances or {}).items()}},
        )

    async def get_positions(self) -> list[ExchangePosition]:
        client = await self._secure_client()
        out: list[ExchangePosition] = []
        try:
            paginator = client.list_positions()
            count = 0
            async for item in paginator.iter_items():
                count += 1
                if count > 500:
                    break
                size = _to_decimal(item.current_size)
                if size <= 0:
                    continue
                out.append(
                    ExchangePosition(
                        asset_id=str(item.asset_id),
                        condition_id=str(item.condition_id),
                        size=size,
                        avg_price=_to_decimal(item.avg_price),
                        current_value=_to_decimal(item.current_value),
                        redeemable=bool(item.redeemable),
                        outcome=str(item.outcome) if item.outcome else None,
                        raw={"status": str(item.status), "unrealized_pnl": str(item.unrealized_pnl)},
                    )
                )
        except Exception as exc:
            raise ExchangeError(f"positions fetch failed: {exc}") from exc
        return out

    async def place_order(self, intent: OrderIntent, market: MarketInfo) -> OrderResult:
        client = await self._secure_client()
        schedule = FeeSchedule.from_market(market)
        started = asyncio.get_running_loop().time()
        try:
            if intent.order_type.upper() in {"FOK", "FAK"}:
                kwargs: dict[str, Any] = {
                    "token_id": intent.asset_id,
                    "side": intent.side.value,
                    "order_type": intent.order_type.upper(),
                }
                if intent.side is Side.BUY:
                    # CLOB BUY market orders are denominated in collateral, not shares:
                    # ``shares`` is rejected by the SDK. Offer the maximum notional and
                    # cap the all-in spend at the planned cost (notional + fee).
                    # The operator sizes in USDC: send exactly that amount. Without it, fall back
                    # to the planning estimate (shares x limit price).
                    notional = intent.usdc_amount if intent.usdc_amount is not None else intent.shares * intent.limit_price
                    kwargs.update(
                        amount=notional,
                        max_spend=notional + taker_fee(intent.shares, intent.limit_price, schedule),
                        max_price=intent.limit_price,
                    )
                else:
                    kwargs.update(shares=intent.shares, min_price=intent.limit_price)
                result = await client.place_market_order(**kwargs)
            else:
                result = await client.place_limit_order(
                    token_id=intent.asset_id,
                    side=intent.side.value,
                    price=intent.limit_price,
                    size=intent.shares,
                    post_only=intent.order_type.upper().endswith("POST_ONLY"),
                )
            latency_ms = int((asyncio.get_running_loop().time() - started) * 1000)
            if getattr(result, "ok", False):
                return map_accepted_order(intent, result, schedule, latency_ms=latency_ms)
            return OrderResult(
                client_order_id=intent.client_order_id,
                status=OrderStatus.REJECTED,
                latency_ms=latency_ms,
                error_code=str(getattr(result, "code", "REJECTED")),
                error_message=str(getattr(result, "message", "")),
                raw_status="rejected",
            )
        except Exception as exc:
            latency_ms = int((asyncio.get_running_loop().time() - started) * 1000)
            try:
                from polymarket.errors import UserInputError
            except Exception:  # pragma: no cover - SDK absent
                UserInputError = None  # type: ignore[assignment,misc]

            if UserInputError is not None and isinstance(exc, UserInputError):
                # The SDK refused before signing/posting; nothing reached the exchange.
                return OrderResult(
                    client_order_id=intent.client_order_id,
                    status=OrderStatus.REJECTED,
                    latency_ms=latency_ms,
                    error_code="INVALID_ORDER",
                    error_message=str(exc)[:500],
                    raw_status="rejected",
                )
            # Ambiguous transport failure: caller must journal UNKNOWN and reconcile.
            return OrderResult(
                client_order_id=intent.client_order_id,
                status=OrderStatus.UNKNOWN,
                latency_ms=latency_ms,
                error_code="TRANSPORT_OR_SIGNING_ERROR",
                error_message=str(exc)[:500],
                raw_status="unknown",
            )

    async def cancel_order(self, order_id: str) -> bool:
        client = await self._secure_client()
        try:
            result = await client.cancel_order(order_id=order_id)
        except Exception as exc:
            log.warning("cancel failed for %s: %s", order_id, exc)
            return False
        return order_id in {str(x) for x in (getattr(result, "canceled", ()) or ())}

    async def cancel_all(self) -> int:
        client = await self._secure_client()
        try:
            result = await client.cancel_all()
        except Exception as exc:
            raise ExchangeError(f"cancel_all failed: {exc}") from exc
        return len(getattr(result, "canceled", ()) or ())

    async def get_open_orders(self, market_id: str | None = None) -> list[ExchangeOpenOrder]:
        client = await self._secure_client()
        out: list[ExchangeOpenOrder] = []
        try:
            paginator = client.list_open_orders(market=market_id) if market_id else client.list_open_orders()
            count = 0
            async for item in paginator.iter_items():
                count += 1
                if count > 500:
                    break
                out.append(
                    ExchangeOpenOrder(
                        order_id=str(item.id),
                        asset_id=str(item.asset_id),
                        condition_id=str(item.condition_id),
                        side=str(item.side),
                        price=_to_decimal(item.price),
                        original_size=_to_decimal(item.original_size),
                        size_matched=_to_decimal(item.size_matched),
                        status=str(item.status),
                        created_at=as_utc(item.created_at) if item.created_at else None,
                    )
                )
        except Exception as exc:
            raise ExchangeError(f"open orders fetch failed: {exc}") from exc
        return out

    async def get_order(self, order_id: str) -> ExchangeOpenOrder | None:
        client = await self._secure_client()
        try:
            item = await client.get_order(order_id=order_id)
        except Exception as exc:
            # Only an explicit 404 proves the order is unknown. A timeout or any other failure
            # says nothing about the order, so it must not look like "not found": callers
            # (recovery) treat None as evidence and an error as "cannot verify, stay blocked".
            if getattr(exc, "status", None) == 404:
                return None
            raise ExchangeError(f"order lookup failed: {exc}") from exc
        if item is None:
            return None
        return ExchangeOpenOrder(
            order_id=str(item.id),
            asset_id=str(item.asset_id),
            condition_id=str(item.condition_id),
            side=str(item.side),
            price=_to_decimal(item.price),
            original_size=_to_decimal(item.original_size),
            size_matched=_to_decimal(item.size_matched),
            status=str(item.status),
            created_at=as_utc(item.created_at) if item.created_at else None,
        )

    async def get_order_fills(
        self, *, order_id: str, asset_id: str, start: datetime, end: datetime
    ) -> list[ExchangeTradeFill]:
        """Fetch authenticated, order-id-attributed fills and preserve venue confirmation state."""
        client = await self._secure_client()
        try:
            paginator = client.list_account_trades(
                asset_id=asset_id,
                after=str(int(start.timestamp())),
                before=str(int(end.timestamp())),
                page_size=500,
            )
            fills: list[ExchangeTradeFill] = []
            seen: set[str] = set()
            async for trade in paginator.iter_items():
                if str(trade.asset_id) != asset_id:
                    continue
                trade_id = str(trade.id)
                if trade_id in seen:
                    continue
                if str(trade.taker_order_id) == order_id:
                    fills.append(ExchangeTradeFill(
                        trade_id=trade_id, order_id=order_id, asset_id=asset_id,
                        condition_id=str(trade.condition_id), side=str(trade.side),
                        trader_side=str(trade.trader_side), shares=Decimal(trade.size),
                        price=Decimal(trade.price), fee_rate_bps=Decimal(trade.fee_rate_bps),
                        status=str(trade.status), matched_at=as_utc(trade.matched_at),
                        transaction_hash=trade.transaction_hash,
                    ))
                    seen.add(trade_id)
                    continue
                for maker in trade.maker_orders:
                    if str(maker.order_id) != order_id:
                        continue
                    fills.append(ExchangeTradeFill(
                        trade_id=trade_id, order_id=order_id, asset_id=asset_id,
                        condition_id=str(trade.condition_id), side=str(maker.side),
                        trader_side="MAKER", shares=Decimal(maker.matched_amount),
                        price=Decimal(maker.price),
                        fee_rate_bps=Decimal(maker.fee_rate_bps or 0),
                        status=str(trade.status), matched_at=as_utc(trade.matched_at),
                        transaction_hash=trade.transaction_hash,
                    ))
                    seen.add(trade_id)
            if len(fills) > 1000:
                raise ExchangeError("order fill history exceeds safe reconciliation bound")
            return fills
        except Exception as exc:
            if isinstance(exc, ExchangeError):
                raise
            raise ExchangeError(f"order-level trade lookup failed: {exc}") from exc

    async def get_resolution(self, condition_id: str, outcomes: tuple[str, ...] | None = None) -> str | None:
        client = await self._secure_client() if self.configured else await self._public_client()
        try:
            resolutions = await client.get_resolutions(condition_ids=[condition_id])
        except Exception as exc:
            log.warning("resolution fetch failed: %s", exc)
            return None
        labels = tuple(outcomes or ())
        for res in resolutions or ():
            payouts = getattr(res, "payouts", None)
            if not payouts or len(payouts) < 2:
                continue
            values = [Decimal(str(p)) for p in payouts]
            high = max(values)
            if high <= 0 or values.count(high) > 1:
                # Unresolved or tied payouts: never guess the winner.
                continue
            idx = values.index(high)
            if idx >= len(labels):
                continue
            label = str(labels[idx]).strip().upper()
            if label == "UP":
                return "UP"
            if label == "DOWN":
                return "DOWN"
            return None
        return None

    async def geoblock(self) -> GeoResult:
        try:
            response = await self._http.get(self.settings.geo_url)
            data = response.json() if response.status_code == 200 else {}
            if response.status_code != 200:
                return GeoResult(blocked=None, detail=f"HTTP {response.status_code}", raw={})
            blocked = data.get("blocked")
            return GeoResult(
                blocked=blocked if isinstance(blocked, bool) else None,
                country=data.get("country"),
                region=data.get("region"),
                ip=data.get("ip"),
                detail="ok" if isinstance(blocked, bool) else "unparseable response",
                raw=data,
            )
        except Exception as exc:
            return GeoResult(blocked=None, detail=f"geo check failed: {exc}", raw={})

    async def send_heartbeat(self, heartbeat_id: str) -> HeartbeatResult:
        client = await self._secure_client()
        try:
            result = await client.send_order_heartbeat(heartbeat_id=heartbeat_id)
        except Exception as exc:
            raise ExchangeError(f"heartbeat failed: {exc}") from exc
        new_id = str(getattr(result, "heartbeat_id", "") or "")
        if not new_id:
            raise ExchangeError("heartbeat response missing heartbeat_id")
        return HeartbeatResult(heartbeat_id=new_id, armed=True, raw={"heartbeat_id": new_id})

    async def redeem(self, *, condition_id: str | None = None, position_id: str | None = None) -> str:
        """Submit redemption and return only after the SDK confirms a mined transaction."""
        if (condition_id is None) == (position_id is None):
            raise ExchangeError("provide exactly one of condition_id or position_id for redemption")
        client = await self._secure_client()
        try:
            handle = await client.redeem_positions(condition_id=condition_id, position_id=position_id)
            wait = getattr(handle, "wait", None)
            if not callable(wait):
                raise ExchangeError("SDK redemption handle has no confirmation waiter")
            outcome = await asyncio.wait_for(wait(), timeout=180.0)
            tx_hash = getattr(outcome, "transaction_hash", None) or getattr(handle, "transaction_hash", None)
            if not tx_hash:
                raise ExchangeError("redemption returned without a confirmed transaction hash")
            return str(tx_hash)
        except Exception as exc:
            if isinstance(exc, ExchangeError):
                raise
            raise ExchangeError(f"redemption failed or confirmation is unknown: {exc}") from exc

    async def server_time(self) -> datetime:
        response = await self._http.get(f"{self.settings.clob_url.rstrip('/')}/time")
        response.raise_for_status()
        return datetime.fromtimestamp(int(response.text.strip()), tz=timezone.utc)

    async def close(self) -> None:
        for obj in (self._secure, self._public):
            if obj is not None:
                try:
                    await obj.close()
                except Exception:
                    pass
        await self._gamma.close()
        await self._http.aclose()
