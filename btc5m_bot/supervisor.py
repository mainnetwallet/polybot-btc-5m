"""Process supervision with crash-loop protection and single-instance locking."""
from __future__ import annotations

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

    # ------------------------------------------------------------------ status
    def _write_status(self, state: str, detail: str = "") -> None:
        payload = {
            "state": state,
            "detail": detail,
            "at": datetime.now(timezone.utc).isoformat(),
            "children": {name: p.pid for name, p in self._children.items() if p.is_alive()},
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
            self._write_status("STARTING")
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
