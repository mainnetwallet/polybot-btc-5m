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


# ------------------------------------------------------------------ dashboard proxy
import threading  # noqa: E402
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402


@pytest.fixture
def fake_dashboard():
    """A stand-in for the loopback dashboard: echoes requests, and speaks a toy WebSocket upgrade."""
    seen: list[dict] = []

    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _reply(self, status: int, body: bytes, ctype: str = "text/html") -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Set-Cookie", "polybot_session=abc; HttpOnly")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            seen.append({"method": "GET", "path": self.path, "host": self.headers.get("Host"),
                         "cookie": self.headers.get("Cookie")})
            if self.path.startswith("/api/stream"):
                self.send_response(101, "Switching Protocols")
                self.send_header("Upgrade", "websocket")
                self.send_header("Connection", "Upgrade")
                self.end_headers()
                self.wfile.flush()
                while True:
                    data = self.request.recv(1024)
                    if not data:
                        return
                    self.request.sendall(data.upper())
            self._reply(200, b"<html>dashboard</html>")

        do_HEAD = do_GET  # noqa: N815

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length)
            seen.append({"method": "POST", "path": self.path, "body": raw,
                         "csrf": self.headers.get("X-CSRF-Token")})
            self._reply(200, raw, "application/json")

        def log_message(self, *a):  # noqa: D401
            return

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield httpd.server_address[1], seen
    finally:
        httpd.shutdown()
        httpd.server_close()


def _proxy_server(upstream_port: int) -> HealthServer:
    server = HealthServer(
        "127.0.0.1", 0, HealthMonitor(_snap(), _lease()), upstream=("127.0.0.1", upstream_port)
    )
    assert server.start()
    return server


def test_proxy_serves_dashboard_at_root_and_keeps_health_local(fake_dashboard):
    upstream_port, seen = fake_dashboard
    server = _proxy_server(upstream_port)
    try:
        status, headers, body = _request(server.bound_port, "GET", "/")
        assert (status, body) == (200, b"<html>dashboard</html>")
        assert "polybot_session" in headers.get("Set-Cookie", "")

        status, _, body = _request(server.bound_port, "GET", "/static/app.js?v=1")
        assert status == 200 and seen[-1]["path"] == "/static/app.js?v=1"

        # Health stays answered locally and never reaches the dashboard.
        before = len(seen)
        for path in ("/healthz", "/healthz/", "/health"):
            status, _, body = _request(server.bound_port, "GET", path)
            assert status == 200
            assert json.loads(body)["service"] == "polybot"
        assert len(seen) == before

        # HEAD is proxied too.
        status, _, body = _request(server.bound_port, "HEAD", "/")
        assert status == 200 and body == b""
    finally:
        server.stop()


def test_proxy_forwards_post_body_headers_and_host(fake_dashboard):
    upstream_port, seen = fake_dashboard
    server = _proxy_server(upstream_port)
    try:
        conn = http.client.HTTPConnection("127.0.0.1", server.bound_port, timeout=5)
        payload = json.dumps({"username": "admin", "password": "p"}).encode()
        conn.request(
            "POST",
            "/api/login",
            body=payload,
            headers={"Content-Type": "application/json", "X-CSRF-Token": "tok", "Host": "bot.example.com"},
        )
        resp = conn.getresponse()
        assert resp.status == 200 and resp.read() == payload
        conn.close()
        post = [r for r in seen if r["method"] == "POST"][-1]
        assert post["body"] == payload and post["csrf"] == "tok"

        conn = http.client.HTTPConnection("127.0.0.1", server.bound_port, timeout=5)
        conn.request("GET", "/api/state", headers={"Host": "bot.example.com", "Cookie": "polybot_session=abc"})
        conn.getresponse().read()
        conn.close()
        get = [r for r in seen if r["method"] == "GET"][-1]
        # The dashboard's WebSocket origin check compares Origin to the *original* Host.
        assert get["host"] == "bot.example.com" and get["cookie"] == "polybot_session=abc"
    finally:
        server.stop()


def test_proxy_relays_websocket_upgrade(fake_dashboard):
    upstream_port, _ = fake_dashboard
    server = _proxy_server(upstream_port)
    try:
        with socket.create_connection(("127.0.0.1", server.bound_port), timeout=5) as sock:
            sock.sendall(
                b"GET /api/stream HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\n"
                b"Connection: Upgrade\r\nSec-WebSocket-Key: abc\r\nSec-WebSocket-Version: 13\r\n\r\n"
            )
            buf = b""
            while b"\r\n\r\n" not in buf:
                buf += sock.recv(1024)
            assert buf.startswith(b"HTTP/1.1 101")
            sock.sendall(b"ping")
            assert sock.recv(1024) == b"PING"
    finally:
        server.stop()


def test_proxy_returns_502_when_dashboard_is_down():
    port = _free_port()  # nothing listens here
    server = _proxy_server(port)
    try:
        status, _, body = _request(server.bound_port, "GET", "/")
        assert status == 502
        assert json.loads(body) == {"status": "dashboard_unavailable"}
        assert _request(server.bound_port, "GET", "/healthz")[0] == 200
    finally:
        server.stop()


def test_proxy_rejects_oversized_and_chunked_bodies(fake_dashboard):
    from btc5m_bot.healthserver import PROXY_MAX_BODY_BYTES

    upstream_port, seen = fake_dashboard
    server = _proxy_server(upstream_port)
    try:
        conn = http.client.HTTPConnection("127.0.0.1", server.bound_port, timeout=5)
        conn.putrequest("POST", "/api/login")
        conn.putheader("Content-Length", str(PROXY_MAX_BODY_BYTES + 1))
        conn.endheaders()
        assert conn.getresponse().status == 413
        conn.close()

        conn = http.client.HTTPConnection("127.0.0.1", server.bound_port, timeout=5)
        conn.putrequest("POST", "/api/login")
        conn.putheader("Transfer-Encoding", "chunked")
        conn.endheaders()
        assert conn.getresponse().status == 501
        conn.close()
        assert not [r for r in seen if r["method"] == "POST"]
    finally:
        server.stop()


def test_without_upstream_root_is_liveness_and_post_is_405(running_server):
    server = running_server(HealthMonitor(_snap(), _lease()))
    assert _request(server.bound_port, "GET", "/")[0] == 200
    assert _request(server.bound_port, "POST", "/api/login")[0] == 405


def test_public_dashboard_enabled_resolution(settings, monkeypatch):
    monkeypatch.delenv("RENDER", raising=False)
    assert settings.public_dashboard_enabled is False  # automatic mode is Render-only
    monkeypatch.setenv("RENDER", "true")
    assert settings.public_dashboard_enabled is True
    assert settings.model_copy(update={"public_dashboard": False}).public_dashboard_enabled is False
    # Never without a password hash, even if explicitly requested.
    bare = settings.model_copy(update={"dashboard_password_hash": None, "public_dashboard": True})
    assert bare.public_dashboard_enabled is False
    monkeypatch.delenv("RENDER", raising=False)
    assert settings.model_copy(update={"public_dashboard": True}).public_dashboard_enabled is True


def test_supervisor_proxies_dashboard_on_the_public_port(settings, monkeypatch, fake_dashboard):
    upstream_port, _ = fake_dashboard
    port = _free_port()
    settings = settings.model_copy(
        update={"health_host": "127.0.0.1", "health_port": port, "port": upstream_port, "public_dashboard": True}
    )
    seen: dict[str, tuple[int, bytes]] = {}

    def probe(_sup):
        for path in ("/", "/healthz"):
            status, _, body = _request(port, "GET", path)
            seen[path] = (status, body)

    sup = _fast_supervisor(settings, monkeypatch, on_sleep=probe)
    assert sup.run() == 0
    assert seen["/"] == (200, b"<html>dashboard</html>")
    assert seen["/healthz"][0] == 200
