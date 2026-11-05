"""Actual performance measurement from recorded trades. No synthetic statistics."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from statistics import mean, pstdev
from typing import Iterable, Sequence


@dataclass
class PerformanceSummary:
    trades: int
    wins: int
    losses: int
    flats: int
    net_pnl: Decimal
    fees: Decimal
    gross_profit: Decimal
    gross_loss: Decimal
    win_rate: float | None
    profit_factor: float | None
    expectancy: Decimal | None
    avg_win: Decimal | None
    avg_loss: Decimal | None
    max_drawdown: Decimal
    sharpe_per_trade: float | None
    total_notional: Decimal

    def as_dict(self) -> dict:
        return {
            "trades": self.trades,
            "wins": self.wins,
            "losses": self.losses,
            "flats": self.flats,
            "net_pnl": str(self.net_pnl),
            "fees": str(self.fees),
            "gross_profit": str(self.gross_profit),
            "gross_loss": str(self.gross_loss),
            "win_rate": self.win_rate,
            "profit_factor": self.profit_factor,
            "expectancy": str(self.expectancy) if self.expectancy is not None else None,
            "avg_win": str(self.avg_win) if self.avg_win is not None else None,
            "avg_loss": str(self.avg_loss) if self.avg_loss is not None else None,
            "max_drawdown": str(self.max_drawdown),
            "sharpe_per_trade": self.sharpe_per_trade,
            "total_notional": str(self.total_notional),
        }


def closing_trades(trades: Iterable) -> list:
    return [t for t in trades if getattr(t, "kind", "") in {"EXIT", "SETTLEMENT"}]


def compute_performance(trades: Iterable) -> PerformanceSummary:
    closes = closing_trades(trades)
    pnls = [Decimal(str(t.realized_pnl)) for t in closes]
    fees = sum((Decimal(str(t.fee)) for t in closes), Decimal(0))
    notional = sum((Decimal(str(t.price)) * Decimal(str(t.shares)) for t in closes), Decimal(0))
    wins = sum(1 for p in pnls if p > 0)
    losses = sum(1 for p in pnls if p < 0)
    flats = len(pnls) - wins - losses
    net = sum(pnls, Decimal(0))
    gross_profit = sum((p for p in pnls if p > 0), Decimal(0))
    gross_loss = -sum((p for p in pnls if p < 0), Decimal(0))
    win_rate = wins / len(pnls) if pnls else None
    profit_factor = float(gross_profit / gross_loss) if gross_loss > 0 else (None if not pnls else math.inf)
    expectancy = net / len(pnls) if pnls else None
    win_vals = [p for p in pnls if p > 0]
    loss_vals = [p for p in pnls if p < 0]
    avg_win = sum(win_vals, Decimal(0)) / len(win_vals) if win_vals else None
    avg_loss = sum(loss_vals, Decimal(0)) / len(loss_vals) if loss_vals else None

    equity = Decimal(0)
    peak = Decimal(0)
    max_dd = Decimal(0)
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)

    sharpe = None
    if len(pnls) >= 2:
        floats = [float(p) for p in pnls]
        sd = pstdev(floats)
        if sd > 0:
            sharpe = mean(floats) / sd

    return PerformanceSummary(
        trades=len(pnls),
        wins=wins,
        losses=losses,
        flats=flats,
        net_pnl=net,
        fees=fees,
        gross_profit=gross_profit,
        gross_loss=gross_loss,
        win_rate=win_rate,
        profit_factor=profit_factor,
        expectancy=expectancy,
        avg_win=avg_win,
        avg_loss=avg_loss,
        max_drawdown=max_dd,
        sharpe_per_trade=sharpe,
        total_notional=notional,
    )


def equity_curve(trades: Iterable) -> list[dict]:
    curve = []
    equity = Decimal(0)
    for t in closing_trades(trades):
        equity += Decimal(str(t.realized_pnl))
        curve.append(
            {
                "ts": getattr(t, "ts", None).isoformat() if getattr(t, "ts", None) else None,
                "market_id": getattr(t, "market_id", None),
                "pnl": str(t.realized_pnl),
                "equity": str(equity),
            }
        )
    return curve


def daily_performance(trades: Iterable) -> list[dict]:
    buckets: dict[date, list] = {}
    for t in closing_trades(trades):
        ts = getattr(t, "ts", None)
        if ts is None:
            continue
        buckets.setdefault(ts.date(), []).append(t)
    out = []
    for day in sorted(buckets):
        summary = compute_performance(buckets[day])
        out.append({"date": day.isoformat(), **summary.as_dict()})
    return out


def loss_breakdown(trades: Iterable) -> dict:
    closes = closing_trades(trades)
    by_outcome: dict[str, Decimal] = {}
    by_kind: dict[str, Decimal] = {}
    by_price_band: dict[str, Decimal] = {}
    for t in closes:
        pnl = Decimal(str(t.realized_pnl))
        if pnl >= 0:
            continue
        by_outcome[str(t.outcome)] = by_outcome.get(str(t.outcome), Decimal(0)) + pnl
        by_kind[str(t.side)] = by_kind.get(str(t.side), Decimal(0)) + pnl
        price = Decimal(str(t.price))
        band = f"{int(price * 100) // 10 * 10}-{int(price * 100) // 10 * 10 + 10}c"
        by_price_band[band] = by_price_band.get(band, Decimal(0)) + pnl
    return {
        "by_outcome": {k: str(v) for k, v in by_outcome.items()},
        "by_side": {k: str(v) for k, v in by_kind.items()},
        "by_exit_price_band": {k: str(v) for k, v in sorted(by_price_band.items())},
        "loss_count": sum(1 for t in closes if Decimal(str(t.realized_pnl)) < 0),
    }
