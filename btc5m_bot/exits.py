"""Hybrid exit manager with strict precedence.

Precedence: emergency -> stop loss -> trailing stop -> take profit -> probability
reversal -> momentum reversal -> liquidity deterioration -> hold-vs-sell EV ->
time exit -> hold-to-resolution. Every exit sells the whole position at market.
SELLs are market orders: no minimum share size, no dust rule.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from .config import RuntimeConfig
from .db.models import PositionRow
from .exchange.fees import FeeSchedule, taker_fee
from .execution.feasibility import ExitFeasibility, assess_exit_feasibility
from .intelligence.features import FeatureSnapshot
from .intelligence.probability import ProbabilityEstimate
from .types import FeatureMode, MarketInfo, OrderBookSnapshot, Outcome, as_utc, utcnow


class ExitAction(str, Enum):
    HOLD = "HOLD"
    SELL = "SELL"


@dataclass
class ExitDecision:
    action: ExitAction
    reason: str
    detail: str
    priority: int
    shares: Decimal = Decimal(0)
    limit_price: Decimal | None = None
    feasibility: ExitFeasibility | None = None
    roi: Decimal | None = None
    mark_price: Decimal | None = None
    expected_hold_value: Decimal | None = None
    expected_sell_value: Decimal | None = None
    shadow_action: ExitAction | None = None
    shadow_reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class HybridExitManager:
    def __init__(self) -> None:
        self._peak_bid: dict[str, Decimal] = {}
        self._depth_peak: dict[str, Decimal] = {}
        self._spread_floor: dict[str, Decimal] = {}
        # True when the tracked values changed since the last export_state(); the engine uses it to
        # persist them, so a restart cannot reset a trailing stop's high-water mark.
        self.dirty = False

    # --------------------------------------------------------------- persistence
    def export_state(self) -> dict[str, dict[str, str]]:
        """JSON-safe snapshot of the per-position peaks and baselines (Decimals as exact text)."""
        self.dirty = False
        return {
            "peak_bid": {k: format(v, "f") for k, v in self._peak_bid.items()},
            "depth_peak": {k: format(v, "f") for k, v in self._depth_peak.items()},
            "spread_floor": {k: format(v, "f") for k, v in self._spread_floor.items()},
        }

    def load_state(self, data: dict[str, Any] | None, *, keep_position_ids: set[int] | None = None) -> int:
        """Restore a snapshot from export_state(). Entries of positions not in keep_position_ids
        (closed or settled while the bot was down) are dropped. Returns the entries restored."""
        restored = 0
        for attr, name in (("_peak_bid", "peak_bid"), ("_depth_peak", "depth_peak"), ("_spread_floor", "spread_floor")):
            store: dict[str, Decimal] = getattr(self, attr)
            store.clear()
            raw = (data or {}).get(name) or {}
            if not isinstance(raw, dict):
                continue
            for key, value in raw.items():
                try:
                    position_id = int(str(key).split(":", 1)[0])
                    amount = Decimal(str(value))
                except Exception:  # noqa: BLE001 - a corrupt entry must never stop the engine
                    continue
                if not amount.is_finite():
                    continue
                if keep_position_ids is not None and position_id not in keep_position_ids:
                    continue
                store[str(key)] = amount
                restored += 1
        self.dirty = False
        return restored

    # ------------------------------------------------------------------ utils
    def _book_for(self, position: PositionRow, up_book: OrderBookSnapshot | None, down_book: OrderBookSnapshot | None) -> OrderBookSnapshot | None:
        return up_book if position.outcome.upper() == Outcome.UP.value else down_book

    def _probability_for(self, probability: ProbabilityEstimate | None, position: PositionRow) -> float | None:
        if probability is None:
            return None
        outcome = Outcome.UP if position.outcome.upper() == Outcome.UP.value else Outcome.DOWN
        return probability.probability(outcome)

    def _hold(self, reason: str, detail: str, priority: int, **kwargs: Any) -> ExitDecision:
        return ExitDecision(ExitAction.HOLD, reason, detail, priority, **kwargs)

    def _sell_or_hold(
        self,
        *,
        reason: str,
        detail: str,
        priority: int,
        position: PositionRow,
        book: OrderBookSnapshot | None,
        market: MarketInfo,
        feasibility: ExitFeasibility,
        **kwargs: Any,
    ) -> ExitDecision:
        if book is None or book.best_bid is None:
            # Nothing to sell into. (Any bid at all is enough: the exit is a market sell.)
            return self._hold(
                "SELL_NOT_FEASIBLE",
                f"{reason}: no bid liquidity to sell into",
                priority,
                feasibility=feasibility,
                shadow_action=ExitAction.SELL,
                shadow_reason=reason,
                **kwargs,
            )
        # MARKET SELL of every share held: no size minimum and no price floor beyond the lowest
        # valid tick, so the order sweeps the bids (sent as FAK; any unfilled remainder is retried).
        limit = market.tick_size
        return ExitDecision(
            ExitAction.SELL,
            reason,
            detail,
            priority,
            shares=position.shares,
            limit_price=limit,
            feasibility=feasibility,
            **kwargs,
        )

    # --------------------------------------------------------------- evaluate
    def evaluate(
        self,
        *,
        position: PositionRow,
        market: MarketInfo,
        up_book: OrderBookSnapshot | None,
        down_book: OrderBookSnapshot | None,
        probability: ProbabilityEstimate | None,
        config: RuntimeConfig,
        features: FeatureSnapshot | None = None,
        now: datetime | None = None,
        remaining_seconds: float | None = None,
        risk_locked: bool = False,
        feed_healthy: bool = True,
        oracle_healthy: bool = True,
        recovery_ready: bool = True,
        book_max_age_ms: int | None = None,
    ) -> ExitDecision:
        now = as_utc(now or utcnow())
        book = self._book_for(position, up_book, down_book)
        reason_prefix = "HOLD"

        if position.status in {"CLOSED", "SETTLED", "REDEEMED"} or position.shares <= 0:
            return self._hold("NOT_OPEN", "position is not open", 0)

        key = f"{position.id}:{position.asset_id}"
        p_own = self._probability_for(probability, position)
        schedule = FeeSchedule.from_market(market)
        stale_book = bool(book is not None and book_max_age_ms and book.is_stale(now, book_max_age_ms))
        feasibility = assess_exit_feasibility(
            book,
            position.shares,
            require_full_exit=True,
        )

        if book is None or book.best_bid is None:
            return self._hold(
                "NO_BID_HOLD_TO_RESOLUTION",
                "no bid liquidity; exit is impossible and settlement is the only path",
                90,
                feasibility=feasibility,
            )

        mark = book.best_bid
        mark_value = position.shares * mark
        basis = position.cost_basis if position.cost_basis > 0 else position.shares * position.avg_price
        roi = (mark_value - basis) / basis if basis > 0 else None
        peak = max(self._peak_bid.get(key, mark), mark)
        if self._peak_bid.get(key) != peak:
            self.dirty = True
        self._peak_bid[key] = peak
        trailing_armed = peak > position.avg_price

        if remaining_seconds is None:
            remaining_seconds = (as_utc(market.end_date) - now).total_seconds()

        # Dynamic TP: lock a weak edge earlier. The target is never raised above take_profit_roi:
        # a position at or past the configured target must sell (a strong favourite used to push
        # the target up to 1.5x, so a +20-25% profit was held and often given back).
        take_profit_target = config.take_profit_roi
        if config.dynamic_take_profit and p_own is not None:
            if p_own >= 0.75:
                multiple = Decimal(1)
            elif p_own < 0.55:
                multiple = config.dynamic_tp_min_multiple
            else:
                multiple = Decimal(1)
            if remaining_seconds <= 60 and p_own < 0.65:
                multiple = min(multiple, config.dynamic_tp_min_multiple)
            take_profit_target = config.take_profit_roi * multiple

        meta = {
            "mark_price": mark,
            "roi": roi,
            "peak_bid": peak,
            "p_own": Decimal(str(p_own)) if p_own is not None else None,
            "remaining_seconds": remaining_seconds,
            "stale_book": stale_book,
            "take_profit_target": take_profit_target,
        }

        own_dir = 1 if position.outcome.upper() == Outcome.UP.value else -1
        opened = getattr(position, "opened_at", None)
        held_seconds = (now - as_utc(opened)).total_seconds() if opened is not None else None
        held_long_enough = held_seconds is None or held_seconds >= config.min_hold_seconds_before_reversal

        expected_hold = Decimal(str(p_own)) * position.shares if p_own is not None else None
        expected_sell = None
        if feasibility.expected_exit_price is not None:
            sell_price = min(feasibility.expected_exit_price, book.best_bid)
            expected_sell = position.shares * sell_price - taker_fee(position.shares, sell_price, schedule)
        if expected_hold is not None:
            meta["expected_hold_value"] = expected_hold
        if expected_sell is not None:
            meta["expected_sell_value"] = expected_sell

        # Track the own-outcome book so deterioration is measured against this position's history.
        depth_now = sum((level.size for level in book.bids[:5]), Decimal(0)) + sum(
            (level.size for level in book.asks[:5]), Decimal(0)
        )
        peak_depth = max(self._depth_peak.get(key, Decimal(0)), depth_now)
        if self._depth_peak.get(key) != peak_depth:
            self.dirty = True
        self._depth_peak[key] = peak_depth
        spread_now = book.spread
        if spread_now is not None and spread_now > 0:
            floor = self._spread_floor.get(key)
            new_floor = spread_now if floor is None else min(floor, spread_now)
            if floor != new_floor:
                self.dirty = True
            self._spread_floor[key] = new_floor
        spread_floor = self._spread_floor.get(key)

        # 1. Emergency: protection state, oracle loss, stale book, recovery failure.
        emergency = risk_locked or not feed_healthy or not oracle_healthy or not recovery_ready or stale_book
        if emergency:
            detail = ", ".join(
                label
                for label, flag in (
                    ("risk lock", risk_locked),
                    ("BTC feed unhealthy", not feed_healthy),
                    ("oracle unhealthy", not oracle_healthy),
                    ("recovery not ready", not recovery_ready),
                    ("stale book", stale_book),
                )
                if flag
            )
            return self._sell_or_hold(
                reason="EMERGENCY_EXIT",
                detail=f"emergency exit: {detail}",
                priority=1,
                position=position,
                book=book,
                market=market,
                feasibility=feasibility,
                mark_price=mark,
                roi=roi,
                metadata=meta,
            )

        # 2. Hard stop loss (immutable protection).
        if roi is not None and roi <= config.stop_loss_roi:
            return self._sell_or_hold(
                reason="STOP_LOSS",
                detail=f"ROI {roi:.4%} <= stop {config.stop_loss_roi:.2%}",
                priority=2,
                position=position,
                book=book,
                market=market,
                feasibility=feasibility,
                mark_price=mark,
                roi=roi,
                metadata=meta,
            )

        # 3. Trailing stop (experimental: SHADOW by default).
        trailing_mode = config.feature("trailing_stop")
        if trailing_mode is not FeatureMode.OFF and trailing_armed:
            drawdown = (peak - mark) / peak if peak > 0 else Decimal(0)
            if drawdown >= config.trailing_stop_pct:
                if trailing_mode is FeatureMode.ACTIVE:
                    return self._sell_or_hold(
                        reason="TRAILING_STOP",
                        detail=f"drawdown {drawdown:.2%} from peak {peak} >= {config.trailing_stop_pct:.2%}",
                        priority=3,
                        position=position,
                        book=book,
                        market=market,
                        feasibility=feasibility,
                        mark_price=mark,
                        roi=roi,
                        metadata=meta,
                    )
                meta["trailing_shadow"] = f"drawdown {drawdown:.2%} from peak"

        # 4. Dynamic take profit.
        if roi is not None and roi >= take_profit_target:
            return self._sell_or_hold(
                reason="TAKE_PROFIT",
                detail=(
                    f"ROI {roi:.4%} >= target {take_profit_target:.2%} "
                    f"(base {config.take_profit_roi:.2%})"
                ),
                priority=4,
                position=position,
                book=book,
                market=market,
                feasibility=feasibility,
                mark_price=mark,
                roi=roi,
                metadata=meta,
            )

        # 5. Probability reversal.
        if probability is not None and p_own is not None:
            reversed_expectation = probability.expected_outcome is not (
                Outcome.UP if position.outcome.upper() == Outcome.UP.value else Outcome.DOWN
            )
            if reversed_expectation and p_own < 0.45 and probability.confidence in {"MEDIUM", "HIGH"}:
                return self._sell_or_hold(
                    reason="PROBABILITY_REVERSAL",
                    detail=f"P(own)={p_own:.4f} < 0.45 with {probability.confidence} confidence",
                    priority=5,
                    position=position,
                    book=book,
                    market=market,
                    feasibility=feasibility,
                    mark_price=mark,
                    roi=roi,
                    metadata=meta,
                )

        # 5b. Momentum reversal: the short-horizon trend has flipped against the position.
        if features is not None and held_long_enough:
            r60 = features.returns.get(60)
            vol60 = features.realized_vol_60s
            z60 = (r60 / (vol60 * math.sqrt(60.0))) if r60 is not None and vol60 and vol60 > 0 else None
            short_adverse = any(features.directions.get(period) == -own_dir for period in (5, 15))
            meta["momentum_z60"] = z60
            if (
                features.weighted_direction == -own_dir
                and r60 is not None
                and r60 * own_dir < 0
                and z60 is not None
                and abs(z60) >= float(config.momentum_reversal_z)
                and short_adverse
            ):
                return self._sell_or_hold(
                    reason="MOMENTUM_REVERSAL",
                    detail=f"60s momentum {r60:+.4%} (z={z60:.2f}) against the {position.outcome} position",
                    priority=5,
                    position=position,
                    book=book,
                    market=market,
                    feasibility=feasibility,
                    mark_price=mark,
                    roi=roi,
                    expected_hold_value=expected_hold,
                    expected_sell_value=expected_sell,
                    metadata=meta,
                )

        # 6. Liquidity deterioration: the own-outcome book has thinned and widened materially.
        depth_collapsed = peak_depth > 0 and depth_now <= peak_depth * (
            Decimal(1) - config.liquidity_exit_depth_drop_pct
        )
        spread_deteriorated = spread_now is not None and (
            (
                spread_floor is not None
                and spread_floor > 0
                and spread_now >= spread_floor * config.liquidity_exit_spread_multiple
            )
            or spread_now >= config.max_spread
        )
        if (
            held_long_enough
            and remaining_seconds > config.time_exit_seconds_before_end
            and depth_collapsed
            and spread_deteriorated
            and (p_own is None or p_own < 0.55)
            and expected_sell is not None
            and expected_sell > 0
        ):
            meta.update(depth_now=depth_now, depth_peak=peak_depth, spread_now=spread_now)
            return self._sell_or_hold(
                reason="LIQUIDITY_DETERIORATION",
                detail=(
                    f"own book depth {depth_now} <= {(Decimal(1) - config.liquidity_exit_depth_drop_pct):.0%} "
                    f"of peak {peak_depth}; spread {spread_now} widened from {spread_floor}"
                ),
                priority=6,
                position=position,
                book=book,
                market=market,
                feasibility=feasibility,
                mark_price=mark,
                roi=roi,
                expected_hold_value=expected_hold,
                expected_sell_value=expected_sell,
                metadata=meta,
            )

        # 7. Hold vs sell: compare settlement expectation with net exit proceeds.
        if expected_hold is not None and expected_sell is not None:
            buffer = Decimal("0.01") * position.shares
            if expected_sell > expected_hold + buffer:
                return self._sell_or_hold(
                    reason="HOLD_VS_SELL_EV",
                    detail=f"net exit {expected_sell:.4f} > expected hold {expected_hold:.4f}",
                    priority=7,
                    position=position,
                    book=book,
                    market=market,
                    feasibility=feasibility,
                    mark_price=mark,
                    roi=roi,
                    expected_hold_value=expected_hold,
                    expected_sell_value=expected_sell,
                    metadata=meta,
                )

        # 8. Time-based exit near the end when holding is worse than selling.
        near_end = remaining_seconds is not None and remaining_seconds <= config.time_exit_seconds_before_end
        if near_end and p_own is not None and expected_sell is not None:
            if p_own < 0.5 and expected_sell > Decimal(0):
                return self._sell_or_hold(
                    reason="TIME_EXIT",
                    detail=f"{remaining_seconds:.1f}s left, P(own)={p_own:.4f}; sell beats holding",
                    priority=8,
                    position=position,
                    book=book,
                    market=market,
                    feasibility=feasibility,
                    mark_price=mark,
                    roi=roi,
                    expected_hold_value=expected_hold,
                    expected_sell_value=expected_sell,
                    metadata=meta,
                )

        return self._hold(
            "HOLD_TO_RESOLUTION",
            "no exit rule triggered; position remains open",
            9,
            feasibility=feasibility,
            mark_price=mark,
            roi=roi,
            expected_hold_value=expected_hold,
            expected_sell_value=expected_sell,
            metadata=meta,
        )

    def forget(self, position_id: int) -> None:
        prefix = f"{position_id}:"
        for store in (self._peak_bid, self._depth_peak, self._spread_floor):
            for key in [k for k in store if k.startswith(prefix)]:
                store.pop(key, None)
                self.dirty = True
