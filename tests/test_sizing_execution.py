from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal

import pytest

from btc5m_bot.exchange.fees import FeeSchedule
from btc5m_bot.execution.executor import ExecutionEngine
from btc5m_bot.execution.feasibility import assess_exit_feasibility
from btc5m_bot.execution.journal import OrderJournal
from btc5m_bot.execution.paper import PaperBroker
from btc5m_bot.sizing import size_position
from btc5m_bot.types import Mode, OrderIntent, OrderStatus, Outcome, ReasonCode, Side

from conftest import make_book, make_market

SCHEDULE = FeeSchedule(rate=Decimal("0.07"), exponent=1, enabled=True)


def _size(target, *, book=None, balance="1000", min_notional="1.00", **kw):
    return size_position(
        trade_size_usd=None if target is None else Decimal(target), balance=Decimal(balance),
        book=book or make_book("up", bid="0.49", ask="0.50", ask_size="10000"),
        schedule=SCHEDULE, min_notional_usd=Decimal(min_notional), **kw,
    )


def test_sizing_buys_exactly_the_usdc_amount():
    result = _size("10")
    assert result.allowed, result.detail
    assert Decimal("10") <= result.notional <= Decimal("10.01")
    assert result.shares == Decimal("20")  # $10 at 0.50: shares are whatever the amount buys
    assert result.order_cost == result.worst_case_loss


def test_unset_trade_size_means_no_trade():
    for missing in (None, "0"):
        result = _size(missing)
        assert not result.allowed
        assert result.reason is ReasonCode.TRADE_SIZE_NOT_SET
        assert result.shares == 0


def test_one_dollar_buys_fractional_shares_with_no_share_minimum():
    # $1 at 0.62 is 1.6129 shares, far below the old 5-share floor, and that is fine.
    result = _size("1", book=make_book("up", ask="0.62", ask_size="10000"))
    assert result.allowed, result.detail
    assert result.shares == Decimal("1.6129")
    assert Decimal("1") <= result.notional <= Decimal("1.001")


def test_below_dollar_minimum_is_refused_never_upsized():
    result = _size("0.50")
    assert not result.allowed
    assert result.reason is ReasonCode.BELOW_MIN_ORDER
    assert result.shares == 0


def test_sizing_needs_depth_for_the_full_amount():
    result = _size("20", book=make_book("up", ask="0.50", ask_size="5"))  # only $2.50 on the ask
    assert not result.allowed
    assert result.reason is ReasonCode.INSUFFICIENT_DEPTH


def test_sizing_needs_enough_balance():
    result = _size("10", balance="5")
    assert not result.allowed
    assert result.reason is ReasonCode.INSUFFICIENT_BALANCE


def test_sizing_does_not_depend_on_the_balance_size():
    small, large = _size("5", balance="300"), _size("5", balance="100000")
    assert small.allowed and large.allowed and small.notional == large.notional


def test_sizing_walks_multiple_levels():
    book = make_book("up", asks=[("0.50", "5"), ("0.51", "50")], bid="0.49")
    result = _size("10", book=book)
    assert result.allowed
    assert result.avg_price > Decimal("0.50")
    assert Decimal("10") <= result.notional <= Decimal("10.01")


def test_sizing_requires_balance_and_book():
    book = make_book("up")
    assert _size("5", book=book).allowed
    assert size_position(
        trade_size_usd=Decimal("5"), balance=None, book=book, schedule=SCHEDULE,
    ).reason is ReasonCode.INSUFFICIENT_BALANCE
    assert size_position(
        trade_size_usd=Decimal("5"), balance=Decimal("100"), book=None, schedule=SCHEDULE,
    ).reason is ReasonCode.INSUFFICIENT_DEPTH


def test_exit_feasibility_requires_full_route_by_default():
    book = make_book("up", bid="0.40", bid_size="10")
    feasible = assess_exit_feasibility(book, Decimal("10"), require_full_exit=True)
    assert feasible.allowed and feasible.can_full_exit
    thin = assess_exit_feasibility(book, Decimal("100"), require_full_exit=True)
    assert not thin.allowed
    assert "only 10/100" in thin.reasons[-2] or any("only 10" in r for r in thin.reasons)


def test_exit_feasibility_has_no_share_minimum():
    # Exits are market sells of every share held: a 1.37-share position is just as sellable.
    book = make_book("up", bid="0.40", bid_size="4")
    assert assess_exit_feasibility(book, Decimal("1.37")).allowed
    assert assess_exit_feasibility(None, Decimal("10")).allowed is False


def test_paper_broker_entry_fill_fees_and_duplicate_guard(clock, repo):
    broker = PaperBroker(
        Decimal("1000"), clock, miss_probability=0.0, partial_probability=0.0,
        maker_fill_probability=0.0, seed=1,
    )
    market = make_market()
    book = make_book(market.asset_id(Outcome.UP), ask="0.50", ask_size="1000")
    journal = OrderJournal(repo)
    intent = journal.new_intent(
        market=market, outcome=Outcome.UP, side=Side.BUY, shares=Decimal("10"),
        limit_price=Decimal("0.51"), order_type="FOK", mode="PAPER", usdc_amount=Decimal("5"),
    )
    result = asyncio.run(broker.place_order(intent, market, book))
    assert result.status is OrderStatus.FILLED
    assert result.filled_shares == Decimal("10")
    assert broker.cash < Decimal("1000")
    assert broker.position_for(market.market_id) is not None
    duplicate = journal.new_intent(
        market=market, outcome=Outcome.UP, side=Side.BUY, shares=Decimal("10"),
        limit_price=Decimal("0.51"), order_type="FOK", mode="PAPER", usdc_amount=Decimal("5"),
    )
    rejected = asyncio.run(broker.place_order(duplicate, market, book))
    assert rejected.status is OrderStatus.REJECTED
    assert rejected.error_code == "DUPLICATE_ENTRY"


def test_paper_buy_is_denominated_in_usdc_and_never_partial(clock, repo):
    # partial_probability=1.0 would shrink a share-based FOK buy; a USDC buy must ignore it.
    broker = PaperBroker(
        Decimal("1000"), clock, miss_probability=0.0, partial_probability=1.0,
        maker_fill_probability=0.0, seed=3,
    )
    market = make_market()
    book = make_book(market.asset_id(Outcome.UP), ask="0.62", ask_size="1000")
    intent = OrderJournal(repo).new_intent(
        market=market, outcome=Outcome.UP, side=Side.BUY, shares=Decimal("2"),
        limit_price=Decimal("0.63"), order_type="FOK", mode="PAPER", usdc_amount=Decimal("1"),
    )
    result = asyncio.run(broker.place_order(intent, market, book))
    assert result.status is OrderStatus.FILLED
    assert result.filled_shares == Decimal("1.6129")  # 1 USDC / 0.62, not the 2 planned shares
    assert abs(result.filled_shares * result.avg_price - Decimal("1")) < Decimal("0.001")


def test_paper_broker_restores_cash_and_open_position_from_ledger(clock, repo):
    market = make_market()
    asset_id = market.asset_id(Outcome.UP)
    repo.create_position(
        market_id=market.market_id,
        condition_id=market.condition_id,
        asset_id=asset_id,
        outcome="UP",
        shares=Decimal("10"),
        avg_price=Decimal("0.50"),
        entry_fee=Decimal("0.10"),
        mode="PAPER",
    )
    repo.add_trade(
        market_id=market.market_id,
        client_order_id="paper-entry-restore",
        side="BUY",
        outcome="UP",
        price=Decimal("0.50"),
        shares=Decimal("10"),
        fee=Decimal("0.10"),
        kind="ENTRY",
        mode="PAPER",
    )
    broker = PaperBroker.restore_from_repository(Decimal("100"), clock, repo, miss_probability=0)
    assert broker.balance == Decimal("94.90")
    restored = broker.position_for(market.market_id)
    assert restored is not None
    assert restored.shares == Decimal("10")
    assert restored.cost_basis == Decimal("5.10")
    assert broker.has_history


def test_paper_broker_sell_and_settlement(clock):
    broker = PaperBroker(Decimal("1000"), clock, miss_probability=0.0, partial_probability=0.0, seed=2)
    market = make_market()
    up = market.asset_id(Outcome.UP)
    book = make_book(up, bid="0.49", ask="0.50", bid_size="1000", ask_size="1000")
    buy = OrderIntent(
        client_order_id=uuid.uuid4().hex, idempotency_key=uuid.uuid4().hex,
        market_id=market.market_id, condition_id=market.condition_id, asset_id=up,
        outcome=Outcome.UP, side=Side.BUY, shares=Decimal("10"), limit_price=Decimal("0.51"),
        usdc_amount=Decimal("5"),
    )
    assert asyncio.run(broker.place_order(buy, market, book)).status is OrderStatus.FILLED
    cost = broker.positions[up].cost_basis
    sell = OrderIntent(
        client_order_id=uuid.uuid4().hex, idempotency_key=uuid.uuid4().hex,
        market_id=market.market_id, condition_id=market.condition_id, asset_id=up,
        outcome=Outcome.UP, side=Side.SELL, shares=Decimal("4"), limit_price=Decimal("0.48"),
    )
    sold = asyncio.run(broker.place_order(sell, market, book))
    assert sold.status is OrderStatus.FILLED
    pos = broker.positions[up]
    assert pos.shares == Decimal("6")
    assert pos.cost_basis < cost
    settlements = broker.settle(market, "DOWN")
    assert len(settlements) == 1
    assert settlements[0].payout == 0
    assert broker.positions == {}
    assert broker.cash < Decimal("1000")


def test_execution_journal_prevents_duplicate_entries(clock, repo, adapter):
    journal = OrderJournal(repo)
    engine = ExecutionEngine(
        repo=repo, journal=journal, adapter=adapter, paper=None, clock=clock,
        mode_provider=lambda: Mode.LIVE,
    )
    market = make_market()
    book = make_book(market.asset_id(Outcome.UP))
    adapter.set_book(market.asset_id(Outcome.UP), bids=[("0.49", "1000")], asks=[("0.50", "1000")])
    intent = journal.new_intent(
        market=market, outcome=Outcome.UP, side=Side.BUY, shares=Decimal("10"),
        limit_price=Decimal("0.51"), mode="LIVE", usdc_amount=Decimal("5"),
    )
    first = asyncio.run(engine.execute_entry(intent, market, book))
    assert first.result.status is OrderStatus.FILLED
    assert repo.get_position(market.market_id, "LIVE") is not None
    second = journal.new_intent(
        market=market, outcome=Outcome.UP, side=Side.BUY, shares=Decimal("10"),
        limit_price=Decimal("0.51"), mode="LIVE", usdc_amount=Decimal("5"),
    )
    duplicate = asyncio.run(engine.execute_entry(second, market, book))
    assert duplicate.result.status is OrderStatus.REJECTED
    assert duplicate.result.error_code == "DUPLICATE_ORDER"


def test_unknown_order_requires_recovery(clock, repo, adapter):
    journal = OrderJournal(repo)
    engine = ExecutionEngine(
        repo=repo, journal=journal, adapter=adapter, paper=None, clock=clock,
        mode_provider=lambda: Mode.LIVE,
    )
    market = make_market()
    adapter.unknown_place = True
    intent = journal.new_intent(
        market=market, outcome=Outcome.UP, side=Side.BUY, shares=Decimal("5"),
        limit_price=Decimal("0.51"), mode="LIVE", usdc_amount=Decimal("2.5"),
    )
    outcome = asyncio.run(engine.execute_entry(intent, market, make_book(market.asset_id(Outcome.UP))))
    assert outcome.result.status is OrderStatus.UNKNOWN
    assert repo.get_state("recovery_required")["client_order_id"] == intent.client_order_id
    assert repo.get_order(intent.client_order_id).status == OrderStatus.UNKNOWN.value


def test_cancel_is_never_assumed(clock, repo, adapter):
    journal = OrderJournal(repo)
    engine = ExecutionEngine(
        repo=repo, journal=journal, adapter=adapter, paper=None, clock=clock,
        mode_provider=lambda: Mode.LIVE,
    )
    market = make_market()
    book = make_book(market.asset_id(Outcome.UP))
    adapter.set_book(market.asset_id(Outcome.UP), bids=[("0.49", "1000")], asks=[("0.50", "1000")])
    intent = journal.new_intent(
        market=market, outcome=Outcome.UP, side=Side.BUY, shares=Decimal("5"),
        limit_price=Decimal("0.51"), mode="LIVE", usdc_amount=Decimal("2.5"),
    )
    asyncio.run(engine.execute_entry(intent, market, book))
    assert asyncio.run(engine.cancel_verified(intent.client_order_id, None)) is False
    assert repo.get_order(intent.client_order_id).status == OrderStatus.UNKNOWN.value
