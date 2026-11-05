"""Deterministic oracle resolution simulator. Never mixes methodologies/providers."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from ..markets.rules import ResolutionRuleSpec
from ..types import Methodology, Outcome, as_utc
from .twap import TwapObservation, TwapTracker


@dataclass
class ResolutionResult:
    outcome: Outcome | None
    opening_reference: Decimal | None
    closing_reference: Decimal | None
    tie: bool
    confidence: str
    validation: str
    reason: str
    notional_methodology: str
    observations_used: list[dict[str, Any]] = field(default_factory=list)
    opening_age_seconds: float | None = None
    closing_age_seconds: float | None = None
    coverage_pct: float = 0.0

    @property
    def valid(self) -> bool:
        return self.validation == "VALID"


def _select(
    tracker: TwapTracker,
    start: datetime,
    end: datetime,
    window_seconds: int,
    *,
    boundary_tolerance_seconds: float,
) -> tuple[TwapObservation | None, TwapObservation | None, list[TwapObservation]]:
    start, end = as_utc(start), as_utc(end)
    pool = [
        o
        for o in tracker.observations
        if o.is_chainlink and o.window_seconds == window_seconds
    ]
    opening_pool = [
        o
        for o in pool
        if o.ts <= start and (start - o.ts).total_seconds() <= boundary_tolerance_seconds
    ]
    closing_pool = [
        o
        for o in pool
        if o.ts <= end and (end - o.ts).total_seconds() <= boundary_tolerance_seconds
    ]
    opening = opening_pool[-1] if opening_pool else None
    closing = closing_pool[-1] if closing_pool else None
    used = [o for o in pool if start - timedelta(seconds=max(window_seconds, 1)) <= o.ts <= end]
    return opening, closing, used


def resolve_market(
    rule: ResolutionRuleSpec,
    tracker: TwapTracker,
    start: datetime,
    end: datetime,
    *,
    min_coverage_seconds: int = 60,
) -> ResolutionResult:
    """Simulate resolution with the exact market methodology or refuse to guess."""
    start, end = as_utc(start), as_utc(end)
    method = rule.methodology
    if method is Methodology.UNKNOWN:
        return ResolutionResult(None, None, None, False, "LOW", "UNKNOWN_RULES", "resolution methodology unknown", method.value)
    if rule.tie_rule not in {"UP", "DOWN"}:
        return ResolutionResult(None, None, None, False, "LOW", "UNKNOWN_RULES", "tie rule unknown", method.value)
    if not rule.source:
        return ResolutionResult(None, None, None, False, "LOW", "UNKNOWN_RULES", "resolution source missing", method.value)
    if rule.source.upper() != "CHAINLINK":
        return ResolutionResult(
            None, None, None, False, "LOW", "UNKNOWN_RULES", "source is not Chainlink", method.value
        )

    if method is Methodology.SNAPSHOT:
        # No snapshot feed is ingested; never fake it.
        return ResolutionResult(
            None, None, None, False, "LOW", "INSUFFICIENT_DATA",
            "single-price snapshot observations are not available", method.value,
        )

    window = rule.window_seconds
    # Reference values must be within a reasonable distance of their boundary.
    tolerance = max(20.0, window * 0.5)
    opening, closing, used = _select(tracker, start, end, window, boundary_tolerance_seconds=tolerance)
    opening_age = (start - opening.ts).total_seconds() if opening else None
    closing_age = (end - closing.ts).total_seconds() if closing else None

    if opening is None or closing is None:
        return ResolutionResult(
            None,
            opening.value if opening else None,
            closing.value if closing else None,
            False,
            "LOW",
            "INSUFFICIENT_DATA",
            "missing opening or closing reference",
            method.value,
            observations_used=[],
            opening_age_seconds=opening_age,
            closing_age_seconds=closing_age,
        )

    # Coverage in the pre-start and pre-end averaging windows.
    def coverage(boundary: datetime) -> float:
        lo = boundary - timedelta(seconds=window)
        items = [o for o in used if lo <= o.ts <= boundary]
        if len(items) < 2:
            return 0.0
        covered = 0.0
        for a, b in zip(items, items[1:]):
            gap = (b.ts - a.ts).total_seconds()
            if gap <= 10.0:
                covered += gap
        return covered / window * 100

    coverage_pct = min(coverage(start), coverage(end))
    required_coverage = max(25.0, min(80.0, 100.0 * min(min_coverage_seconds, window * 0.5) / window))
    if coverage_pct < required_coverage:
        return ResolutionResult(
            None,
            opening.value,
            closing.value,
            False,
            "LOW",
            "INSUFFICIENT_DATA",
            f"oracle coverage {coverage_pct:.0f}% below minimum",
            method.value,
            observations_used=[_as_dict(o) for o in used],
            opening_age_seconds=opening_age,
            closing_age_seconds=closing_age,
            coverage_pct=coverage_pct,
        )

    tie = opening.value == closing.value
    if tie:
        outcome = Outcome.UP if rule.tie_rule == "UP" else Outcome.DOWN
    else:
        outcome = Outcome.UP if closing.value > opening.value else Outcome.DOWN

    if rule.confidence == "HIGH" and coverage_pct >= 90 and (opening_age or 0) <= 5 and (closing_age or 0) <= 5:
        confidence = "HIGH"
    elif coverage_pct >= 50 and (opening_age or 0) <= tolerance and (closing_age or 0) <= tolerance:
        confidence = "MEDIUM"
    else:
        confidence = "LOW"

    return ResolutionResult(
        outcome=outcome,
        opening_reference=opening.value,
        closing_reference=closing.value,
        tie=tie,
        confidence=confidence,
        validation="VALID",
        reason="resolved from Chainlink TWAP observations" + (" (tie)" if tie else ""),
        notional_methodology=method.value,
        observations_used=[_as_dict(o) for o in used],
        opening_age_seconds=opening_age,
        closing_age_seconds=closing_age,
        coverage_pct=coverage_pct,
    )


def _as_dict(obs: TwapObservation) -> dict[str, Any]:
    return {
        "ts": obs.ts.isoformat(),
        "value": str(obs.value),
        "source": obs.source,
        "window_seconds": obs.window_seconds,
    }


def compare_to_actual(result: ResolutionResult, actual: str | None) -> tuple[bool | None, str]:
    if actual is None:
        return None, "actual resolution unavailable"
    if result.outcome is None:
        return None, "simulation had insufficient data"
    actual_norm = actual.upper()
    if actual_norm not in {"UP", "DOWN"}:
        return None, f"unrecognised actual outcome {actual!r}"
    match = result.outcome.value == actual_norm
    return match, "match" if match else f"mismatch: simulated {result.outcome.value}, actual {actual_norm}"
