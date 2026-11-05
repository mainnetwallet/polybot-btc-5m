"""Independently testable strategy variants. Only the ACTIVE one may create entries."""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from ..types import FeatureMode, Outcome
from .features import FeatureSnapshot
from .regimes import RegimeState


@dataclass(frozen=True)
class StrategySignal:
    strategy: str
    outcome: Outcome | None
    action: str
    confidence: float
    score: float
    reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def actionable(self) -> bool:
        return self.action == "BUY" and self.outcome is not None


def _z(f: FeatureSnapshot, seconds: int) -> float | None:
    r = f.returns.get(seconds)
    vol = f.realized_vol_60s
    if r is None or vol is None or vol <= 0:
        return None
    return r / (vol * math.sqrt(seconds))


class StrategyRegistry:
    def evaluate_all(self, f: FeatureSnapshot, regime: RegimeState) -> dict[str, StrategySignal]:
        return {
            "momentum": self.momentum(f, regime),
            "reversal": self.reversal(f, regime),
            "breakout": self.breakout(f, regime),
            "trend_confirmation": self.trend_confirmation(f, regime),
        }

    def select(
        self,
        name: str,
        mode: FeatureMode,
        f: FeatureSnapshot,
        regime: RegimeState,
    ) -> StrategySignal:
        signals = self.evaluate_all(f, regime)
        signal = signals.get(name, StrategySignal(name, None, "NO_SIGNAL", 0.0, 0.0, ("unknown strategy",)))
        if mode is not FeatureMode.ACTIVE:
            return StrategySignal(
                signal.strategy, None, "SHADOW", signal.confidence, signal.score, signal.reasons + (f"mode={mode.value}",)
            )
        return signal

    def momentum(self, f: FeatureSnapshot, regime: RegimeState) -> StrategySignal:
        if f.weighted_direction == 0 or f.mtf_conflict:
            return StrategySignal("momentum", None, "NO_SIGNAL", 0.0, 0.0, ("no directional agreement",))
        z = _z(f, 60)
        if z is None:
            return StrategySignal("momentum", None, "NO_SIGNAL", 0.0, 0.0, ("missing volatility",))
        confidence = min(1.0, 0.35 * f.mtf_agreement + 0.35 * min(1.0, abs(z) / 3.0) + 0.30 * regime.confidence)
        outcome = Outcome.UP if f.weighted_direction > 0 else Outcome.DOWN
        action = "BUY" if confidence >= 0.45 else "NO_SIGNAL"
        return StrategySignal(
            "momentum", outcome if action == "BUY" else None, action, confidence, z,
            (f"weighted_direction={f.weighted_direction}", f"z60={z:.2f}", f"agreement={f.mtf_agreement:.2f}"),
        )

    def reversal(self, f: FeatureSnapshot, regime: RegimeState) -> StrategySignal:
        r15, r60 = f.returns.get(15), f.returns.get(60)
        z15 = _z(f, 15)
        if r15 is None or r60 is None or z15 is None:
            return StrategySignal("reversal", None, "NO_SIGNAL", 0.0, 0.0, ("insufficient data",))
        opposite = (r15 > 0 > r60) or (r15 < 0 < r60)
        if not opposite or abs(z15) < 2.0:
            return StrategySignal("reversal", None, "NO_SIGNAL", 0.0, z15, ("no exhaustion reversal",))
        outcome = Outcome.UP if r15 > 0 else Outcome.DOWN
        confidence = min(1.0, 0.4 + abs(z15) / 8)
        return StrategySignal("reversal", outcome, "BUY", confidence, z15, ("short-term reversal",))

    def breakout(self, f: FeatureSnapshot, regime: RegimeState) -> StrategySignal:
        r15 = f.returns.get(15)
        z15 = _z(f, 15)
        if r15 is None or z15 is None or f.volume_180s <= 0:
            return StrategySignal("breakout", None, "NO_SIGNAL", 0.0, 0.0, ("insufficient data",))
        expected_30 = float(f.volume_180s) / 6.0
        volume_ratio = float(f.volume_30s) / expected_30 if expected_30 > 0 else 0.0
        if abs(z15) < 2.0 or volume_ratio < 0.4:
            return StrategySignal("breakout", None, "NO_SIGNAL", 0.0, z15, ("no confirmed breakout",))
        outcome = Outcome.UP if r15 > 0 else Outcome.DOWN
        return StrategySignal(
            "breakout", outcome, "BUY", min(1.0, 0.5 + abs(z15) / 8),
            z15, (f"z15={z15:.2f}", f"volume_ratio={volume_ratio:.2f}"),
        )

    def trend_confirmation(self, f: FeatureSnapshot, regime: RegimeState) -> StrategySignal:
        r60, r180 = f.returns.get(60), f.returns.get(180)
        if r60 is None or r180 is None or f.mtf_agreement < 0.75:
            return StrategySignal("trend_confirmation", None, "NO_SIGNAL", 0.0, 0.0, ("no aligned trend",))
        if (r60 > 0) != (r180 > 0) or r60 == 0:
            return StrategySignal("trend_confirmation", None, "NO_SIGNAL", 0.0, 0.0, ("trend timeframes conflict",))
        outcome = Outcome.UP if r60 > 0 else Outcome.DOWN
        return StrategySignal(
            "trend_confirmation", outcome, "BUY", min(1.0, 0.5 + f.mtf_agreement / 3),
            f.mtf_agreement, (f"r60={r60:.5f}", f"r180={r180:.5f}"),
        )
