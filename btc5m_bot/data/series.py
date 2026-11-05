"""In-memory tick series for a single BTC price provider.

Design rules (see docs/SPEC.md section 10):

* One series holds ticks from exactly one provider; mixing providers raises ``ValueError``
  so trailing calculations never combine data from different sources.
* Every query is causal: it only ever looks at ticks with ``ts <= now``.
* Ticks must arrive in non-decreasing exchange-time order; late ticks are rejected.
"""
from __future__ import annotations

import bisect
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Sequence

from ..types import as_utc

# How far back a series keeps ticks, and a hard cap on stored ticks.
DEFAULT_RETENTION_SECONDS = 1800.0
DEFAULT_MAX_TICKS = 200_000
# Pruning is amortised: only drop old ticks once they are this far past the cutoff.
_PRUNE_SLACK_SECONDS = 60.0


@dataclass(frozen=True)
class BtcTick:
    """A single trade/price observation.

    ``ts`` is the provider (exchange) timestamp, ``recv_ts`` the local receive time.
    ``sequence`` is a provider trade counter used to detect dropped messages.
    """

    ts: datetime
    price: Decimal
    qty: Decimal = Decimal(0)
    source: str = ""
    sequence: int | None = None
    recv_ts: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "ts", as_utc(self.ts))
        if self.recv_ts is not None:
            object.__setattr__(self, "recv_ts", as_utc(self.recv_ts))
        if not isinstance(self.price, Decimal):
            object.__setattr__(self, "price", Decimal(str(self.price)))
        if not isinstance(self.qty, Decimal):
            object.__setattr__(self, "qty", Decimal(str(self.qty)))

    @property
    def received_at(self) -> datetime:
        """Local receive time, falling back to the exchange timestamp."""
        return self.recv_ts or self.ts


class BtcSeries:
    def __init__(
        self,
        source: str,
        *,
        retention_seconds: float = DEFAULT_RETENTION_SECONDS,
        max_ticks: int = DEFAULT_MAX_TICKS,
    ) -> None:
        self.source = source
        self.retention_seconds = retention_seconds
        self.max_ticks = max_ticks
        self._ticks: list[BtcTick] = []
        self._keys: list[float] = []  # epoch seconds, parallel to _ticks, for bisect

    # ------------------------------------------------------------------ basics
    def __len__(self) -> int:
        return len(self._ticks)

    @property
    def ticks(self) -> Sequence[BtcTick]:
        """Snapshot of all retained ticks, oldest first."""
        return tuple(self._ticks)

    def first(self) -> BtcTick | None:
        return self._ticks[0] if self._ticks else None

    def last(self) -> BtcTick | None:
        return self._ticks[-1] if self._ticks else None

    def clear(self) -> None:
        self._ticks.clear()
        self._keys.clear()

    def add(self, tick: BtcTick) -> bool:
        """Append a tick. Returns False if it was rejected (late, or invalid price).

        Raises ``ValueError`` if the tick belongs to a different provider.
        """
        if tick.source != self.source:
            raise ValueError(f"tick from {tick.source!r} cannot be added to a {self.source!r} series")
        try:
            valid = tick.price.is_finite() and tick.price > 0
        except Exception:  # noqa: BLE001
            valid = False
        if not valid:
            return False
        key = tick.ts.timestamp()
        if self._keys and key < self._keys[-1]:
            return False
        self._ticks.append(tick)
        self._keys.append(key)
        self._prune(key)
        return True

    def _prune(self, newest_key: float) -> None:
        over_cap = len(self._ticks) - self.max_ticks
        cutoff = newest_key - self.retention_seconds
        too_old = self._keys[0] < cutoff - _PRUNE_SLACK_SECONDS
        if over_cap <= 0 and not too_old:
            return
        drop = bisect.bisect_left(self._keys, cutoff) if too_old else 0
        drop = max(drop, over_cap, 0)
        if drop:
            del self._ticks[:drop]
            del self._keys[:drop]

    # ----------------------------------------------------------------- queries
    def _index_at_or_before(self, ts: datetime) -> int:
        return bisect.bisect_right(self._keys, as_utc(ts).timestamp()) - 1

    def last_at_or_before(self, ts: datetime) -> BtcTick | None:
        idx = self._index_at_or_before(ts)
        return self._ticks[idx] if idx >= 0 else None

    def price_at(self, ts: datetime, *, max_gap_seconds: float | None = 60.0) -> Decimal | None:
        """Last known price at ``ts``; None if there is none or it is older than ``max_gap_seconds``."""
        tick = self.last_at_or_before(ts)
        if tick is None:
            return None
        if max_gap_seconds is not None and (as_utc(ts) - tick.ts).total_seconds() > max_gap_seconds:
            return None
        return tick.price

    def ticks_between(self, start: datetime, end: datetime) -> list[BtcTick]:
        """Ticks with ``start <= ts <= end`` (inclusive), oldest first."""
        lo = bisect.bisect_left(self._keys, as_utc(start).timestamp())
        hi = bisect.bisect_right(self._keys, as_utc(end).timestamp())
        return self._ticks[lo:hi]

    def _window(self, seconds: float, now: datetime) -> list[BtcTick]:
        """Ticks in the half-open window (now - seconds, now]."""
        now = as_utc(now)
        lo = bisect.bisect_right(self._keys, (now - timedelta(seconds=seconds)).timestamp())
        hi = bisect.bisect_right(self._keys, now.timestamp())
        return self._ticks[lo:hi]

    def return_over(self, seconds: float, now: datetime, *, max_gap_seconds: float = 10.0) -> float | None:
        """Simple return over the trailing ``seconds`` ending at ``now``."""
        now = as_utc(now)
        end = self.price_at(now, max_gap_seconds=max_gap_seconds)
        start = self.price_at(now - timedelta(seconds=seconds), max_gap_seconds=max_gap_seconds)
        if end is None or start is None or start == 0:
            return None
        return float((end - start) / start)

    def realized_vol(
        self,
        window_seconds: float,
        now: datetime,
        *,
        max_gap_seconds: float = 10.0,
        min_samples: int | None = None,
    ) -> float | None:
        """Per-second realized volatility: stdev of 1-second log returns over the window.

        Prices are sampled on a 1-second grid (last tick at or before each grid time).
        Returns None when the window is not covered well enough to be meaningful.
        """
        now = as_utc(now)
        steps = int(window_seconds)
        needed = min_samples if min_samples is not None else max(10, steps // 3)
        prices: list[float | None] = []
        for k in range(steps + 1):
            p = self.price_at(now - timedelta(seconds=steps - k), max_gap_seconds=max_gap_seconds)
            prices.append(float(p) if p is not None else None)
        returns = [
            math.log(b / a)
            for a, b in zip(prices, prices[1:])
            if a is not None and b is not None and a > 0 and b > 0
        ]
        if len(returns) < max(needed, 2):
            return None
        mean = sum(returns) / len(returns)
        variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
        return math.sqrt(variance)

    def volume(self, window_seconds: float, now: datetime) -> Decimal:
        return sum((t.qty for t in self._window(window_seconds, now)), Decimal(0))

    def tick_count(self, window_seconds: float, now: datetime) -> int:
        return len(self._window(window_seconds, now))
