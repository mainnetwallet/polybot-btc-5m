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
    # Circular moving-block bootstrap preserves short-range dependence between adjacent
    # trades. sqrt(n) is a transparent heuristic; this remains a diagnostic, not walk-forward proof.
    block_length = max(1, int(n ** 0.5))
    means = []
    for _ in range(BOOTSTRAP_ITERATIONS):
        sample: list[float] = []
        while len(sample) < n:
            start = rng.randrange(n)
            sample.extend(values[(start + offset) % n] for offset in range(block_length))
        means.append(sum(sample[:n]) / n)
    means.sort()
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
    ablation = _ablation(repo)
    variant_count = max(N_VARIANTS, len([v for v in ablation if v != "UNKNOWN"]))
    alpha_adj = ALPHA / variant_count
    low, high = _bootstrap_mean_ci(pnls, confidence=1 - alpha_adj)

    # A chronological holdout is kept separate from the aggregate bootstrap diagnostic.
    # This is an out-of-sample check on recorded PAPER closes, not a historical tick replay.
    ordered_closes = sorted(paper_closes, key=lambda t: t.ts)
    split_index = int(len(ordered_closes) * 0.70)
    train_closes = ordered_closes[:split_index]
    holdout_closes = ordered_closes[split_index:]
    min_holdout_trades = max(20, int(required_trades * 0.30 + 0.999))
    holdout_perf = compute_performance(holdout_closes)
    holdout_pnls = [float(t.realized_pnl) for t in holdout_closes]
    holdout_low, holdout_high = _bootstrap_mean_ci(holdout_pnls, confidence=1 - alpha_adj)
    if len(holdout_closes) < min_holdout_trades:
        failures.append(
            f"chronological holdout trades {len(holdout_closes)} < required {min_holdout_trades}"
        )
    else:
        if holdout_perf.net_pnl <= 0:
            failures.append(f"chronological holdout net PnL {holdout_perf.net_pnl} is not positive")
        if holdout_low is None or holdout_low <= 0:
            failures.append(f"chronological holdout expectancy lower bound {holdout_low} is not above zero")
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

    notes.append(f"binomial-adjusted alpha {alpha_adj:.4f} across at least {N_VARIANTS} strategy variants")
    notes.append("validation uses PAPER trades only; LIVE trades must not be used to qualify LIVE")
    notes.append("chronological 70/30 holdout uses recorded PAPER closes; it is not a historical market replay or proof of an untouched research dataset")
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
            "chronological_holdout": {
                "train_trades": len(train_closes),
                "trades": holdout_perf.trades,
                "net_pnl": str(holdout_perf.net_pnl),
                "expectancy_ci_low": holdout_low,
                "expectancy_ci_high": holdout_high,
                "minimum_trades": min_holdout_trades,
                "split": "70/30 chronological",
            },
            "stressed_net_pnl": str(stressed),
            "daily": daily_performance(paper_closes),
            "ablation": ablation,
        },
        notes=notes,
    )
