"""Public health endpoints for Render and external uptime monitors (e.g. UptimeRobot).

The operator dashboard stays loopback-only (the engine refuses any other host). This module is a
separate, deliberately tiny HTTP listener that exposes *liveness and readiness only*:

* fixed status codes and short fixed reason codes,
* no trading state, no secrets, no internal exception text,
* GET and HEAD only, no authentication surface, no writes.

It runs in a daemon thread of the supervisor process, so a failure here (port already taken, a
bad request, a slow client) can never take the trading engine down.

Endpoints
---------
``GET|HEAD /healthz`` and ``/``
    Liveness. 200 as long as the supervisor process is serving. Point Render's
    ``healthCheckPath`` here: Render may restart a service whose health check keeps failing,
    and a trading bot must only restart on purpose.
``GET|HEAD /health``
    Readiness (deep). 200 only when the supervisor loop is running, the engine child process is
    alive and its liveness lease in SQLite is fresh. Otherwise 503 with a short ``reason`` code.
    Point UptimeRobot here so you are alerted when the engine is down or wedged.

Optional dashboard proxy
------------------------
On a host where only one public port exists (Render), the operator dashboard would be
unreachable. When an ``upstream`` is given, every path except ``/healthz`` and ``/health`` is
relayed byte-for-byte (HTTP and WebSocket upgrade) to the loopback dashboard. The dashboard keeps
doing its own authentication (password, signed session cookie, CSRF, login lockout); this
module adds no auth surface and no trading state of its own, and the dashboard still binds to
loopback only. ``/`` then belongs to the dashboard; liveness stays on ``/healthz``.
"""
from __future__ import annotations

import contextlib
import json
import logging
import selectors
import socket
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from .config import Settings

log = logging.getLogger(__name__)

SERVICE_NAME = "polybot"
LIVENESS_PATHS = frozenset({"/", "/healthz"})
READINESS_PATHS = frozenset({"/health"})
# Paths that are always answered locally, even when the dashboard proxy is on.
ALWAYS_LOCAL_PATHS = frozenset({"/healthz", "/health"})

# Dashboard proxy limits. The dashboard only ever receives small JSON bodies.
PROXY_MAX_BODY_BYTES = 1024 * 1024
PROXY_MAX_CONCURRENT = 32
PROXY_CONNECT_TIMEOUT_SECONDS = 3.0
PROXY_IO_TIMEOUT_SECONDS = 60.0
# The dashboard pushes over its WebSocket at least every ~2 s, so a long silence means a dead peer.
PROXY_WS_IDLE_SECONDS = 120.0
_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "content-length",
    }
)

# The supervisor refreshes its snapshot every couple of seconds. If the snapshot is older than
# this, the supervisor loop itself is wedged.
SNAPSHOT_MAX_AGE_SECONDS = 30.0
# Readiness is cached briefly so a burst of requests cannot hammer SQLite.
CACHE_TTL_SECONDS = 2.0
# Per-connection socket timeout: a slow or silent client cannot pin a handler thread.
REQUEST_TIMEOUT_SECONDS = 5.0

SnapshotProvider = Callable[[], dict[str, Any]]
LeaseCheck = Callable[[], "tuple[str | None, float | None]"]


def make_lease_check(settings: Settings) -> LeaseCheck:
    """Build a read-only check of the engine liveness lease.

    Returns ``(reason_code, age_seconds)``; ``reason_code`` is ``None`` when the lease is fresh.
    The database engine is created lazily and never creates or migrates the schema: the engine
    process owns that, and a missing table simply reports as not ready.
    """
    from .db.engine import make_engine, make_session_factory
    from .db.repo import Repository
    from .heartbeat.worker import LEASE_KEY

    guard = threading.Lock()
    holder: dict[str, Any] = {}

    def _repo() -> Repository | None:
        with guard:
            if "repo" not in holder:
                if not settings.uses_external_database and not settings.db_path.exists():
                    return None
                engine = make_engine(settings.database_target)
                holder["engine"] = engine
                holder["repo"] = Repository(make_session_factory(engine))
            return holder["repo"]

    def check() -> tuple[str | None, float | None]:
        try:
            repo = _repo()
            if repo is None:
                return "engine_lease_missing", None
            state = repo.get_state(LEASE_KEY, {})
        except Exception:  # noqa: BLE001 - health must never raise; report not-ready instead
            return "lease_unavailable", None
        if not state:
            return "engine_lease_missing", None
        try:
            ts = datetime.fromisoformat(str(state.get("ts")))
        except ValueError:
            return "engine_lease_invalid", None
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - ts).total_seconds()
        if not state.get("healthy", False):
            return "engine_unhealthy", age
        if age < -5:
            return "engine_lease_invalid", age
        if age > settings.health_max_lease_age_seconds:
            return "engine_lease_stale", age
        return None, age

    return check


class HealthMonitor:
    """Turns the supervisor snapshot plus the engine lease into HTTP status + JSON payloads."""

    def __init__(
        self,
        snapshot_provider: SnapshotProvider,
        lease_check: LeaseCheck,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._snapshot = snapshot_provider
        self._lease_check = lease_check
        self._monotonic = monotonic
        self._started = monotonic()
        self._lock = threading.Lock()
        self._cached: tuple[float, tuple[int, dict[str, Any]]] | None = None

    def _uptime(self) -> int:
        return int(self._monotonic() - self._started)

    def liveness(self) -> tuple[int, dict[str, Any]]:
        return 200, {"status": "ok", "service": SERVICE_NAME, "check": "liveness", "uptime_s": self._uptime()}

    def readiness(self) -> tuple[int, dict[str, Any]]:
        now = self._monotonic()
        with self._lock:
            if self._cached is not None and now - self._cached[0] < CACHE_TTL_SECONDS:
                return self._cached[1]
            result = self._evaluate()
            self._cached = (now, result)
            return result

    def _evaluate(self) -> tuple[int, dict[str, Any]]:
        payload: dict[str, Any] = {
            "service": SERVICE_NAME,
            "check": "readiness",
            "uptime_s": self._uptime(),
        }
        reason = None
        try:
            snap = self._snapshot()
        except Exception:  # noqa: BLE001
            snap = {}
            reason = "supervisor_unavailable"
        state = str(snap.get("state", "UNKNOWN"))
        payload["supervisor"] = state
        payload["engine_alive"] = bool(snap.get("engine_alive", False))

        if reason is None and float(snap.get("age_s", 0.0)) > SNAPSHOT_MAX_AGE_SECONDS:
            reason = "supervisor_stalled"
        elif reason is None and state != "RUNNING":
            reason = "supervisor_not_running"
        elif reason is None and not payload["engine_alive"]:
            reason = "engine_process_down"
        if reason is None:
            lease_reason, age = self._lease_check()
            if age is not None:
                payload["engine_lease_age_s"] = round(age, 1)
            reason = lease_reason

        if reason is None:
            payload["status"] = "ok"
            return 200, payload
        payload["status"] = "unavailable"
        payload["reason"] = reason
        return 503, payload


def _make_handler(
    monitor: HealthMonitor, upstream: "tuple[str, int] | None" = None
) -> type[BaseHTTPRequestHandler]:
    proxy_slots = threading.BoundedSemaphore(PROXY_MAX_CONCURRENT)

    class _Handler(BaseHTTPRequestHandler):
        server_version = "polybot-health"
        sys_version = ""
        timeout = REQUEST_TIMEOUT_SECONDS

        def _send(self, status: int, payload: dict[str, Any], *, body: bool, allow: str | None = None) -> None:
            data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Connection", "close")
            if allow:
                self.send_header("Allow", allow)
            self.end_headers()
            if body:
                self.wfile.write(data)

        def _proxy(self) -> None:
            """Relay this request to the loopback dashboard (HTTP, or a WebSocket upgrade)."""
            assert upstream is not None
            if self.headers.get("Transfer-Encoding"):
                self._send(501, {"status": "not_implemented"}, body=True)
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length < 0:
                self._send(400, {"status": "bad_request"}, body=True)
                return
            if length > PROXY_MAX_BODY_BYTES:
                self._send(413, {"status": "payload_too_large"}, body=True)
                return
            if not proxy_slots.acquire(blocking=False):
                self._send(503, {"status": "busy"}, body=True)
                return
            try:
                self._proxy_locked(length)
            finally:
                proxy_slots.release()

        def _proxy_locked(self, length: int) -> None:
            assert upstream is not None
            upgrade = (
                "upgrade" in self.headers.get("Connection", "").lower()
                and self.headers.get("Upgrade", "").lower() == "websocket"
            )
            payload = self.rfile.read(length) if length else b""
            lines = [f"{self.command} {self.path} HTTP/1.1"]
            for key, value in self.headers.items():
                lowered = key.lower()
                if lowered in _HOP_BY_HOP and not (upgrade and lowered == "upgrade"):
                    continue
                lines.append(f"{key}: {value}")
            lines.append("Connection: Upgrade" if upgrade else "Connection: close")
            if payload or self.command in ("POST", "PUT", "PATCH"):
                lines.append(f"Content-Length: {len(payload)}")
            try:
                head = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")
            except UnicodeEncodeError:
                self._send(400, {"status": "bad_request"}, body=True)
                return
            try:
                remote = socket.create_connection(upstream, timeout=PROXY_CONNECT_TIMEOUT_SECONDS)
            except OSError:
                self._send(502, {"status": "dashboard_unavailable"}, body=True)
                return
            self.close_connection = True
            client = self.connection
            try:
                with remote:
                    remote.settimeout(PROXY_IO_TIMEOUT_SECONDS)
                    client.settimeout(PROXY_IO_TIMEOUT_SECONDS)
                    remote.sendall(head + payload)
                    if upgrade:
                        self._relay(client, remote)
                    else:
                        while True:
                            chunk = remote.recv(65536)
                            if not chunk:
                                break
                            self.wfile.write(chunk)
            except (OSError, ValueError):
                # Peer went away or timed out mid-stream. Nothing useful left to send.
                log.debug("dashboard proxy stream ended", exc_info=True)

        @staticmethod
        def _relay(client: socket.socket, remote: socket.socket) -> None:
            """Pump bytes both ways until either side closes or the link goes idle."""
            with selectors.DefaultSelector() as selector:
                selector.register(client, selectors.EVENT_READ, remote)
                selector.register(remote, selectors.EVENT_READ, client)
                while True:
                    events = selector.select(timeout=PROXY_WS_IDLE_SECONDS)
                    if not events:
                        return
                    for key, _ in events:
                        data = key.fileobj.recv(65536)  # type: ignore[union-attr]
                        if not data:
                            return
                        key.data.sendall(data)

        def _route(self, *, body: bool) -> None:
            path = self.path.split("?", 1)[0].split("#", 1)[0]
            if len(path) > 1:
                path = path.rstrip("/")
            if upstream is not None and path not in ALWAYS_LOCAL_PATHS:
                try:
                    self._proxy()
                except Exception:  # noqa: BLE001 - never leak internals to a public listener
                    log.exception("dashboard proxy failed")
                    self.close_connection = True
                return
            try:
                if path in LIVENESS_PATHS:
                    status, payload = monitor.liveness()
                elif path in READINESS_PATHS:
                    status, payload = monitor.readiness()
                else:
                    status, payload = 404, {"status": "not_found"}
            except Exception:  # noqa: BLE001 - never leak internals to a public listener
                log.exception("health request failed")
                status, payload = 500, {"status": "error"}
            self._send(status, payload, body=body)

        def do_GET(self) -> None:  # noqa: N802 - http.server API
            self._route(body=True)

        def do_HEAD(self) -> None:  # noqa: N802
            self._route(body=False)

        def _other_method(self) -> None:
            path = self.path.split("?", 1)[0].split("#", 1)[0]
            if len(path) > 1:
                path = path.rstrip("/")
            if upstream is not None and path not in ALWAYS_LOCAL_PATHS:
                self._route(body=True)
            else:
                self._send(405, {"status": "method_not_allowed"}, body=True, allow="GET, HEAD")

        do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _other_method  # noqa: N815

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            # Monitors poll every few seconds; keep access noise out of the structured logs.
            return

    return _Handler


class HealthServer:
    """Owns the listener thread. ``start()`` returns False instead of raising on bind failure."""

    def __init__(
        self,
        host: str,
        port: int,
        monitor: HealthMonitor,
        upstream: "tuple[str, int] | None" = None,
    ) -> None:
        self.host = host
        self.port = port
        self._monitor = monitor
        self._upstream = upstream
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def bound_port(self) -> int | None:
        return self._httpd.server_address[1] if self._httpd is not None else None

    def start(self) -> bool:
        try:
            httpd = ThreadingHTTPServer((self.host, self.port), _make_handler(self._monitor, self._upstream))
        except OSError as exc:
            log.error("health server could not bind %s:%s: %s", self.host, self.port, exc)
            return False
        httpd.daemon_threads = True
        self._httpd = httpd
        self._thread = threading.Thread(
            target=httpd.serve_forever,
            kwargs={"poll_interval": 0.5},
            name="polybot-health",
            daemon=True,
        )
        self._thread.start()
        if self._upstream is not None:
            log.info(
                "public listener on %s:%s: /healthz, /health, dashboard proxied to %s:%s",
                self.host,
                httpd.server_address[1],
                *self._upstream,
            )
        else:
            log.info("health server listening on %s:%s (/healthz, /health)", self.host, httpd.server_address[1])
        return True

    def stop(self) -> None:
        httpd, thread = self._httpd, self._thread
        self._httpd = self._thread = None
        if httpd is None:
            return
        with contextlib.suppress(Exception):
            httpd.shutdown()
        with contextlib.suppress(Exception):
            httpd.server_close()
        if thread is not None:
            thread.join(timeout=5)
