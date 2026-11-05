"""Market regime classification with hysteresis. Safety regimes switch immediately."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..types import Regime
from .features import FeatureSnapshot


@dataclass
class RegimeState:
    regime: Regime
    confidence: float
    reasons: list[str] = field(default_factory=list)
    changed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    candidate: Regime | None = None
    candidate_count: int = 0

    @property
    def entries_allowed(self) -> bool:
        return self.regime.entries_allowed and self.confidence >= 0.5


class RegimeDetector:
    def __init__(
        self,
        *,
        high_vol_threshold: float = 0.0012,
        trend_z: float = 1.2,
        reversal_z: float = 2.2,
        hysteresis_ticks: int = 3,
    ) -> None:
        self.high_vol_threshold = high_vol_threshold
        self.trend_z = trend_z
        self.reversal_z = reversal_z
        self.hysteresis_ticks = hysteresis_ticks
        self.state = RegimeState(Regime.UNKNOWN, 0.0, ["not evaluated"])

    def _classify(self, f: FeatureSnapshot) -> tuple[Regime, float, list[str]]:
        vol = f.realized_vol_60s
        r15, r30, r60, r180 = f.returns.get(15), f.returns.get(30), f.returns.get(60), f.returns.get(180)
        if vol is None or r60 is None or r180 is None or f.btc_price is None:
            return Regime.UNKNOWN, 0.0, ["insufficient return/volatility history"]

        z60 = r60 / (vol * math.sqrt(60)) if vol > 0 else 0.0
        z15 = r15 / (vol * math.sqrt(15)) if (vol > 0 and r15 is not None) else 0.0
        z180 = r180 / (vol * math.sqrt(180)) if vol > 0 else 0.0

        if vol >= self.high_vol_threshold:
            return Regime.HIGH_VOLATILITY, min(1.0, 0.6 + vol / (self.high_vol_threshold * 2)), [
                f"realized vol {vol:.6f}/sqrt(s) above {self.high_vol_threshold}"
            ]

        if r15 is not None and r60 is not None:
            opposite = (r15 > 0 > r60) or (r15 < 0 < r60)
            if opposite and abs(z15) >= self.reversal_z and abs(z60) >= 0.8:
                return Regime.SUDDEN_REVERSAL, min(1.0, 0.55 + abs(z15) / 6), [
                    f"15s move z={z15:.2f} opposes 60s z={z60:.2f}"
                ]

        if abs(z60) >= self.trend_z and f.mtf_agreement >= 0.6 and f.weighted_direction != 0:
            strength = min(1.0, 0.5 + abs(z60) / 4 + f.mtf_agreement / 4)
            return Regime.TRENDING, strength, [
                f"|z60|={abs(z60):.2f}, agreement={f.mtf_agreement:.2f}, direction={f.weighted_direction}"
            ]

        if abs(z60) < self.trend_z and abs(z180) < self.trend_z and f.mtf_agreement <= 0.8:
            confidence = max(0.3, 0.7 - abs(z60) / (self.trend_z * 2))
            return Regime.SIDEWAYS, confidence, [f"z60={z60:.2f}, z180={z180:.2f}"]

        return Regime.SIDEWAYS, 0.4, ["no strong trend or reversal"]

    def update(self, f: FeatureSnapshot, now: datetime | None = None) -> RegimeState:
        candidate, confidence, reasons = self._classify(f)
        now = now or f.ts
        immediate = candidate in {Regime.HIGH_VOLATILITY, Regime.SUDDEN_REVERSAL, Regime.UNKNOWN}

        if candidate is self.state.regime:
            self.state.confidence = confidence
            self.state.reasons = reasons
            self.state.candidate = None
            self.state.candidate_count = 0
            return self.state

        if immediate:
            self.state = RegimeState(candidate, confidence, reasons, changed_at=now)
            return self.state

        if self.state.candidate is candidate:
            self.state.candidate_count += 1
        else:
            self.state.candidate = candidate
            self.state.candidate_count = 1

        if self.state.candidate_count >= self.hysteresis_ticks:
            self.state = RegimeState(candidate, confidence, reasons, changed_at=now)
        else:
            self.state.reasons = reasons + [f"candidate {candidate.value} ({self.state.candidate_count}/{self.hysteresis_ticks})"]
        return self.state
