from __future__ import annotations

from decimal import Decimal

import pytest


def _decision(repo, n: int = 3) -> None:
    for i in range(n):
        repo.log_decision(market_id=f"m{i}", decision="NO_TRADE", reason_code="CAPITAL_UNSET")


def _trade(repo, mode: str, oid: str) -> None:
    repo.add_trade(
        market_id="m1",
        client_order_id=oid,
        side="BUY",
        outcome="UP",
        price=Decimal("0.5"),
        shares=Decimal("2"),
        fee=Decimal("0"),
        mode=mode,
    )


def test_clear_decisions_removes_all(repo):
    _decision(repo)
    assert len(repo.recent_decisions(10)) == 3

    assert repo.clear_activity("decisions") == 3
    assert repo.recent_decisions(10) == []


def test_clear_trades_keeps_live_rows(repo):
    _trade(repo, "PAPER", "p1")
    _trade(repo, "PAPER", "p2")
    _trade(repo, "LIVE", "l1")

    assert repo.clear_activity("trades") == 2
    remaining = repo.list_trades()
    assert [t.mode for t in remaining] == ["LIVE"]


def test_clear_unknown_kind_is_rejected(repo):
    with pytest.raises(ValueError):
        repo.clear_activity("risk")
