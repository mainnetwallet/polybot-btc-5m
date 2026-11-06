from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from btc5m_bot.analytics.calibration import CalibrationPrediction, calibration_report
from btc5m_bot.analytics.performance import compute_performance, daily_performance
from btc5m_bot.analytics.validation import build_validation_report
from btc5m_bot.api.app import create_app
from btc5m_bot.config import ConfigStore, RuntimeConfig
from btc5m_bot.gate import GateCondition, GateReport
from btc5m_bot.oracle.twap import TwapObservation, TwapTracker

from conftest import START


def trade(pnl, *, kind="SETTLEMENT", price="0.50", shares="10", fee="0.05", day=0):
    return SimpleNamespace(
        realized_pnl=Decimal(pnl),
        fee=Decimal(fee),
        price=Decimal(price),
        shares=Decimal(shares),
        kind=kind,
        ts=START + timedelta(days=day),
        market_id=f"mkt-{pnl}-{day}",
        outcome="UP",
        side="SETTLE",
    )


def test_performance_uses_realized_closing_trades_only():
    rows = [
        trade("5.00", day=0),
        trade("-2.00", day=1),
        trade("1.00", day=1),
        trade("99.00", kind="ENTRY", day=2),
    ]
    summary = compute_performance(rows)
    assert summary.trades == 3
    assert summary.wins == 2 and summary.losses == 1
    assert summary.net_pnl == Decimal("4.00")
    assert summary.gross_profit == Decimal("6.00")
    assert summary.gross_loss == Decimal("2.00")
    assert summary.profit_factor == pytest.approx(3.0)
    assert summary.max_drawdown == Decimal("2.00")
    assert len(daily_performance(rows)) == 2


def test_performance_is_empty_without_closed_trades():
    summary = compute_performance([trade("9", kind="ENTRY")])
    assert summary.trades == 0
    assert summary.win_rate is None
    assert summary.expectancy is None
    assert summary.net_pnl == 0


def test_calibration_brier_and_bins():
    predictions = [
        CalibrationPrediction("a", 0.8, True),
        CalibrationPrediction("b", 0.6, False),
    ]
    report = calibration_report(predictions)
    assert report.count == 2
    assert report.brier == pytest.approx(0.2)
    assert report.log_loss is not None and report.log_loss > 0
    assert sum(b["count"] for b in report.bins) == 2


def test_calibration_without_samples_is_explicitly_insufficient():
    report = calibration_report([])
    assert report.count == 0
    assert report.brier is None
    assert "insufficient" in report.note


def test_validation_fails_honestly_on_empty_database(repo):
    report = build_validation_report(repo, RuntimeConfig(paper_validation_min_trades=300))
    assert report.passed is False
    assert any("paper trades" in f for f in report.failures)
    assert any("resolution simulator match rate" in f for f in report.failures)
    assert report.metrics["paper_performance"]["trades"] == 0
    assert report.metrics["bootstrap_expectancy"]["low"] is None
    assert any("PAPER trades only" in n for n in report.notes)


class StubRisk:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.reset_result: dict | None = None

    def engage_kill_switch(self, reason, *, actor="system", detail=None):
        self.calls.append(("engage", reason))

    def clear_kill_switch(self, *, actor, reason):
        self.calls.append(("clear", reason))

    def reset_day_lock(self, *, actor, reason):
        self.calls.append(("reset", reason))
        return self.reset_result


class StubEngine:
    def __init__(self) -> None:
        self.risk = StubRisk()
        self.tracker = TwapTracker(window_seconds=60)
        self.state = SimpleNamespace(
            snapshot=lambda: {
                "mode": "PAPER",
                "private_key": "0x" + "a" * 64,
                "clob_api_secret": "super-secret",
                "nested": {"session_secret": "hide-me"},
                "equity": "1000.00",
            }
        )
        self.activations: list[str] = []
        self.revocations: list[str] = []

    async def refresh_gate(self):
        return GateReport(
            conditions=(GateCondition(1, "explicit_activation", "Explicit user activation", True),),
            checked_at=datetime.now(timezone.utc),
        )

    async def activate_live(self, *, typed_phrase, actor):
        self.activations.append(typed_phrase)
        return GateReport(
            conditions=(GateCondition(1, "explicit_activation", "Explicit user activation",
                                      typed_phrase == "ACTIVATE LIVE TRADING"),),
            checked_at=datetime.now(timezone.utc),
        )

    async def revoke_live(self, *, actor, reason):
        self.revocations.append(reason)

    async def arm_heartbeat_test(self, *, typed_phrase, actor):
        return {"ok": False, "detail": "LIVE not active"}


@pytest.fixture
def client(settings, repo, config_store):
    engine = StubEngine()
    app = create_app(engine=engine, repo=repo, settings=settings, config_store=config_store)
    with TestClient(app) as test_client:
        yield test_client, engine, config_store


def login(client) -> dict[str, str]:
    response = client.post("/api/login", json={"username": "admin", "password": "correct-horse-battery"})
    assert response.status_code == 200
    return {"x-csrf-token": response.json()["csrf"]}


def test_dashboard_requires_authentication_and_sets_security_headers(client):
    test_client, engine, _ = client
    assert test_client.get("/api/state").status_code == 401
    assert test_client.post("/api/login", json={"username": "admin", "password": "wrong"}).status_code == 401
    page = test_client.get("/")
    assert page.status_code == 200
    assert "default-src 'self'" in page.headers["content-security-policy"]
    assert page.headers["x-frame-options"] == "DENY"
    assert page.headers["cache-control"] == "no-store"
    assert 'id="cfg-momz"' in page.text
    assert 'id="cfg-liqdrop"' in page.text
    script = test_client.get("/static/app.js")
    assert script.status_code == 200
    for field in ("momentum_reversal_z", "liquidity_exit_depth_drop_pct", "dynamic_take_profit"):
        assert field in script.text


def test_state_is_redacted_and_csrf_is_enforced(client):
    test_client, engine, _ = client
    headers = login(test_client)
    state = test_client.get("/api/state")
    assert state.status_code == 200
    payload = state.json()["state"]
    assert payload["private_key"] == "***REDACTED***"
    assert payload["clob_api_secret"] == "***REDACTED***"
    assert payload["nested"]["session_secret"] == "***REDACTED***"
    assert "a" * 64 not in state.text

    assert test_client.post("/api/control/kill", json={"phrase": "KILL"}).status_code == 403
    denied = test_client.post("/api/control/kill", json={"phrase": "kill"}, headers=headers)
    assert denied.status_code == 400
    assert engine.risk.calls == []


def test_operator_controls_require_typed_phrases(client):
    test_client, engine, _ = client
    headers = login(test_client)
    assert test_client.post("/api/control/kill", json={"phrase": "KILL", "reason": "drill"},
                            headers=headers).status_code == 200
    assert test_client.post("/api/control/kill", json={"phrase": "CLEAR KILL", "action": "clear"},
                            headers=headers).status_code == 200
    assert test_client.post("/api/live/revoke", json={"phrase": "REVOKE LIVE"}, headers=headers).status_code == 200
    assert engine.risk.calls == [("engage", "drill"), ("clear", "")]
    assert engine.revocations == ["operator"]


def test_reset_day_reports_whether_the_lock_will_come_back(client):
    test_client, engine, _ = client
    headers = login(test_client)
    engine.risk.reset_result = {
        "was_locked": True, "will_relock": True, "daily_pnl": "-1.60", "daily_loss_limit": "1.50",
    }
    res = test_client.post("/api/control/reset-day", json={"phrase": "RESET DAY"}, headers=headers)
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True and body["will_relock"] is True
    assert body["daily_pnl"] == "-1.60"


def test_live_activation_requires_the_exact_phrase(client):
    test_client, engine, _ = client
    headers = login(test_client)
    response = test_client.post("/api/live/activate", json={"phrase": "activate"}, headers=headers)
    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert response.json()["gate"]["passed_count"] == 0

    accepted = test_client.post("/api/live/activate", json={"phrase": "ACTIVATE LIVE TRADING"}, headers=headers)
    assert accepted.status_code == 200
    assert accepted.json()["ok"] is True
    assert engine.activations == ["activate", "ACTIVATE LIVE TRADING"]


def test_config_is_allowlisted_and_hard_limits_rejected(client):
    test_client, _, store = client
    headers = login(test_client)
    bad = test_client.post("/api/config", json={"patch": {"mode": "LIVE"}}, headers=headers)
    assert bad.status_code == 400
    hard = test_client.post("/api/config", json={"patch": {"risk_per_trade_pct": "0.05"}}, headers=headers)
    assert hard.status_code == 400
    assert "2% cap" in hard.json()["detail"]
    before = store.revision
    good = test_client.post("/api/config", json={"patch": {"min_probability": "0.65"}}, headers=headers)
    assert good.status_code == 200
    assert store.revision == before + 1
    assert store.snapshot().min_probability == Decimal("0.65")

    exits = test_client.post(
        "/api/config",
        json={
            "patch": {
                "dynamic_take_profit": False,
                "dynamic_tp_min_multiple": "0.70",
                "dynamic_tp_max_multiple": "1.40",
                "momentum_reversal_z": "1.80",
                "min_hold_seconds_before_reversal": 12,
                "liquidity_exit_depth_drop_pct": "0.50",
                "liquidity_exit_spread_multiple": "2.50",
            }
        },
        headers=headers,
    )
    assert exits.status_code == 200
    cfg = store.snapshot()
    assert cfg.dynamic_take_profit is False
    assert cfg.dynamic_tp_min_multiple == Decimal("0.70")
    assert cfg.dynamic_tp_max_multiple == Decimal("1.40")
    assert cfg.momentum_reversal_z == Decimal("1.80")
    assert cfg.min_hold_seconds_before_reversal == 12
    assert cfg.liquidity_exit_depth_drop_pct == Decimal("0.50")
    assert cfg.liquidity_exit_spread_multiple == Decimal("2.50")

    invalid = test_client.post(
        "/api/config", json={"patch": {"liquidity_exit_depth_drop_pct": "1.20"}}, headers=headers
    )
    assert invalid.status_code == 400


def test_analytics_and_gate_endpoints_return_backend_state(client):
    test_client, _, _ = client
    headers = login(test_client)
    analytics = test_client.get("/api/analytics", headers=headers)
    assert analytics.status_code == 200
    body = analytics.json()["analytics"]
    assert body["paper"]["trades"] == 0
    assert body["validation"]["passed"] is False
    gate = test_client.get("/api/gate", headers=headers)
    assert gate.status_code == 200
    assert gate.json()["gate"]["passed"] is True


def test_stream_requires_login_rejects_foreign_origin_and_pushes_redacted_state(client):
    from starlette.websockets import WebSocketDisconnect

    test_client, _engine, _store = client
    with pytest.raises(WebSocketDisconnect):
        with test_client.websocket_connect("/api/stream"):
            pass  # no session cookie yet

    login(test_client)
    with pytest.raises(WebSocketDisconnect):
        with test_client.websocket_connect("/api/stream", headers={"origin": "http://evil.example"}):
            pass

    with test_client.websocket_connect("/api/stream") as ws:
        message = ws.receive_json()
    assert message["ok"] is True
    assert message["state"]["mode"] == "PAPER"
    assert "server_time" in message
    assert "a" * 64 not in str(message), "secrets must be redacted on the stream too"


def test_chart_endpoint_returns_recent_chainlink_ticks_only(client):
    test_client, engine, _ = client
    assert test_client.get("/api/chart").status_code == 401
    login(test_client)

    now = datetime.now(timezone.utc)
    recent = now - timedelta(seconds=30)
    latest = now - timedelta(seconds=5)
    for ts, value, source in (
        (now - timedelta(seconds=400), "1.00", "chainlink"),      # older than the window
        (recent, "75000.50", "chainlink"),
        (now - timedelta(seconds=10), "75001.25", "binance"),     # not the settlement source
        (latest, "75002.75", "chainlink"),
    ):
        engine.tracker.add(TwapObservation(ts=ts, value=Decimal(value), source=source))

    response = test_client.get("/api/chart?seconds=180")
    assert response.status_code == 200
    assert response.json()["points"] == [
        [int(recent.timestamp() * 1000), 75000.5],
        [int(latest.timestamp() * 1000), 75002.75],
    ]


def test_dashboard_serves_the_market_hero(client):
    test_client, _, _ = client
    page = test_client.get("/")
    for element_id in ("hero", "hero-svg", "px-beat", "px-now", "px-delta", "cd-min", "cd-sec", "past-btn", "hero-wins"):
        assert f'id="{element_id}"' in page.text
    assert '<script src="/static/hero.js">' in page.text
    assert test_client.get("/static/hero.js").status_code == 200


def test_state_store_version_changes_on_every_update():
    from btc5m_bot.state import EngineStateStore

    store = EngineStateStore()
    v0 = store.version
    store.update(detail="x")
    v1 = store.version
    store.increment("decisions")
    assert v0 < v1 < store.version


def test_activity_clear_deletes_on_click_without_typed_phrase(client, repo):
    test_client, _, _ = client
    repo.log_decision(market_id="m1", decision="NO_TRADE", reason_code="CAPITAL_UNSET")
    headers = login(test_client)

    unknown = test_client.post("/api/activity/clear", json={"action": "risk"}, headers=headers)
    assert unknown.status_code == 400
    assert len(repo.recent_decisions(10)) == 1

    ok = test_client.post("/api/activity/clear", json={"action": "decisions"}, headers=headers)
    assert ok.status_code == 200 and ok.json()["removed"] == 1
    assert repo.recent_decisions(10) == []


def test_config_api_returns_400_for_out_of_range_tunables(client):
    test_client, _, store = client
    headers = login(test_client)
    before = store.snapshot().cooldown_minutes
    for patch in ({"cooldown_minutes": 0}, {"trailing_stop_pct": "1.5"}, {"min_oracle_coverage_seconds": 0}):
        response = test_client.post("/api/config", json={"patch": patch}, headers=headers)
        assert response.status_code == 400, patch
        assert "invalid configuration" in response.json()["detail"]
    assert store.snapshot().cooldown_minutes == before


# ---------------------------------------------------------------- strict JSON
# profit_factor used to be math.inf when a run had no losing trade (and for all-flat runs). inf is
# not valid JSON. The HTTP endpoints hide it (pydantic turns inf into null) but the websocket
# stream uses json.dumps, which wrote a literal ``Infinity`` the browser's JSON.parse rejects.
@pytest.mark.parametrize("pnls", [["5.00"], ["5.00", "1.00"], ["0", "0"]])
def test_performance_summary_is_strict_json_without_losses(pnls):
    import json

    summary = compute_performance([trade(p, day=i) for i, p in enumerate(pnls)])
    assert summary.profit_factor is None  # undefined without a loss, never infinity
    json.dumps(summary.as_dict(), allow_nan=False)


def test_stream_pushes_strict_json_when_analytics_has_no_losses(client):
    import json

    def reject_constant(name):  # what a browser's JSON.parse does with Infinity / NaN
        raise ValueError(f"non-standard JSON constant {name}")

    test_client, engine, _ = client
    login(test_client)
    analytics = {"paper": compute_performance([trade("5.00")]).as_dict()}
    engine.state = SimpleNamespace(snapshot=lambda: {"mode": "PAPER", "analytics": analytics})
    with test_client.websocket_connect("/api/stream") as ws:
        raw = ws.receive_text()
    message = json.loads(raw, parse_constant=reject_constant)
    assert message["state"]["analytics"]["paper"]["profit_factor"] is None


def test_statistical_gate_threshold_is_not_dashboard_writable(client):
    # paper_validation_min_trades feeds the LIVE statistical gate. Lowering it to 1 from the
    # dashboard would let a handful of trades pass validation, so it is config-file only.
    test_client, _engine, store = client
    headers = login(test_client)
    before = store.snapshot().paper_validation_min_trades
    response = test_client.post("/api/config", json={"patch": {"paper_validation_min_trades": 1}}, headers=headers)
    assert response.status_code == 400
    assert "paper_validation_min_trades" in response.json()["detail"]
    assert store.snapshot().paper_validation_min_trades == before
    assert "paper_validation_min_trades" not in test_client.get("/static/app.js").text
