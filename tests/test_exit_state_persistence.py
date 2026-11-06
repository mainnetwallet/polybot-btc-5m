"""Trailing-stop peaks and depth/spread baselines must survive a restart."""
from __future__ import annotations

from decimal import Decimal

from btc5m_bot.exits import ExitAction, HybridExitManager
from btc5m_bot.types import FeatureMode, Outcome

from conftest import START, make_book, make_market
from test_risk_exits import config, position


def _evaluate(exits, market, bid, cfg=None, **pos):
    book = make_book(market.asset_id(Outcome.UP), bid=bid, bid_size="100")
    return exits.evaluate(
        position=position(**pos), market=market, up_book=book, down_book=None,
        probability=None, config=cfg or config(), now=START,
    )


def test_export_and_load_round_trip_through_the_database(repo):
    market = make_market()
    exits = HybridExitManager()
    _evaluate(exits, market, "0.55")
    assert exits.dirty
    repo.set_state("exit_manager_state", exits.export_state())
    assert not exits.dirty

    reborn = HybridExitManager()
    restored = reborn.load_state(repo.get_state("exit_manager_state", {}), keep_position_ids={1})
    assert restored >= 2
    assert reborn._peak_bid == exits._peak_bid
    assert reborn._depth_peak == exits._depth_peak
    assert reborn._spread_floor == exits._spread_floor
    assert all(isinstance(v, Decimal) for v in reborn._peak_bid.values())


def test_restored_peak_keeps_the_trailing_stop_armed():
    market = make_market()
    modes = {**config().feature_modes, "trailing_stop": FeatureMode.ACTIVE}
    cfg = config(feature_modes=modes)  # trailing_stop_pct defaults to 15%

    before = HybridExitManager()
    _evaluate(before, market, "0.58", cfg)  # peak 0.58 (+16% ROI, below take-profit)
    snapshot = before.export_state()

    # A fresh manager that lost its memory treats the later price as the peak: no trailing exit.
    amnesiac = HybridExitManager()
    assert _evaluate(amnesiac, market, "0.49", cfg).reason != "TRAILING_STOP"

    # The restored one remembers the 0.58 peak: 0.49 is a 15.5% drawdown from it.
    restored = HybridExitManager()
    restored.load_state(snapshot)
    decision = _evaluate(restored, market, "0.49", cfg)
    assert decision.action is ExitAction.SELL
    assert decision.reason == "TRAILING_STOP"


def test_load_drops_closed_positions_and_ignores_corrupt_entries():
    exits = HybridExitManager()
    data = {
        "peak_bid": {"1:up-token": "0.6", "2:down-token": "0.7", "garbage": "x", "3:t": "nan"},
        "depth_peak": {"1:up-token": "250"},
        "spread_floor": "not-a-dict",
    }
    restored = exits.load_state(data, keep_position_ids={1})
    assert restored == 2
    assert exits._peak_bid == {"1:up-token": Decimal("0.6")}
    assert exits._spread_floor == {}
    assert HybridExitManager().load_state(None) == 0


def test_forget_marks_state_dirty_so_the_deletion_is_saved():
    market = make_market()
    exits = HybridExitManager()
    _evaluate(exits, market, "0.55")
    exits.export_state()
    assert not exits.dirty
    exits.forget(1)
    assert exits.dirty
    assert exits.export_state() == {"peak_bid": {}, "depth_peak": {}, "spread_floor": {}}


def test_engine_saves_when_dirty_throttles_and_restores_only_open_positions(settings, repo, adapter, config_store, clock):
    from test_engine_integration import _build

    engine, _market, _spec = _build(settings, repo, adapter, config_store, clock)
    key = engine.EXIT_STATE_KEY
    assert repo.get_state(key, {}) == {}

    engine.exits._peak_bid["7:tok"] = Decimal("0.61")
    engine.exits.dirty = True
    engine._persist_exit_state()
    assert repo.get_state(key, {})["peak_bid"] == {"7:tok": "0.61"}

    # Inside the throttle window a further change is held back (and stays dirty for later).
    engine.exits._peak_bid["7:tok"] = Decimal("0.64")
    engine.exits.dirty = True
    engine._persist_exit_state()
    assert repo.get_state(key, {})["peak_bid"] == {"7:tok": "0.61"}
    assert engine.exits.dirty
    engine._persist_exit_state(force=True)  # shutdown path
    assert repo.get_state(key, {})["peak_bid"] == {"7:tok": "0.64"}

    # Position 7 does not exist in the ledger (closed while the bot was down): it is not restored.
    fresh, _m, _s = _build(settings, repo, adapter, config_store, clock)
    fresh._restore_exit_state()
    assert fresh.exits._peak_bid == {}
