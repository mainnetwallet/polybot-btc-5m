"""Offline engine iteration tests: no network, no funds, no heartbeat."""
from __future__ import annotations

import asyncio
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from btc5m_bot.clock import FixedClock
from btc5m_bot.data.series import BtcSeries, BtcTick
from btc5m_bot.config import Settings
from btc5m_bot.engine import BotEngine
from btc5m_bot.execution.journal import OrderJournal
from btc5m_bot.execution.paper import PaperBroker
from btc5m_bot.markets.rules import build_rule_spec
from btc5m_bot.oracle.twap import TwapObservation, TwapTracker
from btc5m_bot.recovery import RecoveryResult
from btc5m_bot.types import (
    MarketState,
    Mode,
    OrderStatus,
    Outcome,
    ReasonCode,
    RecoveryState,
    Side,
)

from conftest import START, make_market


class DummyFeed:
    def __init__(self, name: str, series: BtcSeries) -> None:
        self.name = name
        self.series = series

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None


class DummyOracle:
    def __init__(self, tracker: TwapTracker) -> None:
        self.tracker = tracker

    def status(self, now):  # noqa: ANN001, ANN201 - mirrors PolyBoltTwapFeed.status
        return self.tracker.health(now)


class DummyRecorder:
    def __init__(self) -> None:
        self.rows: list[tuple[str, dict]] = []

    def write(self, dataset: str, row: dict) -> None:
        self.rows.append((dataset, row))

    def write_many(self, dataset: str, rows: list[dict]) -> None:
        self.rows.extend((dataset, row) for row in rows)

    def close(self) -> None:
        return None


def _series(source: str) -> BtcSeries:
    series = BtcSeries(source)
    for i in range(-30, 1):
        ts = START + timedelta(seconds=i)
        series.add(
            BtcTick(
                ts=ts,
                price=Decimal("100000") + Decimal(str(i)) * Decimal("0.5"),
                qty=Decimal("0.1"),
                source=source,
                sequence=i + 31,
                recv_ts=ts,
            )
        )
    return series


def _oracle() -> DummyOracle:
    tracker = TwapTracker(window_seconds=60)
    for i in range(-120, 1, 2):
        tracker.add(
            TwapObservation(
                ts=START + timedelta(seconds=i),
                value=Decimal("100000") + Decimal(str(i)) * Decimal("0.01"),
                source="chainlink",
                window_seconds=60,
            )
        )
    return DummyOracle(tracker)


def _build(settings, repo, adapter, config_store, clock):
    market = make_market()
    spec = build_rule_spec(market.raw["market"], START, market.version.value)
    # Real discovery publishes this fingerprint; keep the test market consistent.
    market.rules_fingerprint = spec.fingerprint
    adapter.markets[market.market_id] = market
    adapter.set_book(market.asset_id(Outcome.UP), bids=[("0.48", "1000")], asks=[("0.50", "1000")], timestamp=START)
    adapter.set_book(market.asset_id(Outcome.DOWN), bids=[("0.48", "1000")], asks=[("0.50", "1000")], timestamp=START)
    feeds = {
        "binance": DummyFeed("binance", _series("binance")),
        "coinbase": DummyFeed("coinbase", _series("coinbase")),
    }
    engine = BotEngine(
        settings=settings,
        repo=repo,
        adapter=adapter,
        config_store=config_store,
        clock=clock,
        feeds=feeds,
        oracle=_oracle(),
        recorder=DummyRecorder(),
        paper=PaperBroker(Decimal("100"), clock),
        start_api=False,
    )
    engine._recovery_result = RecoveryResult(ready=True, state=RecoveryState.READY)
    # The pipeline tests exercise everything behind the master switch, so turn it on here.
    # The switch itself is covered by the tests at the bottom of this file.
    config_store.update({"trading_enabled": True})
    return engine, market, spec


def test_paper_iteration_discovers_and_fails_closed(settings, repo, adapter, config_store):
    clock = FixedClock(START)
    engine, market, _ = _build(settings, repo, adapter, config_store, clock)

    asyncio.run(engine._iteration())

    machine = engine._machines[market.market_id]
    assert machine.state is MarketState.ACTIVE
    assert engine._effective_mode is Mode.PAPER
    assert adapter.placed == []
    assert adapter.heartbeat_ids == []
    assert engine._last_decision is not None
    assert engine._last_decision["decision"] == "NO_TRADE"
    # No rule approval step any more: the bot goes straight to the normal checks (here: no data yet).
    assert engine._last_decision["reason"] == ReasonCode.LOW_PROBABILITY.value
    assert repo.get_window(market.market_id) is not None


def test_dashboard_snapshot_carries_hero_fields(settings, repo, adapter, config_store):
    """The dashboard hero needs the price to beat and a timestamped Chainlink value."""
    from datetime import datetime

    clock = FixedClock(START)
    engine, _, _ = _build(settings, repo, adapter, config_store, clock)

    asyncio.run(engine._iteration())

    snap = engine.state.snapshot()
    assert Decimal(snap["market"]["price_to_beat"]) == Decimal("100000")
    chainlink = next(f for f in snap["feeds"] if "chainlink" in f["name"])
    assert Decimal(chainlink["last_value"]) == Decimal("100000")
    assert datetime.fromisoformat(chainlink["last_tick_at"]) == START


def test_live_mode_without_activation_stays_paper(settings, repo, adapter, config_store):
    clock = FixedClock(START)
    engine, _, _ = _build(settings, repo, adapter, config_store, clock)
    config_store.update({"mode": "LIVE"})

    asyncio.run(engine._iteration())

    assert engine.mode is Mode.LIVE
    assert engine._effective_mode is Mode.PAPER
    assert engine._gate_report is not None
    assert engine._gate_report.passed is False
    assert adapter.placed == []
    assert engine.state.snapshot()["live_locked"] is True


def test_gate_pass_alone_cannot_unlock_live(settings, repo, adapter, config_store):
    clock = FixedClock(START)
    engine, _, _ = _build(settings, repo, adapter, config_store, clock)
    config_store.update({"mode": "LIVE"})
    engine._gate_report = SimpleNamespace(passed=True)

    engine._refresh_mode()

    assert engine._effective_mode is Mode.PAPER
    assert repo.active_live_activation() is None


def test_paper_iteration_writes_analytics_ready_data(settings, repo, adapter, config_store):
    clock = FixedClock(START)
    engine, market, spec = _build(settings, repo, adapter, config_store, clock)
    # Book snapshots are rate-limited to one per two seconds; the first sample
    # lands after that interval has elapsed.
    clock.advance(2.0)

    asyncio.run(engine._iteration())

    recorder = engine.recorder
    datasets = {name for name, _ in recorder.rows}
    assert {"btc_ticks", "oracle_twap", "book_snapshots", "signal_features"} <= datasets
    feature_rows = [row for name, row in recorder.rows if name == "signal_features"]
    assert feature_rows and feature_rows[-1]["remaining_seconds"] == 298.0
    assert repo.get_resolution_rule(spec.fingerprint) is not None


def test_unresolved_order_blocks_entry(settings, repo, adapter, config_store):
    clock = FixedClock(START)
    engine, market, spec = _build(settings, repo, adapter, config_store, clock)
    asyncio.run(engine._iteration())
    repo.approve_resolution_rule(spec.fingerprint, "test-operator")

    journal = OrderJournal(repo)
    unresolved = journal.new_intent(
        market=market,
        outcome=Outcome.UP,
        side=Side.SELL,
        shares=Decimal("5"),
        limit_price=Decimal("0.49"),
        order_type="FOK",
        mode="PAPER",
    )
    repo.create_order_intent(unresolved, status=OrderStatus.PENDING)

    asyncio.run(engine._iteration())

    assert engine._last_decision is not None
    assert engine._last_decision["decision"] == "NO_TRADE"
    assert engine._last_decision["reason"] == ReasonCode.RECOVERY_NOT_READY.value
    assert unresolved.client_order_id in engine._last_decision["detail"]
    assert adapter.placed == []


def test_live_redemption_requires_redeemable_exchange_position_and_confirms_once(settings, repo, adapter, config_store):
    clock = FixedClock(START)
    engine, market, _ = _build(settings, repo, adapter, config_store, clock)
    config_store.update({"mode": "LIVE"})
    engine._effective_mode = Mode.LIVE
    position = repo.create_position(
        market_id=market.market_id, condition_id=market.condition_id,
        asset_id=market.asset_id(Outcome.UP), outcome="UP",
        shares=Decimal("5"), avg_price=Decimal("0.5"), entry_fee=Decimal(0), mode="LIVE",
    )
    repo.update_position(position.id, status="SETTLED")
    from btc5m_bot.exchange.base import ExchangePosition
    adapter.positions = [ExchangePosition(
        asset_id=position.asset_id, condition_id=position.condition_id,
        size=Decimal("5"), avg_price=Decimal("0.5"), redeemable=True,
    )]
    asyncio.run(engine._maybe_redeem())
    assert repo.get_position(market.market_id, "LIVE").status == "REDEEMED"
    state = repo.get_state(f"redemption:{market.condition_id}")
    assert state["status"] == "CONFIRMED"
    assert state["transaction_hash"] == "0xfake-redeem"
    asyncio.run(engine._maybe_redeem())
    assert repo.get_state(f"redemption:{market.condition_id}")["status"] == "CONFIRMED"


def test_settlement_deferred_while_order_unresolved(settings, repo, adapter, config_store):
    clock = FixedClock(START)
    engine, market, _ = _build(settings, repo, adapter, config_store, clock)
    asyncio.run(engine._iteration())
    machine = engine._machines[market.market_id]

    repo.create_position(
        market_id=market.market_id,
        condition_id=market.condition_id,
        asset_id=market.asset_id(Outcome.UP),
        outcome="UP",
        shares=Decimal("5"),
        avg_price=Decimal("0.5"),
        entry_fee=Decimal(0),
        mode="PAPER",
    )
    journal = OrderJournal(repo)
    unresolved = journal.new_intent(
        market=market,
        outcome=Outcome.UP,
        side=Side.SELL,
        shares=Decimal("5"),
        limit_price=Decimal("0.49"),
        order_type="FOK",
        mode="PAPER",
    )
    repo.create_order_intent(unresolved, status=OrderStatus.UNKNOWN)

    clock.advance(310)
    asyncio.run(engine._finalize_window(machine, "UP", clock.now()))

    position = repo.get_position(market.market_id, "PAPER")
    assert position.status == "OPEN"
    assert position.realized_pnl == Decimal(0)
    assert machine.state is not MarketState.SETTLED
    events = {e.event_type for e in repo.recent_protection_events(limit=25)}
    assert "SETTLEMENT_DEFERRED" in events


def test_trading_switch_defaults_off_and_blocks_new_entries(settings, repo, adapter, config_store):
    clock = FixedClock(START)
    engine, market, spec = _build(settings, repo, adapter, config_store, clock)
    config_store.update({"trading_enabled": False})
    asyncio.run(engine._iteration())  # discovery registers the rule
    repo.approve_resolution_rule(spec.fingerprint, "test-operator")

    asyncio.run(engine._iteration())

    assert engine._last_decision["decision"] == "NO_TRADE"
    assert engine._last_decision["reason"] == ReasonCode.TRADING_OFF.value
    assert adapter.placed == []
    assert engine.ledger.has_active_position(engine._effective_mode.value) is False


def test_trading_switch_on_lets_the_normal_checks_run(settings, repo, adapter, config_store):
    clock = FixedClock(START)
    engine, _, _ = _build(settings, repo, adapter, config_store, clock)  # switch ON

    asyncio.run(engine._iteration())

    assert engine._last_decision["reason"] == ReasonCode.LOW_PROBABILITY.value  # not TRADING_OFF, not a rule gate


def test_manage_position_visits_every_open_position(settings, repo, adapter, config_store):
    clock = FixedClock(START)
    engine, market, _ = _build(settings, repo, adapter, config_store, clock)
    for market_id in ("mkt-a", "mkt-b"):
        repo.create_position(
            market_id=market_id, condition_id=market.condition_id,
            asset_id=market.asset_id(Outcome.UP), outcome=Outcome.UP.value,
            shares=Decimal("5"), avg_price=Decimal("0.5"), entry_fee=Decimal(0), mode="PAPER",
        )
    seen: list[str] = []

    async def fake_manage(now, position):
        seen.append(position.market_id)

    engine._manage_one_position = fake_manage
    asyncio.run(engine._manage_position(START))

    assert sorted(seen) == ["mkt-a", "mkt-b"]


def test_trading_switch_fresh_config_is_off(config_store):
    from btc5m_bot.config import RuntimeConfig

    assert RuntimeConfig().trading_enabled is False


def test_rules_are_approved_automatically_in_paper(settings, repo, adapter, config_store):
    clock = FixedClock(START)
    engine, _, spec = _build(settings, repo, adapter, config_store, clock)

    asyncio.run(engine._iteration())

    rule = repo.get_resolution_rule(spec.fingerprint)
    assert rule is not None and rule.approved
    assert rule.approved_by == "auto-approve"


def test_rules_are_approved_automatically_in_live_mode_too(settings, repo, adapter, config_store):
    clock = FixedClock(START)
    engine, _, spec = _build(settings, repo, adapter, config_store, clock)
    config_store.update({"mode": "LIVE"})

    asyncio.run(engine._iteration())

    rule = repo.get_resolution_rule(spec.fingerprint)
    assert rule is not None and rule.approved  # nothing waits for a manual approval


def test_pinned_fingerprint_is_approved_without_auto_flag(settings, repo, adapter, config_store):
    clock = FixedClock(START)
    engine, _, spec = _build(settings, repo, adapter, config_store, clock)
    settings.approved_rule_fingerprints = spec.fingerprint.upper()

    asyncio.run(engine._iteration())

    rule = repo.get_resolution_rule(spec.fingerprint)
    assert rule is not None and rule.approved


def test_paper_start_balance_applied_on_boot(tmp_path, monkeypatch):
    from btc5m_bot.engine import build_default_engine

    monkeypatch.setenv("POLYBOT_PAPER_START_BALANCE_USD", "500")
    s = Settings(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
        config_dir=tmp_path / "config",
        paper_start_balance_usd=Decimal("500"),
    )
    s.ensure_dirs()
    engine, db_engine = build_default_engine(s, start_api=False)
    try:
        assert engine.paper.cash == Decimal("500")
    finally:
        db_engine.dispose()


def test_repeated_no_trade_reason_is_logged_once_per_minute(settings, repo, adapter, config_store):
    clock = FixedClock(START)
    engine, _, _ = _build(settings, repo, adapter, config_store, clock)
    asyncio.run(engine._iteration())  # discovers the market
    before = len(repo.recent_decisions(500))

    for _ in range(20):  # 40 s of the same reason, as in the 120-second-window-closed spam
        engine._note(clock.now(), "NO_TRADE", ReasonCode.ENTRY_WINDOW_CLOSED, "120-second entry window closed")
        clock.advance(2)
    assert len(repo.recent_decisions(500)) - before == 1

    clock.advance(60)  # heartbeat after a minute
    engine._note(clock.now(), "NO_TRADE", ReasonCode.ENTRY_WINDOW_CLOSED, "120-second entry window closed")
    assert len(repo.recent_decisions(500)) - before == 2

    clock.advance(2)  # a different reason is logged right away
    engine._note(clock.now(), "NO_TRADE", ReasonCode.TRADING_OFF, "trading switch is OFF")
    assert len(repo.recent_decisions(500)) - before == 3


def test_paper_waits_for_actual_resolution_when_oracle_simulation_is_ready(settings, repo, adapter, config_store):
    clock = FixedClock(START)
    engine, market, _ = _build(settings, repo, adapter, config_store, clock)
    asyncio.run(engine._iteration())
    machine = engine._machines[market.market_id]
    repo.create_position(
        market_id=market.market_id,
        condition_id=market.condition_id,
        asset_id=market.asset_id(Outcome.UP),
        outcome="UP",
        shares=Decimal("5"),
        avg_price=Decimal("0.5"),
        entry_fee=Decimal(0),
        mode="PAPER",
    )
    # Oracle simulation says UP; the exchange has not published a resolution yet.
    engine._simulations[market.market_id] = (
        None,
        SimpleNamespace(valid=True, outcome=Outcome.UP, opening_reference=None, closing_reference=None,
                        confidence=1.0, notional_methodology="test", observations_used=[], coverage_pct=100),
    )
    adapter.resolutions.pop(market.condition_id, None)

    # Inside the grace period: still open.
    clock.advance(310)
    asyncio.run(engine._settle_windows(clock.now()))
    assert repo.get_position(market.market_id, "PAPER").status == "OPEN"

    # A valid simulation still cannot be booked as an actual exchange resolution.
    clock.advance(30)
    asyncio.run(engine._settle_windows(clock.now()))
    position = repo.get_position(market.market_id, "PAPER")
    assert position.status == "OPEN"
    assert machine.state is not MarketState.SETTLED
    assert not any(t.kind == "SETTLEMENT" for t in repo.list_trades(limit=10))

    # Once the venue reports the actual outcome, the position can be settled.
    adapter.resolutions[market.condition_id] = "UP"
    clock.advance(11)
    asyncio.run(engine._settle_windows(clock.now()))
    position = repo.get_position(market.market_id, "PAPER")
    assert position.status == "SETTLED"
    assert machine.state is MarketState.SETTLED
    assert any(t.kind == "SETTLEMENT" for t in repo.list_trades(limit=10))


def test_invalid_oracle_simulation_is_retried_until_it_is_valid(settings, repo, adapter, config_store):
    """An early 'not enough oracle coverage' result used to be cached forever, so paper positions
    could never settle from the simulation and stayed open until on-chain resolution."""
    clock = FixedClock(START)
    engine, market, _ = _build(settings, repo, adapter, config_store, clock)
    asyncio.run(engine._iteration())
    calls: list[int] = []

    def fake_attempt(machine, now):
        calls.append(1)
        engine._simulations[machine.market.market_id] = (
            None,
            SimpleNamespace(valid=len(calls) >= 2, outcome=Outcome.UP, reason="coverage 20% < 40%"),
        )

    engine._attempt_simulation = fake_attempt
    clock.advance(310)
    asyncio.run(engine._settle_windows(clock.now()))
    assert len(calls) == 1 and engine._simulations[market.market_id][1].valid is False

    clock.advance(1)  # inside the 5 s throttle: no retry yet
    asyncio.run(engine._settle_windows(clock.now()))
    assert len(calls) == 1

    clock.advance(6)  # throttle elapsed and the cached result is invalid: try again
    asyncio.run(engine._settle_windows(clock.now()))
    assert len(calls) == 2 and engine._simulations[market.market_id][1].valid is True

    clock.advance(6)  # a valid simulation is final: never recomputed
    asyncio.run(engine._settle_windows(clock.now()))
    assert len(calls) == 2


def test_dashboard_edits_of_taker_delay_and_oracle_coverage_apply_without_restart(
    settings, repo, adapter, config_store
):
    clock = FixedClock(START)
    engine, _, _ = _build(settings, repo, adapter, config_store, clock)
    assert engine.paper.taker_delay_ms != 7
    config_store.update({"taker_delay_ms": 7, "min_oracle_coverage_seconds": 33})
    asyncio.run(engine._iteration())
    assert engine.paper.taker_delay_ms == 7
    assert engine.execution.taker_delay_ms == 7
    assert engine.probability_engine.min_coverage_seconds == 33.0


def test_duplicate_exit_order_is_recorded_not_swallowed(settings, repo, adapter, config_store):
    from btc5m_bot.db.repo import DuplicateOrderError
    from btc5m_bot.exits import ExitAction, ExitDecision

    clock = FixedClock(START)
    engine, market, _ = _build(settings, repo, adapter, config_store, clock)
    asyncio.run(engine._iteration())
    repo.create_position(
        market_id=market.market_id,
        condition_id=market.condition_id,
        asset_id=market.asset_id(Outcome.UP),
        outcome="UP",
        shares=Decimal("5"),
        avg_price=Decimal("0.5"),
        entry_fee=Decimal(0),
        mode="PAPER",
    )
    engine.exits.evaluate = lambda **kwargs: ExitDecision(
        ExitAction.SELL, "STOP_LOSS", "test", 2, shares=Decimal("5"), limit_price=Decimal("0.48")
    )

    async def duplicate(*args, **kwargs):
        raise DuplicateOrderError("dup-key")

    engine.execution.execute_exit = duplicate
    clock.advance(5)
    asyncio.run(engine._manage_position(clock.now()))

    events = {e.event_type for e in repo.recent_protection_events(limit=25)}
    assert "EXIT_DUPLICATE_ORDER" in events


def test_usdc_buy_then_market_sell_of_every_share_end_to_end(settings, repo, adapter, config_store):
    """$1 USDC buy (about 1.67 shares, far below any 5-share minimum) -> stop-loss -> whole position
    market-sold as FAK at the lowest tick, even into a thin book. Real sizing, executor and exit
    manager; only the exchange is the paper broker."""
    from btc5m_bot.sizing import size_position
    from btc5m_bot.exchange.fees import FeeSchedule

    clock = FixedClock(START)
    engine, market, _ = _build(settings, repo, adapter, config_store, clock)
    asyncio.run(engine._iteration())
    up = market.asset_id(Outcome.UP)

    # ---- BUY: sized and sent purely in USDC ----
    adapter.set_book(up, bids=[("0.58", "1000")], asks=[("0.60", "1000")], timestamp=START)
    book = asyncio.run(adapter.get_order_book(up))
    sizing = size_position(
        trade_size_usd=Decimal("1"), balance=Decimal("100"), book=book,
        schedule=FeeSchedule.from_market(market),
    )
    assert sizing.allowed, sizing.detail
    intent = engine.journal.new_intent(
        market=market, outcome=Outcome.UP, side=Side.BUY, shares=sizing.shares,
        limit_price=Decimal("0.61"), order_type="FOK", mode="PAPER", usdc_amount=Decimal("1"),
    )
    entry = asyncio.run(engine.execution.execute_entry(intent, market, book))
    assert entry.result.filled_shares == Decimal("1.6666")  # $1 / 0.60, no share minimum applied
    position = repo.get_position(market.market_id, "PAPER")
    assert position is not None and position.shares == Decimal("1.6666")

    # ---- SELL: price collapses into a thin book; everything held is market-sold ----
    adapter.set_book(up, bids=[("0.20", "0.5"), ("0.10", "5")], asks=[("0.25", "100")], timestamp=START)
    clock.advance(5)
    engine.exits.dirty = True
    asyncio.run(engine._manage_position(clock.now()))

    sells = [o for o in repo.list_orders(mode="PAPER", limit=50) if o.side == "SELL"]
    assert len(sells) == 1
    assert sells[0].order_type == "FAK"
    assert Decimal(sells[0].shares) == Decimal("1.6666")  # every share held
    assert Decimal(sells[0].price) == market.tick_size  # market: no price floor above the lowest tick
    assert repo.get_position(market.market_id, "PAPER").status == "CLOSED"


# ------------------------------------------------------------------ regressions: exits and risk
def _buy_paper_position(engine, adapter, market, *, usdc: str = "10"):
    """USDC market BUY of UP at 0.60 through the real executor and the paper broker."""
    up = market.asset_id(Outcome.UP)
    adapter.set_book(up, bids=[("0.58", "1000")], asks=[("0.60", "1000")], timestamp=START)
    book = asyncio.run(adapter.get_order_book(up))
    engine.paper.miss_probability = 0.0  # these tests are about bookkeeping, not random misses
    intent = engine.journal.new_intent(
        market=market, outcome=Outcome.UP, side=Side.BUY,
        shares=(Decimal(usdc) / Decimal("0.60")).quantize(Decimal("0.0001")),
        limit_price=Decimal("0.61"), order_type="FOK", mode="PAPER", usdc_amount=Decimal(usdc),
    )
    entry = asyncio.run(engine.execution.execute_entry(intent, market, book))
    assert entry.result.filled_shares > 0
    return engine.repo.get_position(market.market_id, "PAPER")


@pytest.mark.parametrize(
    ("bid", "config_patch", "lock_reason"),
    [
        ("0.20", {}, ReasonCode.DAILY_LOSS_LIMIT),
        ("0.80", {"daily_profit_lock_usd": "0.5"}, ReasonCode.PROFIT_LOCK),
    ],
)
def test_exit_that_trips_a_daily_lock_still_finishes_its_bookkeeping(
    settings, repo, adapter, config_store, bid, config_patch, lock_reason
):
    """The exit that locked the day died with AttributeError (RiskUpdate had no ``profit_locked``),
    which skipped the EXIT log line and ``exits.forget()`` on exactly the protective event."""
    clock = FixedClock(START)
    engine, market, _ = _build(settings, repo, adapter, config_store, clock)
    config_store.update(config_patch)
    asyncio.run(engine._iteration())
    _buy_paper_position(engine, adapter, market)
    clock.advance(5)
    adapter.set_book(
        market.asset_id(Outcome.UP), bids=[(bid, "1000")], asks=[("0.99", "1000")], timestamp=clock.now()
    )

    asyncio.run(engine._manage_position(clock.now()))

    assert repo.get_position(market.market_id, "PAPER").status == "CLOSED"
    assert repo.get_risk_day(START.date()).locked is True
    assert engine.exits._peak_bid == {}  # exits.forget() ran
    decisions = repo.recent_decisions(50)
    assert any(d.decision == "EXIT" and d.reason_code == lock_reason.value for d in decisions)
    assert any(d.decision == "EXIT" and d.reason_detail.startswith("SELL UP") for d in decisions)


def test_open_position_waits_for_resolution_once_the_market_has_ended(settings, repo, adapter, config_store):
    """After the window ends the venue resolves the market. The bot kept managing the position
    against its last (stale) book and dumped it with EMERGENCY_EXIT, booking a made-up loss and
    feeding it to the paper stats that gate LIVE."""
    clock = FixedClock(START)
    engine, market, _ = _build(settings, repo, adapter, config_store, clock)
    asyncio.run(engine._iteration())
    _buy_paper_position(engine, adapter, market)

    clock.advance(304)  # the market ended at START + 300 s; the book in hand is from START
    asyncio.run(engine._manage_position(clock.now()))

    assert repo.get_position(market.market_id, "PAPER").status == "OPEN"
    assert [o for o in repo.list_orders(mode="PAPER", limit=50) if o.side == "SELL"] == []
    day = repo.get_risk_day(START.date())
    assert (day.trades, day.losses) == (0, 0) and day.realized_pnl == Decimal(0)

    # The venue then reports the real result and the normal settlement path books it once.
    engine._attempt_simulation = lambda machine, now: None
    adapter.resolutions[market.condition_id] = "UP"
    clock.advance(11)
    asyncio.run(engine._settle_windows(clock.now()))
    assert repo.get_position(market.market_id, "PAPER").status == "SETTLED"
    day = repo.get_risk_day(START.date())
    assert (day.trades, day.wins, day.losses) == (1, 1, 0)


def test_losing_position_closed_in_two_partial_fills_counts_as_one_loss(settings, repo, adapter, config_store):
    """Each partial fill was booked as its own losing trade: one position took the loss streak to 2
    (= max_consecutive_losses) and started the 15 minute cooldown, and trades/losses doubled."""
    clock = FixedClock(START)
    engine, market, _ = _build(settings, repo, adapter, config_store, clock)
    config_store.update({"daily_loss_limit_usd": "1000"})  # keep the daily lock out of this test
    asyncio.run(engine._iteration())
    _buy_paper_position(engine, adapter, market)  # 16.6666 shares at 0.60
    up = market.asset_id(Outcome.UP)

    clock.advance(5)  # first exit: only 6 shares of bid depth, so the FAK sell fills partially
    adapter.set_book(up, bids=[("0.30", "6")], asks=[("0.35", "100")], timestamp=clock.now())
    asyncio.run(engine._manage_position(clock.now()))
    partial = repo.get_position(market.market_id, "PAPER")
    assert partial.status == "OPEN" and partial.shares == Decimal("10.6666")
    day = repo.get_risk_day(START.date())
    assert (day.trades, day.wins, day.losses) == (0, 0, 0)  # the position is still open
    assert day.realized_pnl < 0  # but the money already lost is in the daily P&L
    assert repo.get_risk_state().consecutive_losses == 0

    clock.advance(3)  # past the 2 s exit-retry throttle: the rest is swept
    adapter.set_book(up, bids=[("0.30", "100")], asks=[("0.35", "100")], timestamp=clock.now())
    asyncio.run(engine._manage_position(clock.now()))
    closed = repo.get_position(market.market_id, "PAPER")
    assert closed.status == "CLOSED"
    day = repo.get_risk_day(START.date())
    state = repo.get_risk_state()
    assert (day.trades, day.wins, day.losses) == (1, 0, 1)
    assert state.consecutive_losses == 1 and state.cooldown_until is None
    assert day.realized_pnl == closed.realized_pnl  # daily P&L is the sum of both fills
    assert engine.risk.status(Mode.PAPER, now=clock.now()).reason is not ReasonCode.COOLDOWN


def test_position_with_a_partial_exit_is_judged_on_its_total_pnl_at_settlement(settings, repo, adapter, config_store):
    """A big loss on a partial exit, then a small gain on the shares that ride to settlement: the
    position lost money overall, so it is one loss and the streak must not be reset by the gain."""
    clock = FixedClock(START)
    engine, market, _ = _build(settings, repo, adapter, config_store, clock)
    config_store.update({"daily_loss_limit_usd": "1000"})  # keep the daily lock out of this test
    asyncio.run(engine._iteration())
    _buy_paper_position(engine, adapter, market)  # 16.6666 shares at 0.60
    up = market.asset_id(Outcome.UP)

    clock.advance(5)  # 12 of the 16.6666 shares sell at 0.10; the rest stays open
    adapter.set_book(up, bids=[("0.10", "12")], asks=[("0.15", "100")], timestamp=clock.now())
    asyncio.run(engine._manage_position(clock.now()))
    partial = repo.get_position(market.market_id, "PAPER")
    assert partial.status == "OPEN" and partial.shares == Decimal("4.6666")
    assert partial.realized_pnl < 0

    clock.advance(300)  # the market has ended: the remainder is held, not sold
    asyncio.run(engine._manage_position(clock.now()))
    assert repo.get_position(market.market_id, "PAPER").shares == Decimal("4.6666")

    engine._attempt_simulation = lambda machine, now: None
    adapter.resolutions[market.condition_id] = "UP"  # the remaining shares pay out in full
    clock.advance(11)
    asyncio.run(engine._settle_windows(clock.now()))
    settled = repo.get_position(market.market_id, "PAPER")
    assert settled.status == "SETTLED"
    settlement = next(t for t in repo.list_trades(limit=10) if t.kind == "SETTLEMENT")
    assert settlement.realized_pnl > 0  # the settlement leg alone made money...
    assert settled.realized_pnl < 0  # ...but the position as a whole lost
    day = repo.get_risk_day(START.date())
    assert (day.trades, day.wins, day.losses) == (1, 0, 1)
    assert repo.get_risk_state().consecutive_losses == 1
    assert day.realized_pnl == settled.realized_pnl


def test_engine_without_an_oracle_builds_its_own_twap_tracker(settings, repo, adapter, config_store):
    """``TwapTracker(60)`` passed the window positionally to a keyword-only constructor."""
    clock = FixedClock(START)
    engine = BotEngine(
        settings=settings,
        repo=repo,
        adapter=adapter,
        config_store=config_store,
        clock=clock,
        feeds={"binance": DummyFeed("binance", _series("binance")), "coinbase": DummyFeed("coinbase", _series("coinbase"))},
        oracle=None,
        recorder=DummyRecorder(),
        paper=PaperBroker(Decimal("100"), clock),
        start_api=False,
    )
    assert isinstance(engine.tracker, TwapTracker)
    assert engine.tracker.window_seconds == 60
