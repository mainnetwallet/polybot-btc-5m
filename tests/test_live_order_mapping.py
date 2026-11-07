"""Live-order result mapping: matched fills, ambiguous states and reservations."""
from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace

import pytest

from btc5m_bot.exchange.fees import FeeSchedule, taker_fee
from btc5m_bot.exchange.sdk_adapter import PolymarketAdapter, map_accepted_order
from btc5m_bot.execution.executor import ExecutionEngine
from btc5m_bot.execution.journal import OrderJournal
from btc5m_bot.types import Mode, OrderResult, OrderStatus, Outcome, Side

from conftest import make_market

SCHEDULE = FeeSchedule(rate=Decimal("0.07"), exponent=1, enabled=True)


def _intent(journal: OrderJournal, market, *, side: Side, shares: str = "10", order_type: str = "FOK"):
    return journal.new_intent(
        market=market,
        outcome=Outcome.UP,
        side=side,
        shares=Decimal(shares),
        limit_price=Decimal("0.51"),
        order_type=order_type,
        mode="LIVE",
        usdc_amount=Decimal(shares) * Decimal("0.51") if side is Side.BUY else None,
    )


def _accepted(status: str, making: str, taking: str):
    return SimpleNamespace(
        ok=True,
        order_id="ord-1",
        status=status,
        making_amount=Decimal(making),
        taking_amount=Decimal(taking),
        trade_ids=(),
        transactions_hashes=(),
    )


def test_matched_buy_maps_collateral_and_shares(repo):
    market = make_market()
    journal = OrderJournal(repo)
    intent = _intent(journal, market, side=Side.BUY)

    result = map_accepted_order(intent, _accepted("matched", "5", "10"), SCHEDULE)

    assert result.status is OrderStatus.FILLED
    assert result.filled_shares == Decimal("10")
    assert result.avg_price == Decimal("0.5")
    assert result.fee_usd == taker_fee(Decimal("10"), Decimal("0.5"), SCHEDULE)
    assert result.exchange_order_id == "ord-1"


def test_matched_sell_swaps_maker_and_taker_assets(repo):
    market = make_market()
    journal = OrderJournal(repo)
    intent = _intent(journal, market, side=Side.SELL)

    result = map_accepted_order(intent, _accepted("matched", "10", "4.9"), SCHEDULE)

    assert result.status is OrderStatus.FILLED
    assert result.filled_shares == Decimal("10")
    assert result.avg_price == Decimal("0.49")


def test_matched_below_requested_size_is_partial(repo):
    market = make_market()
    journal = OrderJournal(repo)
    intent = _intent(journal, market, side=Side.BUY)

    result = map_accepted_order(intent, _accepted("matched", "3", "6"), SCHEDULE)

    assert result.status is OrderStatus.PARTIAL
    assert result.filled_shares == Decimal("6")


def test_unproven_states_never_claim_a_fill(repo):
    market = make_market()
    journal = OrderJournal(repo)
    intent = _intent(journal, market, side=Side.BUY)

    live = map_accepted_order(intent, _accepted("live", "5", "10"), SCHEDULE)
    delayed = map_accepted_order(intent, _accepted("delayed", "5", "10"), SCHEDULE)
    empty = map_accepted_order(intent, _accepted("matched", "0", "0"), SCHEDULE)

    assert live.status is OrderStatus.ACKED and live.filled_shares == 0
    assert delayed.status is OrderStatus.PENDING and delayed.filled_shares == 0
    assert empty.status is OrderStatus.PENDING and empty.filled_shares == 0


class _StubSecure:
    def __init__(self, result=None, exc: Exception | None = None) -> None:
        self.result = result
        self.exc = exc
        self.calls: list[dict] = []

    async def place_market_order(self, **kwargs):
        self.calls.append(kwargs)
        if self.exc is not None:
            raise self.exc
        return self.result


def _adapter_with(settings, stub):
    adapter = PolymarketAdapter(settings)

    async def secure():
        return stub

    adapter._secure_client = secure
    return adapter


def test_adapter_place_order_returns_real_fill(settings, repo):
    market = make_market()
    journal = OrderJournal(repo)
    intent = _intent(journal, market, side=Side.BUY)
    stub = _StubSecure(_accepted("matched", "5", "10"))
    adapter = _adapter_with(settings, stub)
    try:
        result = asyncio.run(adapter.place_order(intent, market))
    finally:
        asyncio.run(adapter.close())

    assert result.status is OrderStatus.FILLED
    assert result.filled_shares == Decimal("10")
    call = stub.calls[0]
    assert call["order_type"] == "FOK"
    assert call["max_price"] == Decimal("0.51")
    # BUY market orders are collateral-denominated; the SDK rejects ``shares``.
    assert call["amount"] == Decimal("5.10")
    assert call["max_spend"] == Decimal("5.10") + taker_fee(Decimal("10"), Decimal("0.51"), SCHEDULE)
    assert "shares" not in call


def test_adapter_buy_sends_the_exact_usdc_amount_when_set(settings, repo):
    market = make_market()
    journal = OrderJournal(repo)
    intent = journal.new_intent(
        market=market,
        outcome=Outcome.UP,
        side=Side.BUY,
        shares=Decimal("10"),
        limit_price=Decimal("0.51"),
        order_type="FOK",
        mode="LIVE",
        usdc_amount=Decimal("5"),
    )
    stub = _StubSecure(_accepted("matched", "9.8", "5"))
    adapter = _adapter_with(settings, stub)
    try:
        asyncio.run(adapter.place_order(intent, market))
    finally:
        asyncio.run(adapter.close())

    call = stub.calls[0]
    # The order is sized in USDC (5.00), not shares x the max price (5.10).
    assert call["amount"] == Decimal("5")
    assert call["max_spend"] == Decimal("5") + taker_fee(Decimal("10"), Decimal("0.51"), SCHEDULE)
    assert "shares" not in call


def test_adapter_sell_uses_shares_and_min_price(settings, repo):
    market = make_market()
    journal = OrderJournal(repo)
    intent = _intent(journal, market, side=Side.SELL, shares="6")
    stub = _StubSecure(_accepted("matched", "6", "2.94"))
    adapter = _adapter_with(settings, stub)
    try:
        result = asyncio.run(adapter.place_order(intent, market))
    finally:
        asyncio.run(adapter.close())

    assert result.status is OrderStatus.FILLED
    assert result.filled_shares == Decimal("6")
    call = stub.calls[0]
    assert call["shares"] == Decimal("6")
    assert call["min_price"] == Decimal("0.51")
    assert "amount" not in call


def test_adapter_validation_failure_is_rejected_not_unknown(settings, repo):
    from polymarket.errors import UserInputError

    market = make_market()
    journal = OrderJournal(repo)
    intent = _intent(journal, market, side=Side.BUY)
    adapter = _adapter_with(settings, _StubSecure(exc=UserInputError("amount too small")))
    try:
        result = asyncio.run(adapter.place_order(intent, market))
    finally:
        asyncio.run(adapter.close())

    assert result.status is OrderStatus.REJECTED
    assert result.error_code == "INVALID_ORDER"


class _StubPublic:
    def __init__(self, resolutions) -> None:
        self.resolutions = resolutions

    async def get_resolutions(self, condition_ids):
        return self.resolutions


def _adapter_with_public(settings, stub):
    adapter = PolymarketAdapter(settings)

    async def public():
        return stub

    adapter._public_client = public
    return adapter


def test_resolution_maps_payouts_through_outcome_order(settings):
    up_down = SimpleNamespace(payouts=[0, 1])
    adapter = _adapter_with_public(settings, _StubPublic([up_down]))
    try:
        assert asyncio.run(adapter.get_resolution("cond-1", ("Up", "Down"))) == "DOWN"
    finally:
        asyncio.run(adapter.close())

    flipped = SimpleNamespace(payouts=[1, 0])
    adapter = _adapter_with_public(settings, _StubPublic([flipped]))
    try:
        assert asyncio.run(adapter.get_resolution("cond-1", ("Down", "Up"))) == "DOWN"
    finally:
        asyncio.run(adapter.close())


def test_unresolved_or_tied_payouts_are_not_guessed(settings):
    for payouts in ([0, 0], [1, 1], []):
        adapter = _adapter_with_public(settings, _StubPublic([SimpleNamespace(payouts=payouts)]))
        try:
            assert asyncio.run(adapter.get_resolution("cond-1", ("Up", "Down"))) is None
        finally:
            asyncio.run(adapter.close())


def test_adapter_rejection_is_reported(settings, repo):
    market = make_market()
    journal = OrderJournal(repo)
    intent = _intent(journal, market, side=Side.BUY)
    stub = _StubSecure(SimpleNamespace(ok=False, code="fok_not_filled", message="killed"))
    adapter = _adapter_with(settings, stub)
    try:
        result = asyncio.run(adapter.place_order(intent, market))
    finally:
        asyncio.run(adapter.close())

    assert result.status is OrderStatus.REJECTED
    assert result.error_code == "fok_not_filled"


def test_adapter_transport_failure_is_unknown(settings, repo):
    market = make_market()
    journal = OrderJournal(repo)
    intent = _intent(journal, market, side=Side.BUY)
    adapter = _adapter_with(settings, _StubSecure(exc=RuntimeError("socket reset")))
    try:
        result = asyncio.run(adapter.place_order(intent, market))
    finally:
        asyncio.run(adapter.close())

    assert result.status is OrderStatus.UNKNOWN
    assert result.error_code == "TRANSPORT_OR_SIGNING_ERROR"


def test_fok_partial_releases_reservation(clock, repo, adapter):
    market = make_market()
    journal = OrderJournal(repo)
    intent = _intent(journal, market, side=Side.BUY)
    journal.persist_intent(intent, reserve_amount=Decimal("5"))
    assert repo.active_reserved_total() == Decimal("5")

    journal.mark_result(
        intent,
        OrderResult(
            client_order_id=intent.client_order_id,
            status=OrderStatus.PARTIAL,
            filled_shares=Decimal("4"),
            avg_price=Decimal("0.5"),
            fee_usd=Decimal("0.1"),
        ),
    )

    assert repo.active_reserved_total() == 0
    assert repo.get_order(intent.client_order_id).filled_shares == Decimal("4")


def test_resting_partial_keeps_reservation(clock, repo, adapter):
    market = make_market()
    journal = OrderJournal(repo)
    intent = _intent(journal, market, side=Side.BUY, order_type="GTC")
    journal.persist_intent(intent, reserve_amount=Decimal("5"))

    journal.mark_result(
        intent,
        OrderResult(
            client_order_id=intent.client_order_id,
            status=OrderStatus.PARTIAL,
            filled_shares=Decimal("4"),
            avg_price=Decimal("0.5"),
        ),
    )

    assert repo.active_reserved_total() == Decimal("5")


def test_terminal_market_partial_is_not_a_working_order(repo):
    market = make_market()
    journal = OrderJournal(repo)
    terminal = _intent(journal, market, side=Side.BUY, order_type="FOK")
    repo.create_order_intent(terminal, status=OrderStatus.PARTIAL)
    resting = _intent(journal, market, side=Side.BUY, order_type="GTC")
    repo.create_order_intent(resting, status=OrderStatus.PARTIAL)

    working = {order.client_order_id for order in repo.open_orders()}

    assert terminal.client_order_id not in working
    assert resting.client_order_id in working
