"""LIVE restart recovery when an order added to a position that was already open (a top-up).

The exchange reports one position, so its size is the total of the original entry and every top-up.
Recovery must merge only the order's own fill, never double-count it, and block when the sizes
cannot be explained.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal

from btc5m_bot.exchange.base import ExchangeOpenOrder, ExchangePosition
from btc5m_bot.execution.journal import OrderJournal
from btc5m_bot.recovery import RecoveryManager
from btc5m_bot.types import Mode, OrderStatus, Outcome, Side

from conftest import make_market

MARKET_ID = "mkt-1"


def _open_position(repo, *, shares="10", price="0.50"):
    return repo.create_position(
        market_id=MARKET_ID, condition_id="cond-" + MARKET_ID, asset_id="up-token", outcome="UP",
        shares=Decimal(shares), avg_price=Decimal(price), entry_fee=Decimal(0), mode="LIVE",
    )


def _unresolved_buy(repo, adapter, *, shares="10", matched="10", on_exchange=True):
    journal = OrderJournal(repo)
    intent = journal.new_intent(
        market=make_market(), outcome=Outcome.UP, side=Side.BUY, shares=Decimal(shares),
        limit_price=Decimal("0.60"), mode="LIVE",
    )
    journal.persist_intent(intent, reserve_amount=Decimal("6"))
    journal.mark_submitting(intent)
    repo.update_order(intent.client_order_id, exchange_order_id="ex-top", status=OrderStatus.ACKED.value)
    if on_exchange:
        adapter.open_orders["ex-top"] = ExchangeOpenOrder(
            order_id="ex-top", asset_id="up-token", condition_id="cond-" + MARKET_ID, side="BUY",
            price=Decimal("0.60"), original_size=Decimal(shares), size_matched=Decimal(matched), status="matched",
        )
    return intent


def _exchange_position(adapter, *, size, avg_price):
    adapter.positions = [
        ExchangePosition(asset_id="up-token", condition_id="cond-" + MARKET_ID, size=Decimal(size), avg_price=Decimal(avg_price))
    ]


def _recover(repo, adapter, clock):
    return asyncio.run(RecoveryManager(repo=repo, adapter=adapter, clock=clock, mode=Mode.LIVE).run())


def test_unmerged_topup_is_added_to_the_open_position(clock, repo, adapter):
    _open_position(repo, shares="10", price="0.50")
    intent = _unresolved_buy(repo, adapter, matched="10")
    # 10 @ 0.50 plus 10 @ 0.60 -> 20 shares, blended 0.55
    _exchange_position(adapter, size="20", avg_price="0.55")

    result = _recover(repo, adapter, clock)

    assert result.ready is True, result.blocks
    position = repo.get_position(MARKET_ID, "LIVE")
    assert position.shares == Decimal("20")
    assert position.avg_price == Decimal("0.55")
    assert position.cost_basis == Decimal("11.00")
    assert repo.get_order(intent.client_order_id).status == OrderStatus.FILLED.value
    assert [t.kind for t in repo.list_trades(mode="LIVE")] == ["ENTRY_ADD"]


def test_topup_already_merged_before_the_crash_is_not_counted_twice(clock, repo, adapter):
    _open_position(repo, shares="20", price="0.55")  # the merge happened, the order row never closed
    intent = _unresolved_buy(repo, adapter, matched="10")
    _exchange_position(adapter, size="20", avg_price="0.55")

    result = _recover(repo, adapter, clock)

    assert result.ready is True, result.blocks
    assert repo.get_position(MARKET_ID, "LIVE").shares == Decimal("20")
    assert repo.get_order(intent.client_order_id).status == OrderStatus.FILLED.value
    assert repo.list_trades(mode="LIVE") == []


def test_topup_with_an_unexplained_exchange_size_blocks_and_changes_nothing(clock, repo, adapter):
    _open_position(repo, shares="10", price="0.50")
    _unresolved_buy(repo, adapter, matched="10")
    _exchange_position(adapter, size="35", avg_price="0.55")

    result = _recover(repo, adapter, clock)

    assert result.ready is False
    assert any("manual reconciliation" in b for b in result.blocks)
    assert repo.get_position(MARKET_ID, "LIVE").shares == Decimal("10")


def test_topup_inferred_from_position_growth_when_the_order_is_gone(clock, repo, adapter):
    _open_position(repo, shares="10", price="0.50")
    _unresolved_buy(repo, adapter, matched="10", on_exchange=False)
    _exchange_position(adapter, size="20", avg_price="0.55")

    result = _recover(repo, adapter, clock)

    assert result.ready is True, result.blocks
    position = repo.get_position(MARKET_ID, "LIVE")
    assert position.shares == Decimal("20") and position.avg_price == Decimal("0.55")


def test_missing_order_with_no_position_growth_is_not_treated_as_a_fill(clock, repo, adapter):
    _open_position(repo, shares="10", price="0.50")
    intent = _unresolved_buy(repo, adapter, matched="10", on_exchange=False)
    _exchange_position(adapter, size="10", avg_price="0.50")

    result = _recover(repo, adapter, clock)

    assert result.ready is False
    assert repo.get_position(MARKET_ID, "LIVE").shares == Decimal("10")
    assert repo.get_order(intent.client_order_id).status != OrderStatus.FILLED.value


def test_first_entry_still_creates_the_position(clock, repo, adapter):
    intent = _unresolved_buy(repo, adapter, matched="10")
    _exchange_position(adapter, size="10", avg_price="0.60")

    result = _recover(repo, adapter, clock)

    assert result.ready is True, result.blocks
    position = repo.get_position(MARKET_ID, "LIVE")
    assert position.shares == Decimal("10") and position.avg_price == Decimal("0.60")
    assert repo.get_order(intent.client_order_id).status == OrderStatus.FILLED.value
