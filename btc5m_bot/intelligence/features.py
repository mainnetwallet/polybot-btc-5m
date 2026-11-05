"""Causal feature computation for the 5-minute window."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from ..data.series import BtcSeries
from ..oracle.twap import TwapTracker
from ..types import OrderBookSnapshot, Side, as_utc


@dataclass
class TimeframeAnalysis:
    returns: dict[int, float | None]
    directions: dict[int, int]
    agreement: float
    weighted_direction: int
    conflicts: list[str]
    momentum_z: dict[int, float | None]

    @property
    def has_conflict(self) -> bool:
        return bool(self.conflicts)


def analyze_timeframes(series: BtcSeries, now: datetime) -> TimeframeAnalysis:
    periods = (5, 15, 30, 60, 180)
    returns: dict[int, float | None] = {}
    directions: dict[int, int] = {}
    zscores: dict[int, float | None] = {}
    vol = series.realized_vol(180, now)
    for period in periods:
        r = series.return_over(period, now)
        returns[period] = r
        directions[period] = 0 if r is None else (1 if r > 0 else (-1 if r < 0 else 0))
        if r is not None and vol and vol > 0:
            zscores[period] = r / (vol * math.sqrt(period))
        else:
            zscores[period] = None
    known = {p: d for p, d in directions.items() if returns[p] is not None and d != 0}
    if known:
        counts = {1: sum(1 for d in known.values() if d > 0), -1: sum(1 for d in known.values() if d < 0)}
        majority = 1 if counts[1] >= counts[-1] else -1
        agreement = counts[majority] / len(known)
        conflicts = []
        if counts[1] and counts[-1]:
            # A conflict is material only when the 60s/180s context opposes the 5-15s move.
            short = [directions.get(p, 0) for p in (5, 15)]
            long = [directions.get(p, 0) for p in (60, 180)]
            if any(s != 0 for s in short) and any(l != 0 for l in long):
                if all(s == 0 or l == 0 or s != l for s, l in zip(short, long)):
                    conflicts.append("short and long timeframes disagree")
        weighted = 0.15 * directions[5] + 0.15 * directions[15] + 0.2 * directions[30] + 0.25 * directions[60] + 0.25 * directions[180]
        weighted_direction = 1 if weighted > 0.15 else (-1 if weighted < -0.15 else 0)
    else:
        agreement = 0.0
        conflicts = []
        weighted_direction = 0
    return TimeframeAnalysis(returns, directions, agreement, weighted_direction, conflicts, zscores)


def _top_depth(book: OrderBookSnapshot | None, side: Side, levels: int = 5) -> Decimal:
    if book is None:
        return Decimal(0)
    rows = book.asks if side is Side.BUY else book.bids
    return sum((l.size for l in list(rows)[:levels]), Decimal(0))


def _book_fields(book: OrderBookSnapshot | None) -> dict[str, Any]:
    if book is None:
        return {
            "best_bid": None,
            "best_ask": None,
            "spread": None,
            "depth_bid": Decimal(0),
            "depth_ask": Decimal(0),
            "mid": None,
        }
    return {
        "best_bid": book.best_bid,
        "best_ask": book.best_ask,
        "spread": book.spread,
        "depth_bid": _top_depth(book, side=Side.SELL),
        "depth_ask": _top_depth(book, side=Side.BUY),
        "mid": book.mid,
    }


@dataclass
class FeatureSnapshot:
    ts: datetime
    btc_price: Decimal | None
    returns: dict[int, float | None]
    directions: dict[int, int]
    momentum: float | None
    acceleration: float | None
    realized_vol_60s: float | None
    realized_vol_180s: float | None
    volume_30s: Decimal
    volume_180s: Decimal
    tick_rate_30s: float
    mtf_agreement: float
    mtf_conflict: bool
    weighted_direction: int
    twap_current: Decimal | None
    opening_reference: Decimal | None
    twap_distance_bps: float | None
    projected_final_twap: Decimal | None
    remaining_seconds: float
    entry_seconds_remaining: float
    up_book: OrderBookSnapshot | None
    down_book: OrderBookSnapshot | None
    up_best_bid: Decimal | None
    up_best_ask: Decimal | None
    down_best_bid: Decimal | None
    down_best_ask: Decimal | None
    up_spread: Decimal | None
    down_spread: Decimal | None
    up_depth: Decimal
    down_depth: Decimal
    implied_up: float | None
    implied_down: float | None
    drift_per_second: float
    data_age_ms: int
    book_age_ms: int
    oracle_age_ms: int | None
    oracle_coverage_seconds: float
    features: dict[str, Any] = field(default_factory=dict)

    @property
    def spread_ok(self) -> bool:
        return self.up_spread is not None and self.down_spread is not None

    def best_book(self) -> OrderBookSnapshot | None:
        return self.up_book or self.down_book


def compute_features(
    primary: BtcSeries,
    tracker: TwapTracker,
    *,
    now: datetime,
    market_start: datetime,
    market_end: datetime,
    up_book: OrderBookSnapshot | None,
    down_book: OrderBookSnapshot | None,
) -> FeatureSnapshot:
    now = as_utc(now)
    market_start, market_end = as_utc(market_start), as_utc(market_end)
    tf = analyze_timeframes(primary, now)
    last = primary.last_at_or_before(now)
    btc_price = last.price if last else None
    data_age_ms = int((now - (last.recv_ts or last.ts)).total_seconds() * 1000) if last else 10**9

    r60, r180 = tf.returns[60], tf.returns[180]
    momentum = r60
    if r60 is not None and r180 is not None and r180 != 0:
        # Per-second acceleration of the medium-term drift.
        acceleration = (r60 / 60.0) - (r180 / 180.0)
    elif tf.returns[30] is not None and r60 is not None:
        acceleration = (tf.returns[30] / 30.0) - (r60 / 60.0)
    else:
        acceleration = None

    vol60 = primary.realized_vol(60, now)
    vol180 = primary.realized_vol(180, now)
    volume_30 = primary.volume(30, now)
    volume_180 = primary.volume(180, now)
    ticks_30 = primary.tick_count(30, now)

    current_twap = None
    latest_twap = tracker.latest(now)
    if latest_twap is not None:
        current_twap = latest_twap.value
    opening = tracker.opening_reference(market_start)
    twap_distance_bps = None
    if current_twap is not None and opening not in (None, 0):
        twap_distance_bps = float((current_twap - opening) / opening * Decimal(10000))
    oracle_age_ms = None
    if latest_twap is not None:
        oracle_age_ms = max(0, int((now - latest_twap.ts).total_seconds() * 1000))
    coverage = tracker.coverage_seconds(market_start - timedelta(seconds=tracker.window_seconds), market_start) if opening else 0.0

    # Directional drift for the TWAP projection, capped to avoid runaway extrapolation.
    drift = 0.0
    if r60 is not None:
        drift = max(-4e-5, min(4e-5, r60 / 60.0)) * (0.5 if tf.weighted_direction == 0 else 1.0)
    projected = tracker.projected_final_twap(now, market_end, drift_per_second=drift) if current_twap is not None else None

    ub = _book_fields(up_book)
    db = _book_fields(down_book)
    implied_up = implied_down = None
    if ub["mid"] is not None and db["mid"] is not None:
        implied_up = float((Decimal(str(ub["mid"])) + (Decimal(1) - Decimal(str(db["mid"])))) / Decimal(2))
        implied_down = 1.0 - implied_up
    elif ub["mid"] is not None:
        implied_up = float(ub["mid"])
        implied_down = 1.0 - implied_up
    elif db["mid"] is not None:
        implied_down = float(db["mid"])
        implied_up = 1.0 - implied_down

    book_ages = []
    for book in (up_book, down_book):
        if book is not None:
            book_ages.append((now - book.timestamp).total_seconds() * 1000)
    book_age_ms = int(max(book_ages)) if book_ages else 10**9

    snapshot = FeatureSnapshot(
        ts=now,
        btc_price=btc_price,
        returns=tf.returns,
        directions=tf.directions,
        momentum=momentum,
        acceleration=acceleration,
        realized_vol_60s=vol60,
        realized_vol_180s=vol180,
        volume_30s=volume_30,
        volume_180s=volume_180,
        tick_rate_30s=ticks_30 / 30.0,
        mtf_agreement=tf.agreement,
        mtf_conflict=tf.has_conflict,
        weighted_direction=tf.weighted_direction,
        twap_current=current_twap,
        opening_reference=opening,
        twap_distance_bps=twap_distance_bps,
        projected_final_twap=projected,
        remaining_seconds=max(0.0, (market_end - now).total_seconds()),
        entry_seconds_remaining=max(0.0, (market_start + timedelta(seconds=120) - now).total_seconds()),
        up_book=up_book,
        down_book=down_book,
        up_best_bid=ub["best_bid"],
        up_best_ask=ub["best_ask"],
        down_best_bid=db["best_bid"],
        down_best_ask=db["best_ask"],
        up_spread=ub["spread"],
        down_spread=db["spread"],
        up_depth=ub["depth_bid"] + ub["depth_ask"],
        down_depth=db["depth_bid"] + db["depth_ask"],
        implied_up=implied_up,
        implied_down=implied_down,
        drift_per_second=drift,
        data_age_ms=data_age_ms,
        book_age_ms=book_age_ms,
        oracle_age_ms=oracle_age_ms,
        oracle_coverage_seconds=coverage,
        features={
            "returns": tf.returns,
            "directions": tf.directions,
            "agreement": tf.agreement,
            "conflicts": tf.conflicts,
            "vol60": vol60,
            "vol180": vol180,
            "twap_distance_bps": twap_distance_bps,
            "implied_up": implied_up,
            "up_spread": str(ub["spread"]) if ub["spread"] is not None else None,
            "down_spread": str(db["spread"]) if db["spread"] is not None else None,
        },
    )
    return snapshot
