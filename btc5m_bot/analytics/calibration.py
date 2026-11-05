"""Probability calibration: Brier score, log loss and reliability bins."""
from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass
class CalibrationPrediction:
    market_id: str
    probability: float
    outcome_up: bool


@dataclass
class CalibrationReport:
    count: int
    brier: float | None
    log_loss: float | None
    bins: list[dict] = field(default_factory=list)
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "count": self.count,
            "brier": self.brier,
            "log_loss": self.log_loss,
            "bins": self.bins,
            "note": self.note,
        }


def calibration_report(predictions: list[CalibrationPrediction]) -> CalibrationReport:
    if not predictions:
        return CalibrationReport(0, None, None, [], "insufficient calibration samples")
    brier = sum((p.probability - (1.0 if p.outcome_up else 0.0)) ** 2 for p in predictions) / len(predictions)
    eps = 1e-12
    losses = [
        -(math.log(max(min(p.probability, 1 - eps), eps)) if p.outcome_up else math.log(max(min(1 - p.probability, 1 - eps), eps)))
        for p in predictions
    ]
    logloss = sum(losses) / len(losses)
    bins = []
    for lower in [0.5, 0.6, 0.7, 0.8, 0.9]:
        upper = lower + 0.1
        subset = [p for p in predictions if lower <= p.probability < upper or (upper >= 1.0 and p.probability >= lower)]
        if not subset:
            continue
        predicted = sum(p.probability for p in subset) / len(subset)
        actual = sum(1 for p in subset if p.outcome_up) / len(subset)
        bins.append(
            {
                "bucket": f"{lower:.1f}-{upper:.1f}",
                "count": len(subset),
                "predicted": round(predicted, 4),
                "actual": round(actual, 4),
                "error": round(actual - predicted, 4),
            }
        )
    return CalibrationReport(len(predictions), brier, logloss, bins, "computed from resolved decisions")
