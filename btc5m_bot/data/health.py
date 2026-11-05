"""Feed health monitoring: staleness, sequence gaps, timestamp skew, frozen prices, jumps,
provider divergence. Fail-closed: only HEALTHY legs allow new entries (docs/SPEC.md section 10).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from ..types import FeedHealth, FeedStatus, as_utc
from .series import BtcSeries

_SEVERITY = {
    FeedHealth.HEALTHY: 0,
    FeedHealth.DEGRADED: 1,
    FeedHealth.UNVERIFIED: 2,
    FeedHealth.STALE: 3,
    FeedHealth.DOWN: 4,
}


@dataclass(frozen=True)
class HealthThresholds:
    stale_after_seconds: float = 2.5      # age above this -> STALE
    down_after_seconds: float = 5.0       # age above this -> DOWN
    max_skew_seconds: float = 2.0         # |receive time - exchange time| above this -> DEGRADED
    max_frozen_seconds: float = 20.0      # identical price for this long (with ticks arriving) -> DEGRADED
    min_frozen_ticks: int = 5             # ...and at least this many ticks in that run
    max_jump_bps: float = 50.0            # consecutive-tick move above this -> DEGRADED
    jump_lookback_seconds: float = 10.0
    sequence_lookback_seconds: float = 30.0
    max_divergence_bps: float = 25.0      # primary vs secondary provider (docs/OPERATIONS.md)


class FeedHealthMonitor:
    def __init__(self, thresholds: HealthThresholds | None = None) -> None:
        self.thresholds = thresholds or HealthThresholds()

    def evaluate(self, name: str, series: BtcSeries, now: datetime) -> FeedStatus:
        th = self.thresholds
        now = as_utc(now)
        last = series.last_at_or_before(now)
        if last is None:
            return FeedStatus(name=name, health=FeedHealth.DOWN, detail="no ticks received")

        age_s = max(0.0, (now - last.received_at).total_seconds())
        age_ms = int(age_s * 1000)
        base = dict(
            name=name,
            last_tick_at=last.ts,
            last_value=last.price,
            age_ms=age_ms,
            sequence=last.sequence,
        )
        if age_s > th.down_after_seconds:
            return FeedStatus(health=FeedHealth.DOWN, detail=f"no ticks for {age_s:.1f} s", **base)
        if age_s > th.stale_after_seconds:
            return FeedStatus(health=FeedHealth.STALE, detail=f"age {age_ms} ms", **base)

        issues: list[str] = []

        skew_s = abs((last.received_at - last.ts).total_seconds())
        if skew_s > th.max_skew_seconds:
            issues.append(f"timestamp skew {skew_s:.1f} s")

        gaps, missing = self._sequence_gaps(series, last.ts, now)
        if gaps:
            issues.append(f"sequence gaps: {gaps} gap(s), {missing} message(s) missing")

        jump_bps = self._max_jump_bps(series, last.ts)
        if jump_bps > th.max_jump_bps:
            issues.append(f"price jump {jump_bps:.0f} bps")

        frozen_for = self._frozen_seconds(series, last)
        if frozen_for is not None:
            issues.append(f"price frozen for {frozen_for:.0f} s")

        if issues:
            return FeedStatus(health=FeedHealth.DEGRADED, detail="; ".join(issues), **base)
        return FeedStatus(health=FeedHealth.HEALTHY, detail="ok", **base)

    # ----------------------------------------------------------------- checks
    def _sequence_gaps(self, series: BtcSeries, end: datetime, now: datetime) -> tuple[int, int]:
        start = now - timedelta(seconds=self.thresholds.sequence_lookback_seconds)
        gaps = missing = 0
        prev: int | None = None
        for tick in series.ticks_between(start, end):
            if tick.sequence is None:
                prev = None
                continue
            if prev is not None and tick.sequence - prev > 1:
                gaps += 1
                missing += tick.sequence - prev - 1
            prev = tick.sequence
        return gaps, missing

    def _max_jump_bps(self, series: BtcSeries, end: datetime) -> float:
        start = end - timedelta(seconds=self.thresholds.jump_lookback_seconds)
        worst = 0.0
        prev: Decimal | None = None
        for tick in series.ticks_between(start, end):
            if prev is not None and prev > 0:
                worst = max(worst, abs(float((tick.price - prev) / prev)) * 10_000)
            prev = tick.price
        return worst

    def _frozen_seconds(self, series: BtcSeries, last) -> float | None:  # noqa: ANN001
        th = self.thresholds
        first = series.first()
        if first is None or (last.ts - first.ts).total_seconds() < th.max_frozen_seconds:
            return None  # not observed long enough to claim the price is frozen
        window = series.ticks_between(last.ts - timedelta(seconds=th.max_frozen_seconds), last.ts)
        if len(window) < th.min_frozen_ticks:
            return None
        if any(t.price != last.price for t in window):
            return None
        return (last.ts - window[0].ts).total_seconds()


@dataclass
class CompositeHealth:
    primary: FeedStatus
    secondary: FeedStatus
    oracle: FeedStatus
    overall: FeedHealth
    can_enter: bool
    reasons: list[str] = field(default_factory=list)
    divergence_bps: float | None = None
    oracle_basis_bps: float | None = None  # informational; the oracle_divergence filter gates on it


class CompositeFeedHealth:
    """Fuses primary feed, secondary feed and the Chainlink oracle into one entry gate.

    Entries are allowed only if every leg is HEALTHY and the two exchange feeds agree.
    There is no failover: one bad leg blocks (docs/SPEC.md: default failover mode is BLOCK).
    """

    def __init__(self, thresholds: HealthThresholds | None = None) -> None:
        self.thresholds = thresholds or HealthThresholds()
        self.monitor = FeedHealthMonitor(self.thresholds)

    def evaluate(
        self,
        primary: BtcSeries,
        secondary: BtcSeries,
        oracle: FeedStatus,
        now: datetime,
    ) -> CompositeHealth:
        now = as_utc(now)
        p_status = self.monitor.evaluate(primary.source, primary, now)
        s_status = self.monitor.evaluate(secondary.source, secondary, now)

        reasons: list[str] = []
        for role, status in (("primary feed", p_status), ("secondary feed", s_status), ("oracle", oracle)):
            if not status.healthy:
                detail = f": {status.detail}" if status.detail else ""
                reasons.append(f"{role} ({status.name}) {status.health.value}{detail}")

        divergence_bps: float | None = None
        diverged = False
        usable = (FeedHealth.HEALTHY, FeedHealth.DEGRADED)
        if (
            p_status.health in usable
            and s_status.health in usable
            and p_status.last_value is not None
            and s_status.last_value is not None
        ):
            a, b = p_status.last_value, s_status.last_value
            mid = (a + b) / 2
            if mid > 0:
                divergence_bps = abs(float((a - b) / mid)) * 10_000
                if divergence_bps > self.thresholds.max_divergence_bps:
                    diverged = True
                    reasons.append(
                        f"exchange feeds diverge by {divergence_bps:.1f} bps "
                        f"(max {self.thresholds.max_divergence_bps:.0f})"
                    )

        basis_bps: float | None = None
        if p_status.last_value is not None and oracle.last_value not in (None, 0):
            basis_bps = float((p_status.last_value - oracle.last_value) / oracle.last_value) * 10_000

        overall = max((p_status.health, s_status.health, oracle.health), key=_SEVERITY.__getitem__)
        if diverged and overall is FeedHealth.HEALTHY:
            overall = FeedHealth.DEGRADED
        can_enter = p_status.healthy and s_status.healthy and oracle.healthy and not diverged
        return CompositeHealth(
            primary=p_status,
            secondary=s_status,
            oracle=oracle,
            overall=overall,
            can_enter=can_enter,
            reasons=reasons,
            divergence_bps=divergence_bps,
            oracle_basis_bps=basis_bps,
        )
