"""Chainlink TWAP ingestion via PolyBolt. Chainlink only; no silent substitution."""
from __future__ import annotations

import asyncio
import bisect
import json
import logging
import math
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable

import websockets

from ..config import Settings
from ..types import FeedHealth, FeedStatus, as_utc

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class TwapObservation:
    ts: datetime
    value: Decimal
    source: str
    symbol: str = "btcusd"
    window_seconds: int = 60
    sequence: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "ts", as_utc(self.ts))

    @property
    def is_chainlink(self) -> bool:
        return "chainlink" in (self.source or "").lower()


class TwapTracker:
    def __init__(self, *, window_seconds: int = 60, maxlen: int = 10_000) -> None:
        self.window_seconds = window_seconds
        self._obs: list[TwapObservation] = []
        self._maxlen = maxlen

    def __len__(self) -> int:
        return len(self._obs)

    @property
    def observations(self) -> tuple[TwapObservation, ...]:
        return tuple(self._obs)

    def add(self, obs: TwapObservation) -> None:
        if self._obs and obs.ts < self._obs[-1].ts:
            keys = [o.ts for o in self._obs]
            idx = bisect.bisect_right(keys, obs.ts)
            self._obs.insert(idx, obs)
        else:
            self._obs.append(obs)
        if len(self._obs) > self._maxlen:
            self._obs = self._obs[-self._maxlen :]

    def extend(self, items: Iterable[TwapObservation]) -> None:
        for item in items:
            self.add(item)

    def latest(self, now: datetime | None = None) -> TwapObservation | None:
        if not self._obs:
            return None
        if now is None:
            return self._obs[-1]
        cutoff = as_utc(now)
        idx = bisect.bisect_right([o.ts for o in self._obs], cutoff) - 1
        return self._obs[idx] if idx >= 0 else None

    def value_at(self, ts: datetime, *, max_gap_seconds: float = 15.0) -> Decimal | None:
        obs = self.latest(ts)
        if obs is None:
            return None
        if (as_utc(ts) - obs.ts).total_seconds() > max_gap_seconds:
            return None
        return obs.value

    def opening_reference(self, start: datetime, *, tolerance_seconds: float = 20.0) -> Decimal | None:
        return self.value_at(start, max_gap_seconds=tolerance_seconds)

    def observations_between(self, start: datetime, end: datetime) -> list[TwapObservation]:
        start, end = as_utc(start), as_utc(end)
        return [o for o in self._obs if start <= o.ts <= end]

    def coverage_seconds(self, start: datetime, end: datetime, *, max_gap_seconds: float = 10.0) -> float:
        items = self.observations_between(start, end)
        if len(items) < 2:
            return 0.0
        covered = 0.0
        for a, b in zip(items, items[1:]):
            gap = (b.ts - a.ts).total_seconds()
            if gap <= max_gap_seconds:
                covered += gap
        return covered

    def health(
        self,
        now: datetime,
        *,
        max_age_ms: int = 5000,
        require_chainlink: bool = True,
    ) -> FeedStatus:
        now = as_utc(now)
        latest = self.latest(now)
        if latest is None:
            return FeedStatus(name="chainlink_twap", health=FeedHealth.UNVERIFIED, detail="no TWAP observations")
        age_ms = max(0, int((now - latest.ts).total_seconds() * 1000))
        detail: list[str] = []
        health = FeedHealth.HEALTHY
        if require_chainlink and not latest.is_chainlink:
            return FeedStatus(
                name="chainlink_twap",
                health=FeedHealth.UNVERIFIED,
                last_tick_at=latest.ts,
                last_value=latest.value,
                age_ms=age_ms,
                detail=f"provider {latest.source!r} is not Chainlink",
            )
        if latest.window_seconds != self.window_seconds:
            return FeedStatus(
                name="chainlink_twap",
                health=FeedHealth.UNVERIFIED,
                last_tick_at=latest.ts,
                last_value=latest.value,
                age_ms=age_ms,
                detail=f"window {latest.window_seconds}s != required {self.window_seconds}s",
            )
        if age_ms > max_age_ms * 2:
            return FeedStatus(
                name="chainlink_twap",
                health=FeedHealth.DOWN,
                last_tick_at=latest.ts,
                last_value=latest.value,
                age_ms=age_ms,
                detail=f"stale {age_ms} ms",
            )
        if age_ms > max_age_ms:
            health = FeedHealth.STALE
            detail.append(f"age {age_ms} ms")
        return FeedStatus(
            name="chainlink_twap",
            health=health,
            last_tick_at=latest.ts,
            last_value=latest.value,
            age_ms=age_ms,
            detail="; ".join(detail) if detail else "ok",
        )

    def projected_final_twap(
        self,
        now: datetime,
        end: datetime,
        *,
        drift_per_second: float = 0.0,
        max_gap_seconds: float = 10.0,
    ) -> Decimal | None:
        """Transparent projection of the settlement TWAP.

        The final reference is the trailing `window_seconds` TWAP at the end boundary.
        The known portion (already observed) is time-weighted; the unknown future portion
        is projected from the current TWAP with a capped momentum drift. No future data
        is used.
        """
        now, end = as_utc(now), as_utc(end)
        current = self.latest(now)
        if current is None:
            return None
        if now >= end:
            return self.value_at(end, max_gap_seconds=max_gap_seconds)
        window = float(max(self.window_seconds, 1))
        window_start = end - timedelta(seconds=window)
        p = float(current.value)
        g = max(-0.001, min(0.001, drift_per_second))
        area = 0.0
        if now > window_start:
            known_seconds = (now - window_start).total_seconds()
            items = [o for o in self.observations_between(window_start, now) if o.is_chainlink]
            future_span = min(window, (end - now).total_seconds())
            drift_from = known_seconds
            if not items:
                area = p * known_seconds
            else:
                cursor = window_start
                first = items[0]
                if first.ts > cursor:
                    area += p * (first.ts - cursor).total_seconds()
                    cursor = first.ts
                for a, b in zip(items, items[1:]):
                    gap = (b.ts - a.ts).total_seconds()
                    if a.ts < cursor:
                        continue
                    # Hold the last observed value across gaps instead of inventing ticks.
                    area += ((float(a.value) + float(b.value)) / 2 if gap <= max_gap_seconds else float(a.value)) * gap
                    cursor = b.ts
                if cursor < now:
                    area += float(items[-1].value) * (now - cursor).total_seconds()
        else:
            # The entire settlement averaging window is still in the future.
            future_span = window
            drift_from = (window_start - now).total_seconds()
        if future_span <= 0:
            return Decimal(str(area / window)) if area > 0 else None
        if abs(g) < 1e-12:
            future_avg = p
        else:
            future_avg = p * (math.exp(g * (drift_from + future_span)) - math.exp(g * drift_from)) / (g * future_span)
        total = area + future_avg * future_span
        if total <= 0:
            return None
        return Decimal(str(total / window))


def _walk_dicts(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _walk_dicts(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_dicts(item)


def _parse_ts(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        num = float(value)
        if num > 1e11:
            num /= 1000
        return datetime.fromtimestamp(num, tz=timezone.utc)
    except Exception:
        return None


def extract_twap_observations(message: Any) -> list[TwapObservation]:
    """Parse PolyBolt price.crypto.twap messages tolerantly, but enforce Chainlink source."""
    out: list[TwapObservation] = []
    for node in _walk_dicts(message):
        value = node.get("full_accuracy_value")
        if value is None:
            value = node.get("value")
        if value is None:
            continue
        window = node.get("window_seconds", node.get("windowSeconds", 60))
        try:
            window_int = int(window)
        except Exception:
            continue
        if window_int not in {60, 30}:
            continue
        # A node must look like an observation, not the subscription echo.
        ts = _parse_ts(node.get("timestamp") or node.get("ts") or node.get("time"))
        if ts is None:
            continue
        source = str(node.get("source") or "").strip()
        symbol = str(node.get("symbol") or node.get("pair") or "btcusd").lower().replace("/", "").replace("-", "")
        if symbol not in {"btcusd", "btc/usd", "btc-usd"}:
            # allow nodes without symbol if they sit under a crypto channel
            if "symbol" in node:
                continue
            symbol = "btcusd"
        try:
            val = Decimal(str(value))
        except Exception:
            continue
        if val <= 0:
            continue
        out.append(
            TwapObservation(
                ts=ts,
                value=val,
                source=source or "UNKNOWN",
                symbol="btcusd",
                window_seconds=window_int,
                sequence=int(node["sequence"]) if node.get("sequence") is not None else None,
                raw={k: v for k, v in node.items() if k in {"source", "value", "full_accuracy_value", "timestamp", "window_seconds", "symbol"}},
            )
        )
    # Deduplicate identical (ts, value) observations.
    seen = set()
    unique = []
    for obs in out:
        key = (obs.ts, obs.value, obs.window_seconds)
        if key not in seen:
            seen.add(key)
            unique.append(obs)
    return unique


class PolyBoltTwapFeed:
    name = "polybolt"
    source_url = "wss://ws-live-v2.polymarket.com/ws"

    def __init__(self, settings: Settings, *, url: str | None = None, window_seconds: int = 60) -> None:
        self.settings = settings
        self.url = url or settings.polybolt_url
        self.tracker = TwapTracker(window_seconds=window_seconds)
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.last_error: str | None = None
        self.connected = False
        self._backoff = 1.0

    @property
    def configured(self) -> bool:
        return all(
            secret is not None and secret.get_secret_value()
            for secret in (
                self.settings.clob_api_key,
                self.settings.clob_api_secret,
                self.settings.clob_api_passphrase,
            )
        )

    def status(self, now: datetime, *, max_age_ms: int = 5000) -> FeedStatus:
        if not self.configured:
            return FeedStatus(
                name="chainlink_twap",
                health=FeedHealth.UNVERIFIED,
                detail="PolyBolt requires CLOB API credentials; oracle unverified",
            )
        return self.tracker.health(now, max_age_ms=max_age_ms)

    def inject(self, obs: TwapObservation) -> None:
        self.tracker.add(obs)

    async def start(self) -> None:
        if not self.configured:
            self.last_error = "PolyBolt credentials not configured"
            log.warning(self.last_error)
            return
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self.run(), name="polybolt-twap")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                async with websockets.connect(self.url, ping_interval=20, ping_timeout=20, close_timeout=5) as ws:
                    self.connected = True
                    self._backoff = 1.0
                    self.last_error = None
                    await ws.send(
                        json.dumps(
                            {
                                "op": "auth",
                                "auth": {
                                    "apiKey": self.settings.clob_api_key.get_secret_value(),
                                    "secret": self.settings.clob_api_secret.get_secret_value(),
                                    "passphrase": self.settings.clob_api_passphrase.get_secret_value(),
                                },
                            }
                        )
                    )
                    await ws.send(
                        json.dumps(
                            {
                                "op": "subscribe",
                                "subscriptions": [
                                    {
                                        "channel": "price.crypto.twap",
                                        "filter": {"symbol": "btcusd", "window_seconds": self.tracker.window_seconds},
                                    }
                                ],
                            }
                        )
                    )
                    async for raw in ws:
                        if self._stop.is_set():
                            break
                        try:
                            message = json.loads(raw)
                        except Exception:
                            continue
                        for obs in extract_twap_observations(message):
                            if obs.window_seconds == self.tracker.window_seconds:
                                self.tracker.add(obs)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.connected = False
                self.last_error = str(exc)
                log.warning("PolyBolt disconnected: %s", exc)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self._backoff)
                except asyncio.TimeoutError:
                    pass
                self._backoff = min(self._backoff * 2, 30)
        self.connected = False
