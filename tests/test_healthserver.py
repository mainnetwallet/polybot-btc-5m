"""Public health listener: liveness/readiness semantics, HTTP behaviour, supervisor wiring."""
from __future__ import annotations

import http.client
import json
import socket
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from btc5m_bot import supervisor
from btc5m_bot.healthserver import (
    CACHE_TTL_SECONDS,
    SNAPSHOT_MAX_AGE_SECONDS,
    HealthMonitor,
    HealthServer,
    make_lease_check,
)
from btc5m_bot.heartbeat.worker import LEASE_KEY


def _snap(state="RUNNING", engine_alive=True, age_s=0.0):
    return lambda: {"state": state, "engine_alive": engine_alive, "age_s": age_s}


def _lease(reason=None, age=1.0):
    return lambda: (reason, age)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _request(port: int, method: str, path: str):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request(method, path)
        resp = conn.getresponse()
        return resp.status, dict(resp.getheaders()), resp.read()
    finally:
        conn.close()


@pytest.fixture
def running_server():
    servers: list[HealthServer] = []

    def _start(monitor: HealthMonitor) -> HealthServer:
        server = HealthServer("127.0.0.1", 0, monitor)
        assert server.start()
        servers.append(server)
        return server

    yield _start
    for server in servers:
        server.stop()


# ------------------------------------------------------------------ monitor logic
def test_liveness_is_always_ok():
    status, body = HealthMonitor(_snap(engine_alive=False), _lease("engine_unhealthy")).liveness()
    assert status == 200
    assert body["status"] == "ok" and body["check"] == "liveness"


def test_readiness_ok_when_running_and_lease_fresh():
    status, body = HealthMonitor(_snap(), _lease(None, 2.34)).readiness()
    assert status == 200
    assert body["status"] == "ok"
    assert body["engine_lease_age_s"] == 2.3


@pytest.mark.parametrize(
    ("snapshot", "lease", "reason"),
    [
        (_snap(state="STARTING"), _lease(), "supervisor_not_running"),
        (_snap(state="STOPPING"), _lease(), "supervisor_not_running"),
        (_snap(engine_alive=False), _lease(), "engine_process_down"),
        (_snap(age_s=SNAPSHOT_MAX_AGE_SECONDS + 1), _lease(), "supervisor_stalled"),
        (_snap(), _lease("engine_lease_stale", 99.0), "engine_lease_stale"),
        (_snap(), _lease("engine_unhealthy", 1.0), "engine_unhealthy"),
        (_snap(), _lease("engine_lease_missing", None), "engine_lease_missing"),
    ],
)
def test_readiness_unavailable_reasons(snapshot, lease, reason):
    status, body = HealthMonitor(snapshot, lease).readiness()
    assert status == 503
    assert body["status"] == "unavailable"
    assert body["reason"] == reason


def test_readiness_survives_a_failing_snapshot_provider():
    def boom():
        raise RuntimeError("secret internal detail")

    status, body = HealthMonitor(boom, _lease()).readiness()
    assert status == 503
    assert body["reason"] == "supervisor_unavailable"
    assert "secret" not in json.dumps(body)


def test_readiness_is_cached_briefly():
    now = [100.0]
    calls = []

    def lease():
        calls.append(1)
        return None, 1.0

    monitor = HealthMonitor(_snap(), lease, monotonic=lambda: now[0])
    monitor.readiness()
    monitor.readiness()
    assert len(calls) == 1
    now[0] += CACHE_TTL_SECONDS + 0.1
    monitor.readiness()
    assert len(calls) == 2


# ------------------------------------------------------------------ real lease check
def _write_lease(repo, *, age_seconds: float, healthy: bool = True, detail: str = "") -> None:
    ts = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    repo.set_state(LEASE_KEY, {"ts": ts.isoformat(), "pid": 1, "healthy": healthy, "detail": detail})


def test_lease_check_without_database(settings):
    assert not settings.db_path.exists()
    assert make_lease_check(settings)() == ("engine_lease_missing", None)


def test_lease_check_states(settings, repo):
    check = make_lease_check(settings)
    assert check()[0] == "engine_lease_missing"

    _write_lease(repo, age_seconds=1)
    reason, age = check()
    assert reason is None and age is not None and 0 <= age < 10

    _write_lease(repo, age_seconds=settings.health_max_lease_age_seconds + 30)
    assert check()[0] == "engine_lease_stale"

    # The engine's own exception text must never be echoed to a public endpoint.
    _write_lease(repo, age_seconds=1, healthy=False, detail="Traceback: private key ...")
    reason, _ = check()
    assert reason == "engine_unhealthy"


# ------------------------------------------------------------------ HTTP behaviour
def test_http_get_and_head(running_server):
    server = running_server(HealthMonitor(_snap(), _lease()))
    port = server.bound_port

    status, headers, body = _request(port, "GET", "/healthz")
    assert status == 200 and json.loads(body)["status"] == "ok"
    assert headers["Content-Type"] == "application/json"
    assert headers["Cache-Control"] == "no-store"

    status, _, body = _request(port, "GET", "/health?source=uptimerobot")
    assert status == 200 and json.loads(body)["check"] == "readiness"

    # UptimeRobot's default free monitor uses HEAD: same status and length, empty body.
    status, headers, body = _request(port, "HEAD", "/health")
    assert status == 200 and body == b""
    assert int(headers["Content-Length"]) > 0

    assert _request(port, "GET", "/")[0] == 200
    assert _request(port, "GET", "/health/")[0] == 200


def test_http_readiness_503_and_routing_errors(running_server):
    server = running_server(HealthMonitor(_snap(engine_alive=False), _lease()))
    port = server.bound_port

    status, _, body = _request(port, "GET", "/health")
    assert status == 503 and json.loads(body)["reason"] == "engine_process_down"
    assert _request(port, "HEAD", "/health")[0] == 503
    assert _request(port, "GET", "/healthz")[0] == 200  # liveness is independent of the engine

    for path in ("/api/health", "/api/state", "/static/app.js", "/.env"):
        assert _request(port, "GET", path)[0] == 404

    status, headers, _ = _request(port, "POST", "/health")
    assert status == 405 and headers["Allow"] == "GET, HEAD"
    assert _request(port, "DELETE", "/health")[0] == 405


def test_start_returns_false_when_port_is_taken():
    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen()
        port = blocker.getsockname()[1]
        assert HealthServer("127.0.0.1", port, HealthMonitor(_snap(), _lease())).start() is False


# ------------------------------------------------------------------ settings
def test_health_listen_port_resolution(settings, monkeypatch):
    monkeypatch.delenv("PORT", raising=False)
    monkeypatch.delenv("RENDER", raising=False)
    assert settings.health_listen_port is None

    # A stray PORT outside Render must not open a public listener.
    monkeypatch.setenv("PORT", "10000")
    assert settings.health_listen_port is None

    monkeypatch.setenv("RENDER", "true")
    assert settings.health_listen_port == 10000

    monkeypatch.setenv("PORT", "not-a-port")
    assert settings.health_listen_port is None

    # An explicit setting always wins.
    assert settings.model_copy(update={"health_port": 8080}).health_listen_port == 8080


# ------------------------------------------------------------------ supervisor wiring
def _fast_supervisor(settings, monkeypatch, on_sleep=None):
    sup = supervisor.Supervisor(settings)
    monkeypatch.setattr(sup, "_spawn", lambda *a, **k: None)

    def fake_sleep(_seconds):
        if on_sleep is not None:
            on_sleep(sup)
        sup._running = False

    monkeypatch.setattr(
        supervisor, "time", SimpleNamespace(monotonic=supervisor.time.monotonic, sleep=fake_sleep)
    )
    return sup


def test_supervisor_serves_health_while_running(settings, monkeypatch):
    port = _free_port()
    settings = settings.model_copy(update={"health_host": "127.0.0.1", "health_port": port})
    seen: dict[str, tuple[int, bytes]] = {}

    def probe(_sup):
        for path in ("/healthz", "/health"):
            status, _, body = _request(port, "GET", path)
            seen[path] = (status, body)

    sup = _fast_supervisor(settings, monkeypatch, on_sleep=probe)
    assert sup.run() == 0

    assert seen["/healthz"][0] == 200
    assert seen["/health"][0] == 503  # no engine child in this test
    assert json.loads(seen["/health"][1])["reason"] == "engine_process_down"
    # The listener is torn down with the supervisor.
    with pytest.raises(OSError):
        _request(port, "GET", "/healthz")


def test_supervisor_survives_a_taken_health_port(settings, monkeypatch):
    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen()
        port = blocker.getsockname()[1]
        settings = settings.model_copy(update={"health_host": "127.0.0.1", "health_port": port})
        sup = _fast_supervisor(settings, monkeypatch)
        assert sup.run() == 0
        assert sup._health_server is None


def test_supervisor_refuses_health_port_equal_to_dashboard_port(settings, monkeypatch):
    settings = settings.model_copy(update={"health_port": settings.port})
    sup = _fast_supervisor(settings, monkeypatch)
    assert sup.run() == 0
    assert sup._health_server is None


def test_supervisor_snapshot_reflects_children(settings, monkeypatch):
    sup = supervisor.Supervisor(settings)
    assert sup.health_snapshot()["state"] == "STARTING"

    sup._children["engine"] = SimpleNamespace(pid=123, is_alive=lambda: True)
    sup._write_status("RUNNING")
    snap = sup.health_snapshot()
    assert snap["state"] == "RUNNING" and snap["engine_alive"] is True
    assert 0 <= snap["age_s"] < 5

    sup._children["engine"] = SimpleNamespace(pid=123, is_alive=lambda: False)
    sup._write_status("RUNNING")
    assert sup.health_snapshot()["engine_alive"] is False


def test_supervisor_clears_stale_stop_file(settings, monkeypatch):
    stop = settings.data_path / "STOP"
    stop.write_text("left over from the previous run\n", encoding="utf-8")
    sup = _fast_supervisor(settings, monkeypatch)
    assert sup.run() == 0
    assert not stop.exists()
