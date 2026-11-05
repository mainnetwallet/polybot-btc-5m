from __future__ import annotations

import asyncio
from datetime import timedelta
from decimal import Decimal

import pytest

from btc5m_bot.exchange.base import Balance, ExchangeOpenOrder, GeoResult
from btc5m_bot.execution.journal import OrderJournal
from btc5m_bot.gate import LIVE_CONFIRMATION_PHRASE, GateContext, LiveGate
from btc5m_bot.heartbeat.worker import (
    HeartbeatWorker,
    LeaseManager,
    validate_heartbeat_configuration,
)
from btc5m_bot.recovery import RecoveryManager
from btc5m_bot.types import Mode, OrderStatus, Outcome, RecoveryState, Side

from conftest import START, make_market


def test_heartbeat_configuration_requires_every_safety_condition(settings):
    ready = settings.model_copy(
        update={
            "heartbeat_enabled": True,
            "heartbeat_interval_seconds": 5,
            "heartbeat_lease_seconds": 8,
            "heartbeat_dedicated_key_confirmed": True,
        }
    )
    check = validate_heartbeat_configuration(
        ready, live_mode=True, activation_active=True, dedicated_key_confirmed=True, test_recorded=True
    )
    assert check.ok, check.failures

    rejected = validate_heartbeat_configuration(
        ready, live_mode=False, activation_active=True, dedicated_key_confirmed=True, test_recorded=True
    )
    assert not rejected.ok
    assert any("live_mode" in f for f in rejected.failures)

    untested = validate_heartbeat_configuration(
        ready, live_mode=True, activation_active=True, dedicated_key_confirmed=True, test_recorded=False
    )
    assert not untested.ok

    no_key = validate_heartbeat_configuration(
        ready, live_mode=True, activation_active=True, dedicated_key_confirmed=False, test_recorded=True
    )
    assert not no_key.ok
    assert any("dedicated_key" in f for f in no_key.failures)

    bad_interval = settings.model_copy(update={"heartbeat_enabled": True, "heartbeat_interval_seconds": 2})
    assert not validate_heartbeat_configuration(
        bad_interval, live_mode=True, activation_active=True, dedicated_key_confirmed=True, test_recorded=True
    ).ok


def test_lease_must_be_fresh_healthy_and_not_from_the_future(clock, repo):
    lease = LeaseManager(repo, clock)
    assert lease.valid(max_age_seconds=8)[0] is False
    lease.renew(engine_pid=1)
    assert lease.valid(max_age_seconds=8)[0] is True
    lease.invalidate("engine died", engine_pid=1)
    assert lease.valid(max_age_seconds=8)[0] is False
    lease.renew(engine_pid=1)
    state = lease.read()
    state["ts"] = (START + timedelta(seconds=60)).isoformat()
    repo.set_state("engine_lease", state)
    ok, detail = lease.valid(max_age_seconds=8)
    assert not ok and "future" in detail


def test_heartbeat_never_sends_when_live_is_not_authorised(clock, repo, settings, adapter):
    lease = LeaseManager(repo, clock)
    lease.renew(engine_pid=42)
    worker = HeartbeatWorker(
        repo=repo,
        settings=settings.model_copy(update={"heartbeat_enabled": True}),
        clock=clock,
        lease=lease,
        adapter_factory=lambda: adapter,
        live_allowed=lambda: False,
    )

    async def run_briefly() -> None:
        await worker.start()
        await asyncio.sleep(0.05)
        await worker.stop()

    asyncio.run(run_briefly())
    assert adapter.heartbeat_ids == []
    assert repo.get_state("heartbeat")["ok"] is False


def test_heartbeat_requires_a_valid_lease_then_rotates_id(clock, repo, settings, adapter):
    lease = LeaseManager(repo, clock)
    worker = HeartbeatWorker(
        repo=repo,
        settings=settings.model_copy(update={"heartbeat_enabled": True}),
        clock=clock,
        lease=lease,
        adapter_factory=lambda: adapter,
        live_allowed=lambda: True,
    )
    asyncio.run(worker._send_once())
    assert adapter.heartbeat_ids == []
    assert repo.get_state("heartbeat_skipped")["reason"] == "no engine lease"

    lease.renew(engine_pid=7)
    asyncio.run(worker._send_once())
    assert adapter.heartbeat_ids[0] == "hb-0001"
    assert repo.get_heartbeat_state().armed is True
    asyncio.run(worker._send_once())
    assert len(adapter.heartbeat_ids) == 2
    assert adapter.heartbeat_ids[1] != adapter.heartbeat_ids[0]
    assert repo.get_heartbeat_state().consecutive_failures == 0


def test_heartbeat_failure_is_recorded_not_raised(clock, repo, settings):
    lease = LeaseManager(repo, clock)
    lease.renew(engine_pid=7)

    class FailingAdapter:
        heartbeat_ids: list[str] = []

        async def send_heartbeat(self, heartbeat_id: str):
            raise RuntimeError("transport down")

    worker = HeartbeatWorker(
        repo=repo,
        settings=settings.model_copy(update={"heartbeat_enabled": True}),
        clock=clock,
        lease=lease,
        adapter_factory=lambda: FailingAdapter(),
        live_allowed=lambda: True,
    )
    for _ in range(3):
        asyncio.run(worker._send_once())
    state = repo.get_heartbeat_state()
    assert state.consecutive_failures == 3
    assert "transport down" in state.last_error
    assert repo.get_state("heartbeat_failure")["consecutive_failures"] == 3


def test_recovery_blocks_on_database_integrity_failure(clock, repo, adapter):
    recovery = RecoveryManager(
        repo=repo, adapter=adapter, clock=clock, mode=Mode.PAPER,
        integrity_check=lambda: (_ for _ in ()).throw(RuntimeError("corrupt")),
    )
    result = asyncio.run(recovery.run())
    assert result.ready is False
    assert result.state is RecoveryState.BLOCKED
    assert any("integrity" in b for b in result.blocks)
    assert repo.get_recovery_state().state == RecoveryState.BLOCKED.value


def test_recovery_rejects_presubmit_journal_without_guessing(clock, repo, adapter):
    market = make_market()
    journal = OrderJournal(repo)
    intent = journal.new_intent(
        market=market, outcome=Outcome.UP, side=Side.BUY, shares=Decimal("10"),
        limit_price=Decimal("0.50"), mode="PAPER",
    )
    journal.persist_intent(intent, reserve_amount=Decimal("5"))
    recovery = RecoveryManager(repo=repo, adapter=adapter, clock=clock, mode=Mode.PAPER)
    result = asyncio.run(recovery.run())
    assert result.ready is True
    row = repo.get_order(intent.client_order_id)
    assert row.status == OrderStatus.REJECTED.value
    assert row.error_code == "RECOVERED_PRE_SUBMIT"
    assert repo.active_reserved_total() == 0


def test_recovery_blocks_paper_order_that_reached_submission(clock, repo, adapter):
    market = make_market()
    journal = OrderJournal(repo)
    intent = journal.new_intent(
        market=market, outcome=Outcome.UP, side=Side.BUY, shares=Decimal("10"),
        limit_price=Decimal("0.50"), mode="PAPER",
    )
    journal.persist_intent(intent, reserve_amount=Decimal("5"))
    journal.mark_submitting(intent)
    result = asyncio.run(RecoveryManager(repo=repo, adapter=adapter, clock=clock, mode=Mode.PAPER).run())
    assert result.ready is False
    assert any("cannot be verified" in b for b in result.blocks)


def test_recovery_blocks_untracked_exchange_order(clock, repo, adapter):
    adapter.open_orders["ex-1"] = ExchangeOpenOrder(
        order_id="ex-1", asset_id="up-token", condition_id="cond-mkt-1",
        side="BUY", price=Decimal("0.50"), original_size=Decimal("10"),
        size_matched=Decimal("0"), status="live",
    )
    result = asyncio.run(RecoveryManager(repo=repo, adapter=adapter, clock=clock, mode=Mode.LIVE).run())
    assert result.ready is False
    assert any("untracked working order" in b for b in result.blocks)


def _passing_context(**overrides) -> GateContext:
    values = dict(
        activation_requested=True,
        geo=GeoResult(blocked=False, country="GB", detail="eligible"),
        wallet_auth_verified=True,
        signature_type=0,
        signature_type_verified=True,
        funder_verified=True,
        balance=Balance(available=Decimal("500"), total=Decimal("500"), allowance=Decimal("500")),
        capital=Decimal("500"),
        allowance_required=Decimal("100"),
        allowance_ok=True,
        reconciliation_clean=True,
        recovery_ready=True,
        feeds_healthy=True,
        rules_verified=True,
        rules_approved=True,
        resolution_simulator_active=True,
        exposure_monitor_active=True,
        disaster_recovery_active=True,
        risk_manager_active=True,
        protective_shutdown_resolved=True,
        protocol_version_ok=True,
        min_buy_feasible=True,
        min_sell_feasible=True,
        heartbeat_configured=True,
        heartbeat_tested=True,
        statistical_validation_passed=True,
    )
    values.update(overrides)
    return GateContext(**values)


def test_live_gate_has_twenty_three_conditions_and_fails_closed(repo):
    gate = LiveGate(repo)
    report = gate.evaluate(GateContext())
    assert len(report.conditions) == 23
    assert report.passed is False
    assert report.passed_count == 0
    assert {c.number for c in report.failed} == set(range(1, 24))


def test_live_gate_requires_explicit_typed_confirmation(repo):
    gate = LiveGate(repo)
    rejected = gate.activate(_passing_context(), typed_phrase="yes", actor="tester")
    assert rejected.passed is False
    assert any(c.key == "final_confirmation" for c in rejected.failed)
    assert gate.activation_active() is False

    passed = gate.activate(_passing_context(), typed_phrase=LIVE_CONFIRMATION_PHRASE, actor="tester")
    assert passed.passed is True
    assert gate.activation_active() is True
    gate.revoke(actor="tester", reason="test complete")
    assert gate.activation_active() is False


def test_live_gate_blocks_unsafe_geo_balance_and_heartbeat(repo):
    gate = LiveGate(repo)
    assert any(c.key == "geographic_eligibility" and not c.passed
               for c in gate.evaluate(_passing_context(geo=GeoResult(blocked=None))).conditions)
    assert any(c.key == "geographic_eligibility" and not c.passed
               for c in gate.evaluate(_passing_context(geo=GeoResult(blocked=True))).conditions)
    assert any(c.key == "pusd_balance" and not c.passed
               for c in gate.evaluate(_passing_context(balance=Balance(available=Decimal("1"), total=Decimal("1")))).conditions)
    assert any(c.key == "allowance" and not c.passed
               for c in gate.evaluate(_passing_context(allowance_ok=False)).conditions)
    assert any(c.key == "heartbeat" and not c.passed
               for c in gate.evaluate(_passing_context(heartbeat_tested=False)).conditions)
    assert any(c.key == "recovery_ready" and not c.passed
               for c in gate.evaluate(_passing_context(recovery_ready=False)).conditions)
