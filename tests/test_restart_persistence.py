"""State that used to live only in memory must survive a restart.

Each test plays the "restart" by building a second engine / auth manager on the same database.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from btc5m_bot.api.auth import LOCKOUT_STATE_KEY, AuthManager
from btc5m_bot.clock import FixedClock
from btc5m_bot.oracle.twap import TwapObservation

from conftest import START
from test_engine_integration import _build


def _obs(offset_seconds: int, value: str = "100000", source: str = "chainlink") -> TwapObservation:
    return TwapObservation(
        ts=START + timedelta(seconds=offset_seconds),
        value=Decimal(value),
        source=source,
        window_seconds=60,
        sequence=offset_seconds,
    )


def _row(o: TwapObservation) -> dict:
    return {
        "ts": o.ts, "value": o.value, "source": o.source,
        "symbol": o.symbol, "window_seconds": o.window_seconds, "sequence": o.sequence,
    }


# --------------------------------------------------------------- oracle TWAP history
def test_twap_rows_round_trip_in_time_order_and_prune(repo):
    items = [_obs(10, "100010.12345678"), _obs(-5, "99995.5"), _obs(0, "100000")]
    assert repo.add_twap_observations(_row(o) for o in items) == 3
    assert repo.add_twap_observations([]) == 0

    rows = repo.recent_twap_observations(START - timedelta(minutes=1))
    assert [r.ts for r in rows] == sorted(r.ts for r in rows)
    assert rows[-1].value == Decimal("100010.12345678")  # exact decimal, no float rounding
    assert rows[0].source == "chainlink" and rows[0].window_seconds == 60

    assert repo.prune_twap_observations(START) == 1  # only the one before START
    assert len(repo.recent_twap_observations(START - timedelta(hours=1))) == 2


def test_restart_restores_recent_twap_history_and_does_not_duplicate(settings, repo, adapter, config_store, clock):
    before, _m, _s = _build(settings, repo, adapter, config_store, clock)
    before.tracker.add(_obs(30, "100123.45"))
    before._persist_twap(force=True)
    saved = repo.recent_twap_observations(START - timedelta(hours=2))
    assert len(saved) == len(before.tracker)  # the whole in-memory history reached the database

    # "Restart": a fresh engine whose tracker is empty.
    after, _m, _s = _build(settings, repo, adapter, config_store, clock)
    after.tracker._obs.clear()
    assert len(after.tracker) == 0
    after._restore_twap_observations()
    assert len(after.tracker) == len(saved)
    assert after.tracker.latest().value == Decimal("100123.45")
    assert after.tracker.opening_reference(START) is not None  # the window's opening reference is back
    assert after._twap_cursor == saved[-1].ts  # the parquet recorder is not fed the same rows again

    # Persisting again with nothing new must not write the restored rows a second time.
    after._persist_twap(force=True)
    assert len(repo.recent_twap_observations(START - timedelta(hours=2))) == len(saved)

    # New readings after the restart are saved, once.
    after.tracker.add(_obs(60, "100200"))
    after._persist_twap(force=True)
    after._persist_twap(force=True)
    assert len(repo.recent_twap_observations(START - timedelta(hours=2))) == len(saved) + 1


def test_old_twap_history_is_not_restored_and_gets_pruned(settings, repo, adapter, config_store):
    clock = FixedClock(START)
    engine, _m, _s = _build(settings, repo, adapter, config_store, clock)
    repo.add_twap_observations([_row(_obs(-3 * 3600, "90000"))])  # three hours old
    engine.tracker._obs.clear()
    engine._restore_twap_observations()
    assert len(engine.tracker) == 0

    engine.tracker.add(_obs(0, "100000"))  # a fresh reading arrives after the restart
    engine._persist_twap(force=True)  # force also prunes
    remaining = repo.recent_twap_observations(START - timedelta(hours=6))
    assert [r.ts for r in remaining] == [START]  # the fresh reading was saved, the 3-hour-old one is gone


def test_restore_with_empty_database_is_a_noop(settings, repo, adapter, config_store, clock):
    engine, _m, _s = _build(settings, repo, adapter, config_store, clock)
    size = len(engine.tracker)
    engine._restore_twap_observations()
    assert len(engine.tracker) == size
    assert engine._twap_cursor is None


def test_twap_persistence_failure_never_raises_and_is_retried(settings, repo, adapter, config_store, clock, monkeypatch):
    engine, _m, _s = _build(settings, repo, adapter, config_store, clock)

    def boom(rows):
        raise RuntimeError("database asleep")

    monkeypatch.setattr(repo, "add_twap_observations", boom)
    engine._persist_twap(force=True)  # must not raise
    assert engine._twap_persist_cursor is None  # cursor did not advance, so the rows are retried

    monkeypatch.undo()
    engine._persist_twap(force=True)
    assert engine._twap_persist_cursor is not None
    assert len(repo.recent_twap_observations(START - timedelta(hours=2))) == len(engine.tracker)


# ----------------------------------------------------- resolution validation survives
def _save_simulation(repo, simulated: str | None = "UP") -> None:
    repo.save_resolution(
        market_id="mkt-1",
        simulated_outcome=simulated,
        actual_outcome=None,
        opening_ref=Decimal("100000"),
        closing_ref=Decimal("100010"),
        confidence="HIGH",
        methodology="TWAP_60S",
        observations={"used": 60, "coverage_pct": 100},
        validated=False,
    )


def _mismatch_events(repo) -> list:
    return [e for e in repo.recent_protection_events(50) if e.event_type == "RESOLUTION_MISMATCH"]


def test_actual_outcome_is_attached_to_a_simulation_saved_before_the_restart(settings, repo, adapter, config_store, clock):
    _save_simulation(repo, "UP")
    engine, _m, _s = _build(settings, repo, adapter, config_store, clock)
    assert engine._simulations == {}  # the in-memory simulation did not survive

    engine._record_actual("mkt-1", "up")
    row = repo.get_resolution("mkt-1")
    assert (row.actual_outcome, row.match, row.validated) == ("UP", True, True)
    assert _mismatch_events(repo) == []
    assert repo.resolution_validation_count() == (1, 1)


def test_mismatch_after_restart_is_flagged_once(settings, repo, adapter, config_store, clock):
    _save_simulation(repo, "UP")
    engine, _m, _s = _build(settings, repo, adapter, config_store, clock)

    engine._record_actual("mkt-1", "DOWN")
    engine._record_actual("mkt-1", "DOWN")  # the settle loop calls this again every few seconds
    row = repo.get_resolution("mkt-1")
    assert (row.actual_outcome, row.match, row.validated) == ("DOWN", False, False)
    assert len(_mismatch_events(repo)) == 1
    assert repo.resolution_validation_count() == (1, 0)


def test_no_saved_simulation_means_nothing_is_invented(settings, repo, adapter, config_store, clock):
    engine, _m, _s = _build(settings, repo, adapter, config_store, clock)
    engine._record_actual("mkt-1", "UP")
    assert repo.get_resolution("mkt-1") is None

    _save_simulation(repo, simulated=None)  # a simulation that had too little data to call a side
    engine._record_actual("mkt-1", "UP")
    row = repo.get_resolution("mkt-1")
    assert (row.actual_outcome, row.match, row.validated) == ("UP", None, False)
    assert _mismatch_events(repo) == []


# --------------------------------------------------- deferred settlement markers
def test_deferred_settlement_marker_survives_restart(settings, repo, adapter, config_store, clock):
    before, _m, _s = _build(settings, repo, adapter, config_store, clock)
    before._settlement_deferred.add("mkt-7")
    before._save_settlement_deferred()

    after, _m, _s = _build(settings, repo, adapter, config_store, clock)
    assert after._settlement_deferred == set()
    after._restore_settlement_deferred()
    assert after._settlement_deferred == {"mkt-7"}


def test_corrupt_deferred_marker_state_is_ignored(settings, repo, adapter, config_store, clock):
    repo.set_state("settlement_deferred", {"market_ids": "not-a-list"})
    engine, _m, _s = _build(settings, repo, adapter, config_store, clock)
    engine._restore_settlement_deferred()
    assert engine._settlement_deferred == set()


# --------------------------------------------------------------- login lockouts
def _fail(manager: AuthManager, times: int, ip: str = "9.9.9.9") -> None:
    for _ in range(times):
        with pytest.raises(Exception, match="invalid credentials|too many"):
            manager.login("admin", "wrong", ip=ip)


def test_lockout_survives_a_restart(settings, repo):
    _fail(AuthManager(settings, state_store=repo), 5)

    restarted = AuthManager(settings, state_store=repo)
    with pytest.raises(Exception, match="too many"):
        restarted.login("admin", "correct-horse-battery", ip="9.9.9.9")
    # Another address is unaffected.
    assert restarted.login("admin", "correct-horse-battery", ip="8.8.8.8").subject == "admin"


def test_partial_failure_count_survives_a_restart(settings, repo):
    _fail(AuthManager(settings, state_store=repo), 3)
    restarted = AuthManager(settings, state_store=repo)
    _fail(restarted, 2)  # 3 + 2 = 5 failures in the window: now locked
    with pytest.raises(Exception, match="too many"):
        restarted.login("admin", "correct-horse-battery", ip="9.9.9.9")


def test_successful_login_clears_saved_failures(settings, repo):
    manager = AuthManager(settings, state_store=repo)
    _fail(manager, 3)
    manager.login("admin", "correct-horse-battery", ip="9.9.9.9")
    assert repo.get_state(LOCKOUT_STATE_KEY)["failures"] == {}

    _fail(AuthManager(settings, state_store=repo), 4)  # would lock if the 3 old failures were still counted
    AuthManager(settings, state_store=repo).login("admin", "correct-horse-battery", ip="9.9.9.9")


def test_expired_or_corrupt_saved_lockouts_are_ignored(settings, repo):
    repo.set_state(
        LOCKOUT_STATE_KEY,
        {"blocked_until": {"9.9.9.9": 1.0, "bad": "x"}, "failures": {"9.9.9.9": [1.0, 2.0]}},
    )
    assert AuthManager(settings, state_store=repo).login("admin", "correct-horse-battery", ip="9.9.9.9")

    repo.set_state(LOCKOUT_STATE_KEY, {"blocked_until": ["nonsense"], "failures": "nonsense"})
    assert AuthManager(settings, state_store=repo).login("admin", "correct-horse-battery", ip="9.9.9.9")


def test_login_still_works_if_the_database_is_down(settings):
    class BrokenStore:
        def get_state(self, key, default=None):
            raise RuntimeError("down")

        def set_state(self, key, value):
            raise RuntimeError("down")

    manager = AuthManager(settings, state_store=BrokenStore())
    _fail(manager, 5)  # still enforced in memory
    with pytest.raises(Exception, match="too many"):
        manager.login("admin", "correct-horse-battery", ip="9.9.9.9")
    assert AuthManager(settings, state_store=BrokenStore()).login("admin", "correct-horse-battery", ip="1.1.1.1")
