"""Heartbeat worker and engine liveness lease.

The Polymarket heartbeat has irreversible semantics: the first accepted send arms a
key-wide timeout-cancel-all for every resting order under the API key, and there is
no disarm operation. POLYBOT therefore only ever sends when LIVE is explicitly
authorised, the engine lease is fresh, and a dedicated API key is configured.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Awaitable, Callable

from ..clock import Clock
from ..config import Settings
from ..db.repo import Repository
from ..exchange.base import ExchangeAdapter, HeartbeatResult
from ..types import Mode, as_utc

log = logging.getLogger(__name__)

LEASE_KEY = "engine_lease"


class LeaseManager:
    """Engine liveness lease persisted in SQLite so the worker can verify it."""

    def __init__(self, repo: Repository, clock: Clock) -> None:
        self.repo = repo
        self.clock = clock

    def renew(self, *, engine_pid: int, healthy: bool = True, detail: str = "") -> None:
        now = as_utc(self.clock.now())
        self.repo.set_state(
            LEASE_KEY,
            {
                "ts": now.isoformat(),
                "pid": int(engine_pid),
                "healthy": bool(healthy),
                "detail": detail,
            },
        )

    def invalidate(self, reason: str, *, engine_pid: int | None = None) -> None:
        self.repo.set_state(
            LEASE_KEY,
            {
                "ts": as_utc(self.clock.now()).isoformat(),
                "pid": engine_pid,
                "healthy": False,
                "detail": reason,
            },
        )

    def read(self) -> dict:
        return self.repo.get_state(LEASE_KEY, {})

    def valid(self, *, max_age_seconds: float, now: datetime | None = None) -> tuple[bool, str]:
        state = self.read()
        if not state:
            return False, "no engine lease"
        if not state.get("healthy", False):
            return False, f"engine marked unhealthy: {state.get('detail', '')}"
        try:
            ts = as_utc(datetime.fromisoformat(str(state.get("ts"))))
        except Exception:
            return False, "unparseable engine lease"
        now = as_utc(now or self.clock.now())
        age = (now - ts).total_seconds()
        if age > max_age_seconds:
            return False, f"engine lease stale ({age:.1f}s > {max_age_seconds:.1f}s)"
        if age < -5:
            return False, "engine lease timestamp is in the future"
        return True, f"lease fresh ({age:.1f}s)"


@dataclass(frozen=True)
class HeartbeatSafetyCheck:
    checks: tuple[tuple[str, bool, str], ...]

    @property
    def ok(self) -> bool:
        return all(passed for _, passed, _ in self.checks)

    @property
    def failures(self) -> list[str]:
        return [f"{name}: {detail}" for name, passed, detail in self.checks if not passed]


def validate_heartbeat_configuration(
    settings: Settings,
    *,
    live_mode: bool,
    activation_active: bool,
    dedicated_key_confirmed: bool,
    test_recorded: bool,
) -> HeartbeatSafetyCheck:
    creds_present = all(
        secret is not None and secret.get_secret_value()
        for secret in (settings.clob_api_key, settings.clob_api_secret, settings.clob_api_passphrase)
    )
    interval_ok = 3 <= settings.heartbeat_interval_seconds <= 6
    lease_ok = 0 < settings.heartbeat_lease_seconds < 10
    checks = (
        ("heartbeat_enabled", settings.heartbeat_enabled, "POLYBOT_HEARTBEAT_ENABLED must be true"),
        ("live_mode", live_mode, "runtime mode must be LIVE"),
        ("live_activation", activation_active, "no active LIVE activation"),
        ("credentials_present", creds_present, "CLOB API credentials are incomplete"),
        ("dedicated_key", dedicated_key_confirmed, "dedicated heartbeat API key not confirmed"),
        ("interval", interval_ok, f"send interval {settings.heartbeat_interval_seconds}s must be 3-6s"),
        ("lease_window", lease_ok, f"lease {settings.heartbeat_lease_seconds}s must be < 10s deadline"),
        ("heartbeat_test", test_recorded, "no successful heartbeat test recorded"),
    )
    return HeartbeatSafetyCheck(checks)


@dataclass
class HeartbeatWorker:
    repo: Repository
    settings: Settings
    clock: Clock
    lease: LeaseManager
    adapter_factory: Callable[[], ExchangeAdapter]
    live_allowed: Callable[[], bool]

    _task: asyncio.Task[None] | None = None
    _adapter: ExchangeAdapter | None = None
    _stop: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def state(self):
        return self.repo.get_heartbeat_state()

    def _record_attempt(self) -> None:
        self.repo.update_heartbeat_state(last_attempt_at=as_utc(self.clock.now()))

    def _record_success(self, result: HeartbeatResult) -> None:
        now = as_utc(self.clock.now())
        self.repo.update_heartbeat_state(
            heartbeat_id=result.heartbeat_id,
            armed=True,
            last_success_at=now,
            consecutive_failures=0,
            last_error=None,
        )
        self.repo.set_state(
            "heartbeat",
            {"ok": True, "last_success_at": now.isoformat(), "heartbeat_id": result.heartbeat_id},
        )

    def _record_failure(self, error: str) -> int:
        state = self.repo.get_heartbeat_state()
        failures = state.consecutive_failures + 1
        self.repo.update_heartbeat_state(consecutive_failures=failures, last_error=error[:500])
        if failures >= 3:
            self.repo.set_state(
                "heartbeat_failure",
                {
                    "at": as_utc(self.clock.now()).isoformat(),
                    "consecutive_failures": failures,
                    "error": error[:500],
                },
            )
            self.repo.add_protection_event(
                "HEARTBEAT_FAILURE", "CRITICAL", consecutive_failures=failures, error=error[:500]
            )
        return failures

    async def _send_once(self) -> None:
        if self._adapter is None:
            self._adapter = self.adapter_factory()
        valid, detail = self.lease.valid(max_age_seconds=self.settings.heartbeat_lease_seconds)
        if not valid:
            # Do not send on a stale lease. Sending would keep the platform's cancel-all
            # deadline alive for an engine that is not demonstrably healthy.
            self.repo.set_state(
                "heartbeat_skipped",
                {"at": as_utc(self.clock.now()).isoformat(), "reason": detail},
            )
            log.warning("heartbeat skipped: %s", detail)
            return
        heartbeat_id = self.repo.get_heartbeat_state().heartbeat_id or ""
        self._record_attempt()
        try:
            result = await asyncio.wait_for(
                self._adapter.send_heartbeat(heartbeat_id),
                timeout=min(4.0, self.settings.heartbeat_interval_seconds),
            )
            self._record_success(result)
            log.debug("heartbeat accepted; id rotated")
        except Exception as exc:  # noqa: BLE001 - transport failures must be recorded, not raised
            failures = self._record_failure(str(exc))
            log.error("heartbeat failed (%d consecutive): %s", failures, exc)

    async def run(self) -> None:
        # Idle without exiting: a supervisor restart loop must never be triggered
        # by the heartbeat being disabled/unarmed, and LIVE may be activated later.
        while not self._stop.is_set() and not self.live_allowed():
            self.repo.set_state(
                "heartbeat",
                {
                    "ok": False,
                    "detail": "LIVE not authorised; heartbeat never sends in PAPER/dev",
                    "at": as_utc(self.clock.now()).isoformat(),
                },
            )
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass
        if self._stop.is_set():
            return
        log.warning("heartbeat worker ACTIVE: first accepted send arms key-wide cancel-all")
        while not self._stop.is_set():
            if not self.live_allowed():
                # Revocation stops sends. The platform deadline may then cancel resting
                # orders, which is the intended fail-safe for a revoked LIVE session.
                # Stay resident and idle so an operator can re-activate without a restart.
                while not self._stop.is_set() and not self.live_allowed():
                    self.repo.set_state(
                        "heartbeat",
                        {"ok": False, "detail": "LIVE authorisation revoked; sends stopped", "at": as_utc(self.clock.now()).isoformat()},
                    )
                    try:
                        await asyncio.wait_for(self._stop.wait(), timeout=2.0)
                    except asyncio.TimeoutError:
                        pass
                if self._stop.is_set():
                    return
                log.warning("heartbeat worker resuming after LIVE re-activation")
                continue
            await self._send_once()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.settings.heartbeat_interval_seconds)
            except asyncio.TimeoutError:
                pass

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self.run(), name="heartbeat-worker")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        if self._adapter is not None:
            await self._adapter.close()
            self._adapter = None
