"""Capital that follows the real wallet balance (LIVE)."""
from __future__ import annotations

from decimal import ROUND_DOWN, Decimal


def balance_capital_target(
    *,
    balance: Decimal | None,
    current_capital: Decimal | None,
    flat: bool,
    day_trades: int,
) -> Decimal | None:
    """Return the capital to adopt from the wallet balance, or None to leave capital unchanged.

    Capital only moves while the bot is flat (no open position or reserved order), because the
    balance excludes cash tied up in a position and would understate the bankroll. It also only
    moves before the first trade of the trading day, so the daily loss limit stays anchored to the
    start-of-day balance instead of shrinking after a loss. An unset capital is filled at once.
    """
    if balance is None or balance <= 0 or not flat:
        return None
    if current_capital is not None and day_trades > 0:
        return None
    target = balance.quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    if target <= 0 or target == current_capital:
        return None
    return target
