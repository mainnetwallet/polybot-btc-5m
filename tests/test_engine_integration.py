"""Offline engine iteration tests: no network, no funds, no heartbeat."""
from __future__ import annotations

import asyncio
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

from btc5m_bot.clock import FixedClock
from btc5m_bot.data.series import BtcSeries, BtcTick
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
    assert engine._last_decision["reason"] == ReasonCode.RULES_UNAPPROVED.value
    assert repo.get_window(market.market_id) is not None


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


def test_approved_rule_still_blocks_without_allocated_capital(settings, repo, adapter, config_store):
    clock = FixedClock(START)
    engine, _, spec = _build(settings, repo, adapter, config_store, clock)

    asyncio.run(engine._iteration())
    repo.approve_resolution_rule(spec.fingerprint, "test-operator")
    asyncio.run(engine._iteration())

    assert engine._last_decision is not None
    assert engine._last_decision["decision"] == "NO_TRADE"
    assert engine._last_decision["reason"] == ReasonCode.CAPITAL_UNSET.value
    assert adapter.placed == []


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
