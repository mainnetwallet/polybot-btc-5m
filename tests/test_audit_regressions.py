"""Regression tests for the bugs found in the full-repo audit."""
from __future__ import annotations

import ast
import asyncio
from decimal import Decimal
from pathlib import Path

import pytest

from btc5m_bot.db.repo import DuplicateOrderError
from btc5m_bot.execution.executor import ExecutionEngine
from btc5m_bot.execution.journal import OrderJournal
from btc5m_bot.types import Mode, OrderStatus, Outcome, Side

from conftest import make_book, make_market

PKG = Path(__file__).resolve().parents[1] / "btc5m_bot"


def _sell(journal, market, shares="10", price="0.40", mode="LIVE"):
    return journal.new_intent(
        market=market, outcome=Outcome.UP, side=Side.SELL, shares=Decimal(shares),
        limit_price=Decimal(price), mode=mode,
    )


def test_failed_exit_can_be_retried_with_identical_size_and_price(clock, repo, adapter):
    """A FOK sell that does not fill must be retryable. Same shares + same price used to hash to
    the same UNIQUE idempotency key, so the retry was dropped as a 'duplicate' forever."""
    journal = OrderJournal(repo)
    execution = ExecutionEngine(
        repo=repo, journal=journal, adapter=adapter, paper=None, clock=clock,
        mode_provider=lambda: Mode.LIVE,
    )
    market = make_market()
    asset = market.asset_id(Outcome.UP)
    adapter.set_book(asset, bids=[("0.40", "1000")], asks=[("0.42", "1000")])
    position = repo.create_position(
        market_id=market.market_id, condition_id=market.condition_id, asset_id=asset,
        outcome=Outcome.UP.value, shares=Decimal("10"), avg_price=Decimal("0.50"),
        entry_fee=Decimal(0), mode="LIVE",
    )
    book = make_book(asset)

    adapter.fail_place = True
    first = asyncio.run(execution.execute_exit(_sell(journal, market), market, book, position))
    assert first.result.status is OrderStatus.REJECTED
    assert repo.get_position(market.market_id, "LIVE").status != "CLOSED"

    adapter.fail_place = False
    retry_intent = _sell(journal, market)  # exactly the same shares and price
    assert retry_intent.idempotency_key != first.result.client_order_id
    second = asyncio.run(
        execution.execute_exit(retry_intent, market, book, repo.get_position(market.market_id, "LIVE"))
    )
    assert second.result.filled_shares > 0
    assert repo.get_position(market.market_id, "LIVE").status == "CLOSED"


def test_each_exit_attempt_gets_its_own_key_but_the_same_attempt_is_still_deduplicated(clock, repo):
    journal = OrderJournal(repo)
    market = make_market()
    a = _sell(journal, market)
    # before a is persisted, building the same intent again yields the same key (backstop for races)
    assert _sell(journal, market).idempotency_key == a.idempotency_key
    journal.persist_intent(a, reserve_amount=Decimal(0))
    with pytest.raises(DuplicateOrderError):
        journal.persist_intent(_sell_with_key(journal, market, a.idempotency_key), reserve_amount=Decimal(0))
    b = _sell(journal, market)
    assert b.idempotency_key != a.idempotency_key
    journal.persist_intent(b, reserve_amount=Decimal(0))


def _sell_with_key(journal, market, key):
    intent = _sell(journal, market)
    intent.idempotency_key = key
    return intent


def test_buy_idempotency_key_is_unchanged_and_still_blocks_duplicates(clock, repo):
    journal = OrderJournal(repo)
    market = make_market()

    def buy():
        return journal.new_intent(
            market=market, outcome=Outcome.UP, side=Side.BUY, shares=Decimal("10"),
            limit_price=Decimal("0.51"), mode="LIVE",
        )

    first = buy()
    journal.persist_intent(first, reserve_amount=Decimal(0))
    second = buy()
    assert second.idempotency_key == first.idempotency_key
    with pytest.raises(DuplicateOrderError):
        journal.persist_intent(second, reserve_amount=Decimal(0))


@pytest.mark.parametrize("mode", ["PAPER", "LIVE", Mode.LIVE])
def test_order_mode_is_persisted_not_defaulted_to_paper(clock, repo, mode):
    journal = OrderJournal(repo)
    market = make_market()
    intent = journal.new_intent(
        market=market, outcome=Outcome.UP, side=Side.BUY, shares=Decimal("10"),
        limit_price=Decimal("0.51"), mode=mode,
    )
    journal.persist_intent(intent, reserve_amount=Decimal(0))
    expected = Mode(mode).value if not isinstance(mode, Mode) else mode.value
    assert repo.get_order(intent.client_order_id).mode == expected


def test_live_working_order_is_visible_to_the_exposure_ledger(clock, repo):
    from btc5m_bot.risk.exposure import ExposureLedger

    journal = OrderJournal(repo)
    market = make_market()
    intent = journal.new_intent(
        market=market, outcome=Outcome.UP, side=Side.BUY, shares=Decimal("10"),
        limit_price=Decimal("0.51"), mode="LIVE",
    )
    journal.persist_intent(intent, reserve_amount=Decimal(0))
    repo.update_order(intent.client_order_id, status=OrderStatus.SUBMITTING.value)
    ledger = ExposureLedger(repo)
    assert ledger.entries_used(market.market_id, "LIVE") == 1
    assert len(repo.open_orders(mode="LIVE")) == 1
    assert len(repo.open_orders(mode="PAPER")) == 0


def test_engine_never_drops_an_async_call_without_await():
    """engine.py once called `self._publish(now)` bare: the coroutine was created and discarded, so
    the dashboard never got the post-entry state push. Guard every module against that class of bug."""
    offenders = []
    for path in PKG.rglob("*.py"):
        tree = ast.parse(path.read_text())
        async_names = {n.name for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)}
        for node in ast.walk(tree):
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                func = node.value.func
                # only `self.<name>(...)` calls to a coroutine function defined in the same module
                if (
                    isinstance(func, ast.Attribute)
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "self"
                    and func.attr in async_names
                ):
                    offenders.append(f"{path.relative_to(PKG)}:{node.lineno} self.{func.attr}()")
    assert offenders == []


# ------------------------------------------------------------------ EV reason codes
def _edge(probability, **book_kwargs):
    from btc5m_bot.exchange.fees import FeeSchedule
    from btc5m_bot.intelligence.ev import evaluate_edge

    schedule = FeeSchedule(rate=Decimal("0.07"), exponent=1, enabled=True)
    book = book_kwargs.pop("book", None) or make_book("up", **book_kwargs)
    return evaluate_edge(
        probability=probability, book=book, outcome="UP", shares=Decimal("10"),
        schedule=schedule, max_slippage=Decimal("0.01"),
    )


def test_ev_reason_codes_match_the_actual_cause():
    from btc5m_bot.intelligence.ev import rejection_reason
    from btc5m_bot.types import ReasonCode

    # thin book: a depth problem, NOT a negative-EV one
    assert rejection_reason(_edge(0.9, bid="0.49", ask="0.50", ask_size="2")) is ReasonCode.INSUFFICIENT_DEPTH
    # no book at all
    assert rejection_reason(_no_book_edge()) is ReasonCode.INSUFFICIENT_DEPTH
    # walking into a worse level: slippage
    slip = _edge(0.95, asks=[("0.50", "5"), ("0.60", "100")], bid="0.49")
    assert rejection_reason(slip) is ReasonCode.SLIPPAGE_TOO_HIGH
    # model says the contract is overpriced: genuinely negative EV
    assert rejection_reason(_edge(0.30, bid="0.49", ask="0.50", ask_size="1000")) is ReasonCode.NEGATIVE_EV
    # positive EV but below the configured minimum net edge: insufficient edge
    small = _edge(0.52, bid="0.49", ask="0.50", ask_size="1000")
    assert small.net_edge > 0 and small.failure == ""
    assert rejection_reason(small) is ReasonCode.INSUFFICIENT_EDGE


def _no_book_edge():
    from btc5m_bot.exchange.fees import FeeSchedule
    from btc5m_bot.intelligence.ev import evaluate_edge

    return evaluate_edge(
        probability=0.9, book=None, outcome="UP", shares=Decimal("10"),
        schedule=FeeSchedule(rate=Decimal("0.07"), exponent=1, enabled=True),
    )
