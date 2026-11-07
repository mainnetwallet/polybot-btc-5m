"""Risk manager: the absolute authority for every entry and protection state.

Hard limits are enforced by config.HardLimitError. This module never raises them
to satisfy exchange minimums and never treats a planned stop-loss as a reason to risk
more: the planned loss is assumed to be a full loss of the order cost. There is no
loss-streak cooldown: the daily loss limit (a fixed USDC amount) is the loss guard.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable

from ..clock import Clock
from ..config import RuntimeConfig
from ..db.repo import Repository
from ..types import Mode, ReasonCode, as_utc
from .exposure import ExposureLedger


@dataclass(frozen=True)
class RiskStatus:
    allowed: bool
    reason: ReasonCode
    detail: str
    equity_floor: Decimal | None
    daily_pnl: Decimal
    daily_loss_limit: Decimal
    daily_profit_target: Decimal | None
    locked: bool
    profit_locked: bool
    kill_switch: bool
    kill_reason: str
    consecutive_losses: int
    cooldown_until: datetime | None
    reserved: Decimal
    open_position_cost: Decimal
    open_positions: int


@dataclass(frozen=True)
class RiskUpdate:
    realized_pnl: Decimal
    daily_pnl: Decimal
    consecutive_losses: int
    cooldown_until: datetime | None
    locked: bool
    kill_switch: bool
    events: tuple[str, ...] = ()
    # True when the day's lock is the daily PROFIT lock (``locked`` alone also covers the loss limit).
    profit_locked: bool = False


class RiskManager:
    def __init__(
        self,
        *,
        repo: Repository,
        clock: Clock,
        config_provider: Callable[[], RuntimeConfig],
        ledger: ExposureLedger | None = None,
    ) -> None:
        self.repo = repo
        self.clock = clock
        self.config_provider = config_provider
        self.ledger = ledger or ExposureLedger(repo)

    # ------------------------------------------------------------------ limits
    def _cfg(self) -> RuntimeConfig:
        return self.config_provider()

    def daily_loss_limit(self) -> Decimal:
        return self._cfg().daily_loss_limit_usd

    def daily_profit_target(self) -> Decimal | None:
        return self._cfg().daily_profit_lock_usd

    def _today(self, now: datetime):
        return as_utc(now).astimezone(timezone.utc).date()

    # ------------------------------------------------------------------ status
    def status(self, mode: Mode, *, balance: Decimal | None = None, now: datetime | None = None) -> RiskStatus:
        now = as_utc(now or self.clock.now())
        cfg = self._cfg()
        day = self.repo.get_risk_day(self._today(now))
        state = self.repo.get_risk_state()
        summary = self.ledger.summary(mode.value, balance)
        limit = cfg.daily_loss_limit_usd
        target = cfg.daily_profit_lock_usd

        reason = ReasonCode.OK
        detail = "risk checks pass"
        if state.kill_switch:
            reason, detail = ReasonCode.KILL_SWITCH, state.kill_reason or "kill switch engaged"
        elif day.locked and day.profit_lock_triggered:
            reason, detail = ReasonCode.PROFIT_LOCK, "daily profit lock reached"
        elif day.locked:
            reason, detail = ReasonCode.DAILY_LOSS_LIMIT, "daily loss limit reached"
        elif summary.open_orders > 0:
            reason, detail = ReasonCode.RISK_LIMIT, "unresolved working order exists"

        return RiskStatus(
            allowed=reason is ReasonCode.OK,
            reason=reason,
            detail=detail,
            equity_floor=-limit,
            daily_pnl=day.realized_pnl,
            daily_loss_limit=limit,
            daily_profit_target=target,
            locked=day.locked,
            profit_locked=day.profit_lock_triggered,
            kill_switch=state.kill_switch,
            kill_reason=state.kill_reason,
            consecutive_losses=state.consecutive_losses,
            cooldown_until=state.cooldown_until,
            reserved=summary.reserved,
            open_position_cost=summary.open_position_cost,
            open_positions=summary.open_positions,
        )

    # ------------------------------------------------------------- pre-trade
    def pre_trade_check(
        self,
        *,
        market_id: str,
        mode: Mode,
        planned_loss: Decimal,
        planned_cost: Decimal,
        balance: Decimal | None,
        recovery_ready: bool,
        entries_used: int | None = None,
        now: datetime | None = None,
    ) -> tuple[bool, ReasonCode, str]:
        now = as_utc(now or self.clock.now())
        cfg = self._cfg()
        state = self.repo.get_risk_state()
        if state.kill_switch:
            return False, ReasonCode.KILL_SWITCH, state.kill_reason or "kill switch engaged"
        if not recovery_ready:
            return False, ReasonCode.RECOVERY_NOT_READY, "recovery has not completed reconciliation"
        day = self.repo.get_risk_day(self._today(now))
        if day.locked:
            if day.profit_lock_triggered:
                return False, ReasonCode.PROFIT_LOCK, "daily profit lock reached; trading stopped for the day"
            return False, ReasonCode.DAILY_LOSS_LIMIT, "daily loss limit reached; trading stopped for the day"
        limit = cfg.daily_loss_limit_usd
        if day.realized_pnl <= -limit:
            self._lock_day(day.trading_day, "DAILY_LOSS_LIMIT", {"realized_pnl": str(day.realized_pnl), "limit": str(limit)})
            return False, ReasonCode.DAILY_LOSS_LIMIT, f"daily loss {day.realized_pnl} reached limit {-limit}"
        # A top-up of the position already held in this market does not open a new position, so only
        # an entry into a market without a position counts against max_active_positions.
        open_positions = self.ledger.open_positions(mode.value)
        holds_this_market = any(p.market_id == market_id for p in open_positions)
        if not holds_this_market and len(open_positions) >= cfg.max_active_positions:
            return False, ReasonCode.ACTIVE_POSITION, f"{cfg.max_active_positions} active position(s) maximum"
        if self.ledger.entries_used(market_id, mode.value) >= cfg.max_entries_per_market:
            return False, ReasonCode.ENTRY_ALREADY_USED, f"{cfg.max_entries_per_market} entries per market maximum"
        if entries_used is not None and entries_used >= cfg.max_entries_per_market:
            return False, ReasonCode.ENTRY_ALREADY_USED, f"{cfg.max_entries_per_market} entries per market maximum"

        if planned_loss <= 0:
            return False, ReasonCode.RISK_LIMIT, "planned loss must be positive"

        reserved = self.repo.active_reserved_total()
        if balance is None:
            return False, ReasonCode.INSUFFICIENT_BALANCE, "balance unavailable"
        available = balance - reserved
        if planned_cost > available:
            return False, ReasonCode.INSUFFICIENT_BALANCE, f"cost {planned_cost} exceeds available {available}"
        return True, ReasonCode.OK, "risk checks pass"

    # --------------------------------------------------------------- outcomes
    def record_result(
        self,
        *,
        realized_pnl: Decimal,
        market_id: str,
        mode: Mode,
        now: datetime | None = None,
        position_closed: bool = True,
        position_pnl: Decimal | None = None,
    ) -> RiskUpdate:
        """Book realized P&L.

        ``realized_pnl`` is the P&L of this one fill or settlement; it always moves the daily P&L
        and the daily loss/profit locks. A position is a single trade, though: while it is still
        open (``position_closed=False``, a partial exit fill) nothing else is touched, and the call
        that closes it counts the trade, win/loss and loss streak once. That count is decided by
        ``position_pnl``, the position's total P&L over all of its fills (default: ``realized_pnl``),
        so a position that lost money overall is a loss even if its last fill was a small gain.
        """
        now = as_utc(now or self.clock.now())
        cfg = self._cfg()
        day = self.repo.get_risk_day(self._today(now))
        state = self.repo.get_risk_state()
        events: list[str] = []

        day_pnl = day.realized_pnl + realized_pnl
        trades, wins, losses = day.trades, day.wins, day.losses
        consecutive = state.consecutive_losses
        cooldown_until = None  # no loss-streak cooldown any more; the streak is a statistic only
        if position_closed:
            outcome_pnl = realized_pnl if position_pnl is None else position_pnl
            trades += 1
            if outcome_pnl > 0:
                wins += 1
                consecutive = 0
            elif outcome_pnl < 0:
                losses += 1
                consecutive += 1

        locked = day.locked
        profit_locked = day.profit_lock_triggered
        loss_limit = cfg.daily_loss_limit_usd
        if day_pnl <= -loss_limit and not locked:
            locked = True
            events.append("DAILY_LOSS_LIMIT")
            self.repo.add_protection_event(
                "DAILY_LOSS_LIMIT",
                "CRITICAL",
                market_id=market_id,
                daily_pnl=str(day_pnl),
                limit=str(loss_limit),
            )
        target = cfg.daily_profit_lock_usd
        if target is not None and day_pnl >= target and not profit_locked:
            locked = True
            profit_locked = True
            events.append("PROFIT_LOCK")
            self.repo.add_protection_event(
                "PROFIT_LOCK",
                "INFO",
                market_id=market_id,
                daily_pnl=str(day_pnl),
                target=str(target),
            )

        self.repo.update_risk_day(
            day.trading_day,
            realized_pnl=day_pnl,
            trades=trades,
            wins=wins,
            losses=losses,
            locked=locked,
            profit_lock_triggered=profit_locked,
        )
        if position_closed:
            self.repo.update_risk_state(consecutive_losses=consecutive, cooldown_until=cooldown_until)
        return RiskUpdate(
            realized_pnl=realized_pnl,
            daily_pnl=day_pnl,
            consecutive_losses=consecutive,
            cooldown_until=cooldown_until,
            locked=locked,
            kill_switch=state.kill_switch,
            events=tuple(events),
            profit_locked=profit_locked,
        )

    # ------------------------------------------------------------- protections
    def _lock_day(self, day, event: str, detail: dict) -> None:
        self.repo.update_risk_day(day, locked=True)
        self.repo.add_protection_event(event, "CRITICAL", **detail)

    def engage_kill_switch(self, reason: str, *, actor: str = "system", detail: dict | None = None) -> None:
        self.repo.update_risk_state(kill_switch=True, kill_reason=reason)
        self.repo.add_protection_event("KILL_SWITCH", "CRITICAL", reason=reason, **(detail or {}))
        self.repo.audit("KILL_SWITCH_ENGAGED", actor=actor, detail={"reason": reason, **(detail or {})})

    def clear_kill_switch(self, *, actor: str, reason: str) -> None:
        # Clearing the kill switch never clears a losing day.
        self.repo.update_risk_state(kill_switch=False, kill_reason="")
        self.repo.add_protection_event("KILL_SWITCH_CLEARED", "WARN", actor=actor, reason=reason)
        self.repo.audit("KILL_SWITCH_CLEARED", actor=actor, detail={"reason": reason})

    def reset_day_lock(self, *, actor: str, reason: str) -> dict:
        """Explicit, audited override for a daily lock. Never touches the kill switch.

        Returns ``will_relock``: a daily-LOSS lock is re-derived from realized P&L on the next
        entry check, so clearing the flag alone does not resume trading while the day's P&L is
        still at or below the loss limit. Callers (API/UI) should tell the operator.
        """
        now = self.clock.now()
        day = self.repo.get_risk_day(self._today(now))
        limit = self._cfg().daily_loss_limit_usd
        was_locked = day.locked
        self.repo.update_risk_day(day.trading_day, locked=False, profit_lock_triggered=False)
        will_relock = day.realized_pnl <= -limit
        self.repo.audit(
            "DAY_LOCK_RESET",
            actor=actor,
            detail={
                "reason": reason,
                "day": day.trading_day.isoformat(),
                "was_locked": was_locked,
                "will_relock": will_relock,
            },
        )
        return {
            "was_locked": was_locked,
            "will_relock": will_relock,
            "daily_pnl": str(day.realized_pnl),
            "daily_loss_limit": str(limit),
        }
