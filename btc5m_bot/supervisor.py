"""Process supervision with crash-loop protection and single-instance locking."""
from __future__ import annotations

import contextlib
import json
import logging
import multiprocessing as mp
import os
import signal
import sys
import time
import traceback
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Deque

from .clock import RealClock
from .config import Settings, load_settings
from .db.engine import init_db, make_engine, make_session_factory, single_instance_lock
from .db.repo import Repository
from .healthserver import HealthMonitor, HealthServer, make_lease_check
from .heartbeat import LeaseManager

log = logging.getLogger(__name__)

POLL_SECONDS = 2.0


def heartbeat_process_main(settings_dict: dict) -> None:
    import asyncio

    from .config import Settings as _Settings
    from .db.engine import init_db as _init_db
    from .db.engine import make_engine as _make_engine
    from .db.engine import make_session_factory as _make_session_factory
    from .db.repo import Repository as _Repository
    from .exchange.sdk_adapter import PolymarketAdapter
    from .heartbeat import HeartbeatWorker, LeaseManager
    from .logging_setup import setup_logging

    settings = _Settings(**settings_dict)
    setup_logging(settings.logs_path, component="heartbeat")
    engine = _make_engine(settings.db_path)
    _init_db(engine)
    repo = _Repository(_make_session_factory(engine))
    clock = RealClock()
    lease = LeaseManager(repo, clock)

    def live_allowed() -> bool:
        if repo.active_live_activation() is None:
            return False
        try:
            runtime = json.loads(settings.runtime_config_file.read_text(encoding="utf-8"))
        except Exception:
            return False
        return str(runtime.get("mode", "PAPER")).upper() == "LIVE"

    worker = HeartbeatWorker(
        repo=repo,
        settings=settings,
        clock=clock,
        lease=lease,
        adapter_factory=lambda: PolymarketAdapter(settings),
        live_allowed=live_allowed,
    )

    async def _run() -> None:
        await worker.start()
        try:
            while worker._task is not None and not worker._task.done():
                await asyncio.sleep(1.0)
        finally:
            await worker.stop()
            if worker._adapter is not None:
                await worker._adapter.close()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


class Supervisor:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.ctx = mp.get_context("spawn")
        self.clock = RealClock()
        self._restarts: Deque[float] = deque()
        self._children: dict[str, mp.Process] = {}
        self._stop_file = settings.data_path / "STOP"
        self._running = True
        self._engine_pid: int | None = None
        self._health_server: HealthServer | None = None
        # Replaced wholesale on every status write so the health thread never touches
        # multiprocessing objects (Process.is_alive() is not meant to be shared across threads).
        self._snapshot: dict[str, object] = {"state": "STARTING", "engine_alive": False, "mono": time.monotonic()}

    # ------------------------------------------------------------------ status
    def health_snapshot(self) -> dict[str, object]:
        snap = self._snapshot
        return {
            "state": snap["state"],
            "engine_alive": snap["engine_alive"],
            "age_s": time.monotonic() - float(snap["mono"]),  # type: ignore[arg-type]
        }

    def _start_health_server(self) -> None:
        port = self.settings.health_listen_port
        if port is None:
            return
        if port == self.settings.port:
            log.error("health port %s collides with the dashboard port; health server not started", port)
            return
        monitor = HealthMonitor(self.health_snapshot, make_lease_check(self.settings))
        # On Render the health port is the only public port, so the password-protected dashboard
        # is reached through it (proxied to the loopback dashboard). Health paths stay local.
        upstream = (self.settings.host, self.settings.port) if self.settings.public_dashboard_enabled else None
        server = HealthServer(self.settings.health_host, port, monitor, upstream)
        # A health listener problem must never stop the trading supervisor.
        if server.start():
            self._health_server = server

    def _write_status(self, state: str, detail: str = "") -> None:
        children = {name: p.pid for name, p in self._children.items() if p.is_alive()}
        self._snapshot = {"state": state, "engine_alive": "engine" in children, "mono": time.monotonic()}
        payload = {
            "state": state,
            "detail": detail,
            "at": datetime.now(timezone.utc).isoformat(),
            "children": children,
        }
        try:
            self.settings.data_path.mkdir(parents=True, exist_ok=True)
            tmp = self.settings.data_path / "supervisor.json.tmp"
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            os.replace(tmp, self.settings.data_path / "supervisor.json")
        except Exception:
            log.exception("failed to write supervisor status")

    def _register_restart(self) -> bool:
        now = time.monotonic()
        hour_ago = now - 3600
        while self._restarts and self._restarts[0] < hour_ago:
            self._restarts.popleft()
        if len(self._restarts) >= self.settings.supervisor_max_restarts_per_hour:
            return False
        self._restarts.append(now)
        return True

    def _spawn(self, name: str, target, *args) -> None:
        process = self.ctx.Process(target=target, args=args, name=f"polybot-{name}", daemon=False)
        process.start()
        self._children[name] = process
        log.info("spawned %s pid=%s", name, process.pid)

    def _terminate(self, name: str) -> None:
        process = self._children.get(name)
        if process is None:
            return
        if process.is_alive():
            process.terminate()
            process.join(timeout=10)
            if process.is_alive():
                process.kill()
                process.join(timeout=5)
        self._children.pop(name, None)

    # -------------------------------------------------------------------- run
    def run(self) -> int:
        from .engine import engine_process_main

        lock_path = self.settings.data_path / self.settings.single_instance_lock
        with single_instance_lock(lock_path):
            self._install_signals()
            # A STOP file is a request to a *running* supervisor. One left over from a previous
            # run would make every restart exit immediately, which on a managed host (Render)
            # becomes a silent restart loop. We hold the single-instance lock here, so this
            # cannot cancel a stop requested of another live supervisor.
            if self._stop_file.exists():
                log.warning("clearing stale STOP file from a previous run: %s", self._stop_file)
                with contextlib.suppress(OSError):
                    self._stop_file.unlink()
            self._write_status("STARTING")
            self._start_health_server()
            heartbeat_enabled = self.settings.heartbeat_enabled
            if heartbeat_enabled:
                # Heartbeat must only exist in LIVE configurations; it never sends until
                # active LIVE authorisation and a fresh engine lease are both present.
                self._spawn("heartbeat", heartbeat_process_main, self.settings.model_dump())
            self._spawn("engine", engine_process_main, self.settings.model_dump())
            try:
                while self._running:
                    if self._stop_file.exists():
                        log.warning("STOP file detected; shutting down")
                        break
                    for name in list(self._children):
                        process = self._children[name]
                        if process.is_alive():
                            continue
                        exitcode = process.exitcode
                        log.error("%s exited with code %s", name, exitcode)
                        self._terminate(name)
                        if not self._register_restart():
                            log.critical("crash-loop protection tripped; supervisor stopping")
                            self._write_status("STOPPED", f"{name} crashed repeatedly")
                            return 2
                        # On engine restart the lease is stale, so the heartbeat worker
                        # cannot send until the restarted engine renews it.
                        self._spawn(name, engine_process_main if name == "engine" else heartbeat_process_main, self.settings.model_dump())
                    self._write_status("RUNNING")
                    time.sleep(POLL_SECONDS)
            finally:
                self._shutdown()
                if self._health_server is not None:
                    self._health_server.stop()
            self._write_status("STOPPED")
        return 0

    def _shutdown(self) -> None:
        self._write_status("STOPPING")
        # Stop the heartbeat first so no send can outlive a shutting-down engine.
        self._terminate("heartbeat")
        self._terminate("engine")

    def _install_signals(self) -> None:
        def _handler(signum, frame):  # type: ignore[no-untyped-def]
            log.warning("signal %s received; shutting down", signum)
            self._running = False

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, _handler)
            except (ValueError, OSError):
                pass


def main() -> int:
    settings = load_settings()
    from .logging_setup import setup_logging

    setup_logging(settings.logs_path, component="supervisor")
    try:
        return Supervisor(settings).run()
    except Exception:
        log.error("supervisor failed\n%s", traceback.format_exc())
        return 1


if __name__ == "__main__":
    sys.exit(main())
