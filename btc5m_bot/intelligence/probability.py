"""TWAP-aware probability model (transparent, no instantaneous-price shortcut)."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Decimal
from statistics import NormalDist

from ..types import Outcome, Regime
from .features import FeatureSnapshot
from .regimes import RegimeState

_N = NormalDist()


@dataclass
class ProbabilityEstimate:
    p_up: float
    p_down: float
    confidence: str
    uncertainty_low: float
    uncertainty_high: float
    expected_outcome: Outcome
    expected_final_twap: Decimal | None
    opening_reference: Decimal | None
    current_twap: Decimal | None
    sigma: float
    z: float
    regime: Regime
    signal_quality: float
    coverage_seconds: float
    method: str = "twap_projection_v1"
    details: dict = field(default_factory=dict)

    def probability(self, outcome: Outcome) -> float:
        return self.p_up if outcome is Outcome.UP else self.p_down

    @property
    def tradable(self) -> bool:
        return self.confidence in {"MEDIUM", "HIGH"}


class ProbabilityEngine:
    def __init__(
        self,
        *,
        min_coverage_seconds: float = 60.0,
        twap_window_seconds: int = 60,
    ) -> None:
        self.min_coverage_seconds = min_coverage_seconds
        self.twap_window_seconds = twap_window_seconds

    def estimate(
        self,
        f: FeatureSnapshot,
        regime: RegimeState,
        *,
        rule_confidence: str = "LOW",
        min_data_age_ms: int = 2500,
    ) -> ProbabilityEstimate | None:
        if (
            f.opening_reference is None
            or f.projected_final_twap is None
            or f.twap_current is None
            or f.btc_price is None
            or f.realized_vol_60s is None
            or f.remaining_seconds <= 0
        ):
            return None
        if f.oracle_coverage_seconds < self.min_coverage_seconds:
            return None

        vol = f.realized_vol_60s
        rem = min(f.remaining_seconds, 300.0)
        window = float(self.twap_window_seconds)
        if rem <= 0:
            return None
        # Stdev of the future price average over the remaining part of the TWAP window.
        if rem <= window:
            factor = math.sqrt((rem ** 3) / 3.0) / window
        else:
            factor = math.sqrt(window / 3.0)
        sigma = float(f.btc_price) * vol * factor
        sigma = max(sigma, float(f.btc_price) * 1e-6)

        distance = float(f.projected_final_twap - f.opening_reference)
        z = distance / sigma
        p = _N.cdf(z)
        p = min(0.995, max(0.005, p))

        freshness = max(0.0, min(1.0, 1.0 - f.data_age_ms / max(min_data_age_ms, 1)))
        coverage_ratio = min(1.0, f.oracle_coverage_seconds / max(self.min_coverage_seconds, 1))
        rule_score = {"HIGH": 1.0, "MEDIUM": 0.65, "LOW": 0.25}.get(rule_confidence, 0.25)
        mtf_score = f.mtf_agreement if f.weighted_direction != 0 else 0.35
        regime_score = regime.confidence
        quality = (
            0.20 * freshness
            + 0.25 * coverage_ratio
            + 0.15 * rule_score
            + 0.20 * regime_score
            + 0.20 * mtf_score
        )
        quality = max(0.0, min(1.0, quality))

        # Shrink toward 0.5 as evidence quality degrades; never inflate confidence.
        p_adj = 0.5 + (p - 0.5) * quality
        p_adj = min(0.995, max(0.005, p_adj))

        # Approximate binomial standard error from effective independent 5s samples.
        n_eff = max(2.0, f.oracle_coverage_seconds / 5.0 + f.remaining_seconds / 10.0)
        se = min(0.20, 0.5 / math.sqrt(n_eff))
        low = max(0.0, p_adj - 1.64 * se)
        high = min(1.0, p_adj + 1.64 * se)

        if quality >= 0.75 and coverage_ratio >= 0.75 and abs(p_adj - 0.5) >= 0.08:
            confidence = "HIGH"
        elif quality >= 0.55 and coverage_ratio >= 0.5 and abs(p_adj - 0.5) >= 0.04:
            confidence = "MEDIUM"
        else:
            confidence = "LOW"

        return ProbabilityEstimate(
            p_up=p_adj,
            p_down=1.0 - p_adj,
            confidence=confidence,
            uncertainty_low=low,
            uncertainty_high=high,
            expected_outcome=Outcome.UP if p_adj >= 0.5 else Outcome.DOWN,
            expected_final_twap=f.projected_final_twap,
            opening_reference=f.opening_reference,
            current_twap=f.twap_current,
            sigma=sigma,
            z=z,
            regime=regime.regime,
            signal_quality=quality,
            coverage_seconds=f.oracle_coverage_seconds,
            details={
                "raw_p": p,
                "shrink": quality,
                "freshness": freshness,
                "coverage_ratio": coverage_ratio,
                "rule_confidence": rule_confidence,
                "regime_confidence": regime_score,
                "mtf_score": mtf_score,
                "distance": distance,
                "remaining_seconds": f.remaining_seconds,
                "twap_distance_bps": f.twap_distance_bps,
                "market_implied_up": f.implied_up,
                "market_implied_down": f.implied_down,
            },
        )
