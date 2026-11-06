"""Order lookups and book timestamps must fail closed, never read as "nothing there"."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from polymarket.errors import RequestRejectedError

from btc5m_bot.exchange.base import ExchangeError, ExchangeOpenOrder
from btc5m_bot.exchange.sdk_adapter import PolymarketAdapter
from btc5m_bot.execution.journal import OrderJournal
from btc5m_bot.recovery import RecoveryManager
from btc5m_bot.types import Mode, OrderStatus, Outcome, Side

from conftest import make_market


def _adapter(settings, *, secure=None, public=None) -> PolymarketAdapter:
    adapter = PolymarketAdapter(settings)

    async def _secure():
        return secure

    async def _public():
        return public

    adapter._secure_client = _secure
    adapter._public_client = _public
    return adapter


def _run(adapter, coro):
    try:
        return asyncio.run(coro)
    finally:
        asyncio.run(adapter.close())


class _SecureLookup:
    def __init__(self, *, item=None, exc: Exception | None = None) -> None:
        self.item, self.exc = item, exc

    async def get_order(self, *, order_id: str):
        if self.exc is not None:
            raise self.exc
        return self.item


def _sdk_order():
    return SimpleNamespace(
        id="ex-1", asset_id="up-token", condition_id="cond-mkt-1", side="BUY",
        price="0.5", original_size="10", size_matched="0", status="live", created_at=None,
    )


def test_get_order_returns_the_order_when_found(settings):
    adapter = _adapter(settings, secure=_SecureLookup(item=_sdk_order()))
    order = _run(adapter, adapter.get_order("ex-1"))
    assert order is not None and order.order_id == "ex-1" and order.size_matched == Decimal(0)


def test_get_order_maps_only_an_explicit_404_to_not_found(settings):
    exc = RequestRejectedError("order not found", status=404)
    adapter = _adapter(settings, secure=_SecureLookup(exc=exc))
    assert _run(adapter, adapter.get_order("ex-1")) is None


@pytest.mark.parametrize(
    "exc", [TimeoutError("read timed out"), RuntimeError("boom"), RequestRejectedError("server error", status=500)]
)
def test_get_order_failures_are_errors_not_not_found(settings, exc):
    adapter = _adapter(settings, secure=_SecureLookup(exc=exc))
    with pytest.raises(ExchangeError):
        _run(adapter, adapter.get_order("ex-1"))


def test_recovery_blocks_when_the_post_cancel_lookup_fails(clock, repo, adapter):
    market = make_market()
    journal = OrderJournal(repo)
    intent = journal.new_intent(
        market=market, outcome=Outcome.UP, side=Side.BUY, shares=Decimal("10"),
        limit_price=Decimal("0.50"), mode="LIVE",
    )
    journal.persist_intent(intent, reserve_amount=Decimal("5"))
    journal.mark_submitting(intent)
    repo.update_order(intent.client_order_id, exchange_order_id="ex-1", status=OrderStatus.ACKED.value)
    adapter.open_orders["ex-1"] = ExchangeOpenOrder(
        order_id="ex-1", asset_id="up-token", condition_id="cond-mkt-1", side="BUY",
        price=Decimal("0.50"), original_size=Decimal("10"), size_matched=Decimal("0"), status="live",
    )

    async def lookup_fails(order_id: str):
        raise ExchangeError("order lookup failed: timeout")

    adapter.get_order = lookup_fails
    result = asyncio.run(RecoveryManager(repo=repo, adapter=adapter, clock=clock, mode=Mode.LIVE).run())

    assert result.ready is False
    assert any("could not be verified" in b for b in result.blocks)
    assert repo.get_order(intent.client_order_id).status != OrderStatus.CANCELLED.value


def _sdk_book(timestamp):
    return SimpleNamespace(
        asset_id="up-token", timestamp=timestamp, bids=[], asks=[],
        tick_size="0.01", min_order_size="5", hash="h1",
    )


class _PublicBook:
    def __init__(self, book) -> None:
        self.book = book

    async def get_order_book(self, *, token_id: str):
        return self.book


def test_order_book_without_a_timestamp_is_refused_not_stamped_with_now(settings):
    adapter = _adapter(settings, public=_PublicBook(_sdk_book(None)))
    with pytest.raises(ExchangeError, match="timestamp"):
        _run(adapter, adapter.get_order_book("up-token"))


def test_order_book_keeps_the_provider_timestamp(settings):
    stamp = datetime(2026, 11, 5, 12, 0, 0, tzinfo=timezone.utc)
    adapter = _adapter(settings, public=_PublicBook(_sdk_book(stamp)))
    book = _run(adapter, adapter.get_order_book("up-token"))
    assert book.timestamp == stamp
