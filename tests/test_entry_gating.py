"""Engine-level entry gating: up to 2 open positions, up to 3 entries per market.

A second or third entry in a market is a top-up of the same side, allowed only while the position
is in profit and never after an exit. A second position may open in a new market while the first is
still open; a third may not. The real position/order ledger and the real risk caps run here. Only
the market signal, edge and book-quality checks are stubbed, so these tests are about the gating.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace

import pytest

from btc5m_bot import engine as engine_module
from btc5m_bot.clock import FixedClock
from btc5m_bot.types import OrderStatus, Outcome, ReasonCode, Side

from conftest import START, make_book
from test_engine_integration import _build

UP, DOWN = Outcome.UP, Outcome.DOWN


class _Harness:
    def __init__(self, engine, market, repo, calls):
        self.engine, self.market, self.repo, self.calls = engine, market, repo, calls
        self._orders = 0

    def position(self, market_id, outcome=UP, *, shares="10", cost="4.00", status="OPEN"):
        row = self.repo.create_position(
            market_id=market_id, condition_id="cond-" + market_id,
            asset_id=self.market.asset_id(outcome), outcome=outcome.value,
            shares=Decimal(shares), avg_price=Decimal(cost) / Decimal(shares), entry_fee=Decimal(0), mode="PAPER",
        )
        if status != "OPEN":
            self.repo.update_position(row.id, status=status)
        return row

    def buys(self, n, market_id=None, outcome=UP):
        """Record n BUY orders that were already used in the market."""
        from btc5m_bot.execution.journal import OrderJournal

        journal = OrderJournal(self.repo)
        market = self.market if market_id in (None, self.market.market_id) else SimpleNamespace()
        for _ in range(n):
            self._orders += 1  # a distinct size per order keeps each idempotency key unique
            intent = journal.new_intent(
                market=market, outcome=outcome, side=Side.BUY, shares=Decimal("10") + Decimal(self._orders) / 100,
                limit_price=Decimal("0.5"), order_type="FOK", mode="PAPER",
            )
            self.repo.create_order_intent(intent, status=OrderStatus.FILLED)

    def go(self, expects: Outcome = UP):
        """Run one entry evaluation with the signal saying `expects`; return the orders it tried to place."""
        self.engine._probability = SimpleNamespace(
            tradable=True, expected_outcome=expects, p_up=Decimal("0.7"), confidence="HIGH",
            probability=lambda outcome: Decimal("0.70"),
        )
        self.calls.clear()
        asyncio.run(self.engine._evaluate_entry(START))
        return list(self.calls)

    @property
    def reason(self):
        return self.engine._last_decision["reason"], self.engine._last_decision["detail"]


@pytest.fixture
def gate(settings, repo, adapter, config_store, monkeypatch):
    clock = FixedClock(START)
    engine, market, _ = _build(settings, repo, adapter, config_store, clock)
    config_store.update({"trade_size_usd": "3", "daily_loss_limit_usd": "100"})
    asyncio.run(engine._iteration())  # registers the market and opens its entry window

    up_book = make_book(market.asset_id(UP), bid="0.48", ask="0.50", ts=START)
    down_book = make_book(market.asset_id(DOWN), bid="0.48", ask="0.50", ts=START)
    engine._features = SimpleNamespace(
        remaining_seconds=290.0, entry_seconds_remaining=140.0, data_age_ms=100, book_age_ms=100,
        mtf_conflict=False, up_book=up_book, down_book=down_book, ts=START,
    )
    engine._regime = SimpleNamespace(entries_allowed=True)
    engine._composite = SimpleNamespace(can_enter=True, reasons=[])
    engine._balance = Decimal("100")
    engine.strategies.select = lambda *a, **k: SimpleNamespace(outcome=None, action="BUY")
    config_store.update({"strategy_mode": "SHADOW"})  # signal confirmation is not what is under test

    # Everything after the gating is a pass-through stub.
    monkeypatch.setattr(engine_module, "size_position", lambda **k: SimpleNamespace(
        allowed=True, shares=Decimal("6"), worst_case_loss=Decimal("3"), order_cost=Decimal("3"),
        reason=ReasonCode.OK, detail="", notional=Decimal("3"), avg_price=Decimal("0.5"), fee=Decimal(0),
        max_by_balance=Decimal(0), max_by_depth=Decimal(0), min_order_cost=Decimal(1)))
    monkeypatch.setattr(engine_module, "evaluate_edge", lambda **k: SimpleNamespace(
        positive=True, net_edge=Decimal("0.2"), executable_price=Decimal("0.5"), failure=""))
    monkeypatch.setattr(engine_module, "assess_exit_feasibility", lambda *a, **k: SimpleNamespace(allowed=True, reasons=[]))
    monkeypatch.setattr(engine_module, "FilterEngine", lambda **k: SimpleNamespace(
        evaluate=lambda *a, **kw: SimpleNamespace(all_pass=True, unavailable=[], summary="")))
    monkeypatch.setattr(engine, "_edge_dict", lambda edge: {})
    monkeypatch.setattr(engine, "_market_seconds_delay", lambda market: 0.0)

    calls: list = []

    async def fake_execute_entry(intent, market, book, signal_ts=None):
        calls.append(intent)
        return SimpleNamespace(result=SimpleNamespace(
            filled_shares=Decimal(0), status=OrderStatus.REJECTED, error_code="TEST", raw_status="", avg_price=Decimal(0)))

    monkeypatch.setattr(engine.execution, "execute_entry", fake_execute_entry)

    async def no_publish(now):  # the dashboard snapshot has its own tests
        return None

    monkeypatch.setattr(engine, "_publish", no_publish)
    return _Harness(engine, market, repo, calls)


def test_a_flat_market_gets_its_first_entry(gate):
    assert len(gate.go()) == 1


def test_top_up_is_allowed_while_the_position_is_in_profit(gate):
    gate.position(gate.market.market_id, UP, shares="10", cost="4.00")  # bid 0.48 x 10 = 4.80 > 4.00
    gate.buys(1)
    assert len(gate.go(expects=UP)) == 1


def test_top_up_is_refused_when_the_position_is_not_in_profit(gate):
    gate.position(gate.market.market_id, UP, shares="10", cost="6.00")  # worth 4.80 against 6.00 paid
    gate.buys(1)
    assert gate.go(expects=UP) == []
    reason, detail = gate.reason
    assert reason == ReasonCode.ENTRY_ALREADY_USED.value and "not in profit" in detail


def test_top_up_is_refused_on_the_opposite_side(gate):
    gate.position(gate.market.market_id, DOWN, shares="10", cost="4.00")
    gate.buys(1, outcome=DOWN)
    assert gate.go(expects=UP) == []
    reason, detail = gate.reason
    assert reason == ReasonCode.ENTRY_ALREADY_USED.value and "same side" in detail


def test_no_entry_after_an_exit_in_the_same_market(gate):
    gate.position(gate.market.market_id, UP, status="CLOSED")  # exited earlier in this market
    gate.buys(1)
    assert gate.go(expects=UP) == []


def test_the_third_entry_is_allowed_and_a_fourth_is_not(gate):
    gate.position(gate.market.market_id, UP, shares="10", cost="4.00")
    gate.buys(2)
    assert len(gate.go()) == 1  # 3rd entry in the market
    gate.buys(1)
    assert gate.go() == []  # 3 already used


def test_a_second_position_opens_in_a_new_market_while_the_first_is_open(gate):
    gate.position("mkt-earlier", UP)
    assert len(gate.go()) == 1


def test_a_third_position_is_refused(gate):
    gate.position("mkt-earlier-1", UP)
    gate.position("mkt-earlier-2", DOWN)
    assert gate.go() == []


def test_the_third_position_cap_follows_the_setting(gate, config_store):
    config_store.update({"max_active_positions": 1})
    gate.position("mkt-earlier", UP)
    assert gate.go() == []
