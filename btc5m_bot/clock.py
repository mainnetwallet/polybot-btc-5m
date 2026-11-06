"""Injectable clock so tests never depend on wall time or sleeps."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...
    def monotonic(self) -> float: ...
    async def sleep(self, seconds: float) -> None: ...


class RealClock:
    """Wall clock disciplined against the exchange time endpoint."""

    def __init__(self) -> None:
        self._offset = timedelta(0)

    def now(self) -> datetime:
        return datetime.now(timezone.utc) + self._offset

    def sync_to(self, server_time: datetime, *, max_drift_seconds: float = 5.0) -> bool:
        """Apply a bounded exchange-time correction; reject implausible local clock drift."""
        raw_now = datetime.now(timezone.utc)
        server_time = server_time.astimezone(timezone.utc)
        offset = server_time - raw_now
        if abs(offset.total_seconds()) > max_drift_seconds:
            return False
        self._offset = offset
        return True

    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        import asyncio

        await asyncio.sleep(seconds)


class FixedClock:
    """Deterministic clock for tests and simulators."""

    def __init__(self, start: datetime | str | None = None) -> None:
        if start is None:
            dt = datetime(2026, 1, 1, tzinfo=timezone.utc)
        elif isinstance(start, str):
            dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
        else:
            dt = start
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        self._now = dt.astimezone(timezone.utc)
        self._mono = 0.0

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._mono

    async def sleep(self, seconds: float) -> None:
        self.advance(seconds)

    def advance(self, seconds: float) -> datetime:
        self._now = self._now + timedelta(seconds=seconds)
        self._mono += seconds
        return self._now

    def set(self, dt: datetime) -> None:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        self._now = dt.astimezone(timezone.utc)
