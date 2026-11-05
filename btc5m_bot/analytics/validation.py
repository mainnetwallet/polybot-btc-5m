"""Statistical validation for the LIVE gate. Fails honestly when data is insufficient."""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from decimal import Decimal

from ..config import RuntimeConfig
from ..db.repo import Repository
from .calibration import CalibrationPrediction, calibration_report
from .performance import closing_trades, compute_performance, daily_performance

BOOTSTRAP_ITERATIONS = 2000
BOOTSTRAP_SEED = 20261105
ALPHA = 0.05
N_VARIANTS = 4  # momentum, reversal, breakout, trend_confirmation


@dataclass
class ValidationReport:
    passed: bool
    failures: list[str] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"passed": self.passed, "failures": self.failures, "metrics": self.metrics, "notes": self.notes}


def _bootstrap_mean_ci(values: list[float], *, confidence: float) -> tuple[float | None, float | None]:
    if len(values) < 2:
        return None, None
    rng = random.Random(BOOTSTRAP_SEED)
    n = len(values)
    means = sorted(sum(rng.choices(values, k=n)) / n for _ in range(BOOTSTRAP_ITERATIONS))
    tail = (1.0 - confidence) / 2.0
    low = means[max(0, int(tail * len(means)) - 1)]
    high = means[min(len(means) - 1, int((1 - tail) * len(means)))]
    return low, high


def _calibration_predictions(repo: Repository) -> list[CalibrationPrediction]:
    resolutions = {r.market_id: r for r in repo.list_resolutions(limit=5000) if r.actual_outcome}
    decisions = repo.recent_decisions(limit=50_000)
    latest: dict[str, object] = {}
    for d in decisions:
        if d.probability is None or d.market_id in latest:
            continue
        latest[d.market_id] = d
    out: list[CalibrationPrediction] = []
    for market_id, resolution in resolutions.items():
        decision = latest.get(market_id)
        if decision is None:
            continue
        probability = float(decision.probability)
        if not 0.0 <= probability <= 1.0:
            continue
        out.append(
            CalibrationPrediction(
                market_id=market_id,
                probability=probability,
                outcome_up=str(resolution.actual_outcome).upper() == "UP",
            )
        )
    return out


def _ablation(repo: Repository) -> dict:
    """Compare realized outcomes by strategy version recorded on orders."""
    versions: dict[str, list] = {}
    orders = {o.client_order_id: o for o in repo.list_orders(limit=5000)}
    for trade in closing_trades(repo.list_trades(limit=5000)):
        order = orders.get(trade.client_order_id)
        version = order.strategy_version if order else "UNKNOWN"
        versions.setdefault(version, []).append(trade)
    out = {}
    for version, trades in versions.items():
        summary = compute_performance(trades)
        out[version] = {"trades": summary.trades, "net_pnl": str(summary.net_pnl), "expectancy": str(summary.expectancy) if summary.expectancy is not None else None}
    return out


def build_validation_report(repo: Repository, config: RuntimeConfig) -> ValidationReport:
    failures: list[str] = []
    notes: list[str] = []
    trades = repo.list_trades(limit=100_000)
    paper_closes = closing_trades([t for t in trades if t.mode == "PAPER"])
    perf = compute_performance(paper_closes)
    total, matched = repo.resolution_validation_count()
    match_rate = (matched / total) if total else None
    predictions = _calibration_predictions(repo)
    calib = calibration_report(predictions)

    required_trades = config.paper_validation_min_trades
    required_markets = config.live_validation_min_markets
    if perf.trades < required_trades:
        failures.append(f"paper trades {perf.trades} < required {required_trades}")
    if total < required_markets:
        failures.append(f"validated resolutions {total} < required {required_markets}")
    if match_rate is None or match_rate < 0.95:
        failures.append(f"resolution simulator match rate {match_rate} < 0.95")
    if calib.count < 50:
        failures.append(f"calibration samples {calib.count} < 50")
    elif calib.brier is None or calib.brier > 0.22:
        failures.append(f"Brier score {calib.brier} > 0.22")

    pnls = [float(t.realized_pnl) for t in paper_closes]
    alpha_adj = ALPHA / N_VARIANTS
    low, high = _bootstrap_mean_ci(pnls, confidence=1 - alpha_adj)
    if perf.trades >= required_trades:
        if perf.net_pnl <= 0:
            failures.append(f"paper net PnL {perf.net_pnl} is not positive")
        if low is None or low <= 0:
            failures.append(f"bootstrap expectancy lower bound {low} is not above zero after multiple-comparison adjustment")

    fees = sum((Decimal(str(t.fee)) for t in paper_closes), Decimal(0))
    notional = sum((Decimal(str(t.price)) * Decimal(str(t.shares)) for t in paper_closes), Decimal(0))
    stressed = perf.net_pnl - fees * Decimal("0.25") - notional * Decimal("0.005")
    if perf.trades >= required_trades and stressed <= 0:
        failures.append(f"stressed PnL {stressed} is not positive under +25% fees and +0.5c/share slippage")

    if perf.gross_profit > 0 and paper_closes:
        biggest = max((Decimal(str(t.realized_pnl)) for t in paper_closes), default=Decimal(0))
        if biggest > perf.gross_profit * Decimal("0.25"):
            failures.append("profit concentration: a single trade exceeds 25% of gross profit")

    notes.append(f"binomial-adjusted alpha {alpha_adj:.4f} across {N_VARIANTS} strategy variants")
    notes.append("validation uses PAPER trades only; LIVE trades must not be used to qualify LIVE")
    if total == 0:
        notes.append("no resolution simulations have been validated yet")

    return ValidationReport(
        passed=not failures,
        failures=failures,
        metrics={
            "paper_performance": perf.as_dict(),
            "resolution_validation": {"total": total, "matched": matched, "match_rate": match_rate},
            "calibration": calib.as_dict(),
            "bootstrap_expectancy": {"low": low, "high": high, "confidence": 1 - alpha_adj},
            "stressed_net_pnl": str(stressed),
            "daily": daily_performance(paper_closes),
            "ablation": _ablation(repo),
        },
        notes=notes,
    )
