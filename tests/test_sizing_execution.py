from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal

import pytest

from btc5m_bot.exchange.fees import FeeSchedule
from btc5m_bot.execution.executor import ExecutionEngine
from btc5m_bot.execution.feasibility import assess_dust, assess_exit_feasibility
from btc5m_bot.execution.journal import OrderJournal
from btc5m_bot.execution.paper import PaperBroker
from btc5m_bot.sizing import capital_compatibility, size_position
from btc5m_bot.types import Mode, OrderIntent, OrderStatus, Outcome, ReasonCode, Side

from conftest import make_book, make_market

SCHEDULE = FeeSchedule(rate=Decimal("0.07"), exponent=1, enabled=True)


def test_sizing_stays_within_two_percent():
    book = make_book("up", bid="0.49", ask="0.50", ask_size="10000")
    result = size_position(
        capital=Decimal("1000"), risk_pct=Decimal("0.02"), balance=Decimal("1000"),
        book=book, min_order_size=Decimal("5"), schedule=SCHEDULE,
    )
    assert result.allowed
    assert result.worst_case_loss <= Decimal("20")
    assert result.capital_required <= Decimal("20")
    assert result.shares >= Decimal("5")


def test_sizing_never_raises_risk_for_exchange_minimum():
    # 2% of 20 is 0.40: even one minimum-size order costs more than that.
    book = make_book("up", ask="0.50")
    result = size_position(
        capital=Decimal("20"), risk_pct=Decimal("0.02"), balance=Decimal("20"),
        book=book, min_order_size=Decimal("5"), schedule=SCHEDULE,
    )
    assert not result.allowed
    assert result.reason is ReasonCode.BELOW_MIN_ORDER
    assert result.shares == 0
    assert "never" in result.detail or "below" in result.detail


def test_sizing_respects_available_balance_and_depth():
    book = make_book("up", ask="0.50", ask_size="8")
    balance_limited = size_position(
        capital=Decimal("1000"), risk_pct=Decimal("0.02"), balance=Decimal("2"),
        book=book, min_order_size=Decimal("5"), schedule=SCHEDULE,
    )
    assert not balance_limited.allowed
    depth_limited = size_position(
        capital=Decimal("1000"), risk_pct=Decimal("0.02"), balance=Decimal("1000"),
        book=book, min_order_size=Decimal("5"), schedule=SCHEDULE,
    )
    assert depth_limited.allowed
    assert depth_limited.shares <= Decimal("8")


def test_sizing_walks_multiple_levels():
    book = make_book("up", asks=[("0.50", "5"), ("0.51", "50")], bid="0.49")
    result = size_position(
        capital=Decimal("1000"), risk_pct=Decimal("0.02"), balance=Decimal("1000"),
        book=book, min_order_size=Decimal("5"), schedule=SCHEDULE,
    )
    assert result.allowed
    assert result.shares > Decimal("5")
    assert result.avg_price > Decimal("0.50")
    assert result.capital_required <= Decimal("20")


def test_sizing_requires_capital_and_balance():
    book = make_book("up")
    assert size_position(
        capital=None, risk_pct=Decimal("0.02"), balance=Decimal("100"),
        book=book, min_order_size=Decimal("5"), schedule=SCHEDULE,
    ).reason is ReasonCode.CAPITAL_UNSET
    assert size_position(
        capital=Decimal("100"), risk_pct=Decimal("0.02"), balance=None,
        book=book, min_order_size=Decimal("5"), schedule=SCHEDULE,
    ).reason is ReasonCode.INSUFFICIENT_BALANCE
    assert size_position(
        capital=Decimal("100"), risk_pct=Decimal("0.02"), balance=Decimal("100"),
        book=None, min_order_size=Decimal("5"), schedule=SCHEDULE,
    ).reason is ReasonCode.INSUFFICIENT_DEPTH


def test_capital_compatibility_reports_true_minimum():
    report = capital_compatibility(
        capital=Decimal("1000"),
        risk_pct=Decimal("0.02"),
        min_order_size=Decimal("5"),
        schedule=SCHEDULE,
    )
    assert report.minimum_capital > Decimal(100)
    row = next(r for r in report.rows if r.price == Decimal("0.50"))
    assert row.order_cost >= Decimal("5") * Decimal("0.50")
    assert float(row.min_capital) == pytest.approx(float(row.worst_case_loss / Decimal("0.02")), rel=1e-6)


def test_exit_feasibility_requires_full_route_by_default():
    book = make_book("up", bid="0.40", bid_size="10")
    feasible = assess_exit_feasibility(book, Decimal("10"), Decimal("5"), require_full_exit=True)
    assert feasible.allowed and feasible.can_full_exit
    thin = assess_exit_feasibility(book, Decimal("100"), Decimal("5"), require_full_exit=True)
    assert not thin.allowed
    assert "only 10/100" in thin.reasons[-2] or any("only 10" in r for r in thin.reasons)


def test_exit_feasibility_below_minimum_is_dust():
    book = make_book("up", bid="0.40", bid_size="4")
    result = assess_exit_feasibility(book, Decimal("4"), Decimal("5"))
    assert not result.allowed and result.dust_risk
    assert assess_dust(Decimal("4.99"), Decimal("5"))[0] is True
    assert assess_dust(Decimal("5"), Decimal("5"))[0] is False
    assert assess_exit_feasibility(None, Decimal("10"), Decimal("5")).allowed is False


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
        limit_price=Decimal("0.51"), order_type="FOK", mode="PAPER",
    )
    result = asyncio.run(broker.place_order(intent, market, book))
    assert result.status is OrderStatus.FILLED
    assert result.filled_shares == Decimal("10")
    assert broker.cash < Decimal("1000")
    assert broker.position_for(market.market_id) is not None
    duplicate = journal.new_intent(
        market=market, outcome=Outcome.UP, side=Side.BUY, shares=Decimal("10"),
        limit_price=Decimal("0.51"), order_type="FOK", mode="PAPER",
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


def test_paper_broker_sell_and_settlement(clock):
    broker = PaperBroker(Decimal("1000"), clock, miss_probability=0.0, partial_probability=0.0, seed=2)
    market = make_market()
    up = market.asset_id(Outcome.UP)
    book = make_book(up, bid="0.49", ask="0.50", bid_size="1000", ask_size="1000")
    buy = OrderIntent(
        client_order_id=uuid.uuid4().hex, idempotency_key=uuid.uuid4().hex,
        market_id=market.market_id, condition_id=market.condition_id, asset_id=up,
        outcome=Outcome.UP, side=Side.BUY, shares=Decimal("10"), limit_price=Decimal("0.51"),
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
        limit_price=Decimal("0.51"), mode="LIVE",
    )
    first = asyncio.run(engine.execute_entry(intent, market, book))
    assert first.result.status is OrderStatus.FILLED
    assert repo.get_position(market.market_id, "LIVE") is not None
    second = journal.new_intent(
        market=market, outcome=Outcome.UP, side=Side.BUY, shares=Decimal("10"),
        limit_price=Decimal("0.51"), mode="LIVE",
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
        limit_price=Decimal("0.51"), mode="LIVE",
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
        limit_price=Decimal("0.51"), mode="LIVE",
    )
    asyncio.run(engine.execute_entry(intent, market, book))
    assert asyncio.run(engine.cancel_verified(intent.client_order_id, None)) is False
    assert repo.get_order(intent.client_order_id).status == OrderStatus.UNKNOWN.value


def test_dollar_minimum_allows_sub_five_share_entry_within_risk():
    # Capital 100 -> risk budget $2.00. At $0.50 that is ~3.9 shares: below the 5-share limit-order
    # minimum but well above the $1 marketable-BUY minimum.
    book = make_book("up", bid="0.49", ask="0.50", ask_size="10000")
    legacy = size_position(
        capital=Decimal("100"), risk_pct=Decimal("0.02"), balance=Decimal("100"),
        book=book, min_order_size=Decimal("5"), schedule=SCHEDULE,
    )
    assert not legacy.allowed and legacy.reason is ReasonCode.BELOW_MIN_ORDER
    dollars = size_position(
        capital=Decimal("100"), risk_pct=Decimal("0.02"), balance=Decimal("100"),
        book=book, min_order_size=Decimal("5"), schedule=SCHEDULE,
        min_notional_usd=Decimal("1.00"),
    )
    assert dollars.allowed
    assert dollars.shares < Decimal("5")
    assert dollars.notional >= Decimal("1.00")
    assert dollars.worst_case_loss <= Decimal("2.00")  # risk is never exceeded


def test_dollar_minimum_never_raises_risk_to_reach_one_dollar():
    # Capital 20 -> risk budget $0.40, below the $1 minimum: must refuse, never size up.
    book = make_book("up", bid="0.49", ask="0.50", ask_size="10000")
    result = size_position(
        capital=Decimal("20"), risk_pct=Decimal("0.02"), balance=Decimal("20"),
        book=book, min_order_size=Decimal("5"), schedule=SCHEDULE,
        min_notional_usd=Decimal("1.00"),
    )
    assert not result.allowed
    assert result.reason is ReasonCode.BELOW_MIN_ORDER
    assert result.shares == Decimal(0)


# ---- fixed dollar trade size (trade_size_usd) ---------------------------------------------------
def _fixed(target, *, capital="1000", ask="0.50", ask_size="10000", balance="1000", min_notional="1.00"):
    return size_position(
        capital=Decimal(capital), risk_pct=Decimal("0.02"), balance=Decimal(balance),
        book=make_book("up", ask=ask, ask_size=ask_size), min_order_size=Decimal("5"),
        schedule=SCHEDULE, min_notional_usd=Decimal(min_notional), fixed_notional_usd=Decimal(target),
    )


@pytest.mark.parametrize("target", ["1", "2", "5", "10"])
def test_fixed_size_trades_the_requested_dollar_amount(target):
    result = _fixed(target)
    assert result.allowed, result.detail
    # order value sits at the target, at most one share step (~$0.01) above it
    assert Decimal(target) <= result.notional <= Decimal(target) + Decimal("0.01")


def test_fixed_size_ignores_capital_size():
    small = _fixed("2", capital="200")
    large = _fixed("2", capital="100000", balance="100000")
    assert small.allowed and large.allowed
    assert small.notional == large.notional


def test_fixed_size_has_no_minimum_capital():
    # $5 trade on a $100 capital (5%) and even a $20 capital: allowed, no RISK_LIMIT
    for capital in ("100", "20"):
        result = _fixed("5", capital=capital, balance=capital)
        assert result.allowed, result.detail
        assert Decimal("5") <= result.notional <= Decimal("5.01")


def test_fixed_size_below_exchange_minimum_is_refused():
    result = _fixed("0.50")
    assert not result.allowed
    assert result.reason is ReasonCode.BELOW_MIN_ORDER


def test_fixed_size_never_shrinks_when_book_is_thin():
    result = _fixed("20", ask_size="5")  # $20 at 0.50 needs 40 shares, only 5 on the ask
    assert not result.allowed
    assert result.reason is ReasonCode.INSUFFICIENT_DEPTH


def test_fixed_size_needs_enough_balance():
    result = _fixed("10", balance="5")
    assert not result.allowed
    assert result.reason is ReasonCode.INSUFFICIENT_BALANCE


def test_fixed_size_none_keeps_percent_sizing():
    book = make_book("up", ask="0.50", ask_size="10000")
    kw = dict(capital=Decimal("1000"), risk_pct=Decimal("0.02"), balance=Decimal("1000"),
              book=book, min_order_size=Decimal("5"), schedule=SCHEDULE)
    assert size_position(**kw, fixed_notional_usd=None).shares == size_position(**kw).shares
