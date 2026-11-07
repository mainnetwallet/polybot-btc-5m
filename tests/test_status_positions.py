"""`status` and the repository must see every open position, not just the first row."""
from __future__ import annotations

import argparse
import json
from decimal import Decimal

from btc5m_bot import cli


def _position(repo, market_id, *, mode="PAPER", outcome="UP", shares="10", price="0.50"):
    return repo.create_position(
        market_id=market_id, condition_id="cond-" + market_id, asset_id=f"{market_id}-{outcome.lower()}",
        outcome=outcome, shares=Decimal(shares), avg_price=Decimal(price), entry_fee=Decimal(0), mode=mode,
    )


def test_open_positions_returns_every_open_row_oldest_first(repo):
    first = _position(repo, "mkt-1")
    second = _position(repo, "mkt-2", outcome="DOWN")
    closed = _position(repo, "mkt-3")
    repo.update_position(closed.id, status="CLOSED")
    _position(repo, "mkt-4", mode="LIVE")

    paper = repo.open_positions("PAPER")
    assert [p.id for p in paper] == [first.id, second.id]
    assert [p.market_id for p in repo.open_positions("LIVE")] == ["mkt-4"]
    assert len(repo.open_positions()) == 3


def test_status_lists_both_open_positions(monkeypatch, capsys, settings, repo, db):
    _position(repo, "mkt-1", outcome="UP")
    _position(repo, "mkt-2", outcome="DOWN", shares="7")
    monkeypatch.setattr(cli, "_settings", lambda: settings)
    monkeypatch.setattr(cli, "_repo", lambda s: (repo, db))

    assert cli.cmd_status(argparse.Namespace()) == 0
    payload = json.loads(capsys.readouterr().out)

    assert [(p["market_id"], p["outcome"], p["shares"]) for p in payload["open_positions"]] == [
        ("mkt-1", "UP", "10"),
        ("mkt-2", "DOWN", "7"),
    ]
    assert "cooldown_until" not in payload["risk"]
