"""Risk manager: the absolute authority for every entry and protection state.

Hard limits are immutable (see config.HardLimitError). This module never raises them
to satisfy exchange minimums and never treats a planned stop-loss as a reason to risk
more: the planned loss is assumed to be a full loss of the order cost.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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
    capital: Decimal | None
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

    def capital(self) -> Decimal | None:
        capital = self._cfg().capital_usd
        return capital if capital is not None and capital > 0 else None

    def daily_loss_limit(self) -> Decimal:
        capital = self.capital()
        return capital * self._cfg().daily_loss_limit_pct if capital else Decimal(0)

    def daily_profit_target(self) -> Decimal | None:
        capital = self.capital()
        pct = self._cfg().daily_profit_lock_pct
        if capital is None or pct is None:
            return None
        return capital * pct

    def _today(self, now: datetime):
        return as_utc(now).astimezone(timezone.utc).date()

    # ------------------------------------------------------------------ status
    def _sync_cooldown(self, now: datetime) -> None:
        state = self.repo.get_risk_state()
        if state.cooldown_until is not None and as_utc(now) >= as_utc(state.cooldown_until):
            # Cooldown served: allow one fresh probe. A loss immediately re-arms it.
            self.repo.update_risk_state(consecutive_losses=0, cooldown_until=None)
            self.repo.add_protection_event(
                "COOLDOWN_ENDED", "INFO", consecutive_losses_reset=0, at=as_utc(now).isoformat()
            )

    def status(self, mode: Mode, *, balance: Decimal | None = None, now: datetime | None = None) -> RiskStatus:
        now = as_utc(now or self.clock.now())
        cfg = self._cfg()
        capital = self.capital()
        day = self.repo.get_risk_day(self._today(now))
        state = self.repo.get_risk_state()
        summary = self.ledger.summary(mode.value, balance)
        limit = capital * cfg.daily_loss_limit_pct if capital else Decimal(0)
        target = capital * cfg.daily_profit_lock_pct if (capital and cfg.daily_profit_lock_pct) else None

        reason = ReasonCode.OK
        detail = "risk checks pass"
        if capital is None:
            reason, detail = ReasonCode.CAPITAL_UNSET, "allocated capital is not configured"
        elif state.kill_switch:
            reason, detail = ReasonCode.KILL_SWITCH, state.kill_reason or "kill switch engaged"
        elif day.locked and day.profit_lock_triggered:
            reason, detail = ReasonCode.PROFIT_LOCK, "daily profit lock reached"
        elif day.locked:
            reason, detail = ReasonCode.DAILY_LOSS_LIMIT, "daily loss limit reached"
        elif state.cooldown_until is not None and now < as_utc(state.cooldown_until):
            reason, detail = ReasonCode.COOLDOWN, f"cooling down until {as_utc(state.cooldown_until).isoformat()}"
        elif state.consecutive_losses >= cfg.max_consecutive_losses:
            reason, detail = ReasonCode.CONSECUTIVE_LOSS_LIMIT, "consecutive-loss limit reached"
        elif summary.open_positions >= cfg.max_active_positions:
            reason, detail = ReasonCode.ACTIVE_POSITION, "an active position already exists"
        elif summary.open_orders > 0:
            reason, detail = ReasonCode.RISK_LIMIT, "unresolved working order exists"

        return RiskStatus(
            allowed=reason is ReasonCode.OK,
            reason=reason,
            detail=detail,
            capital=capital,
            equity_floor=-limit if capital else None,
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
        self._sync_cooldown(now)
        cfg = self._cfg()
        capital = self.capital()
        if capital is None:
            return False, ReasonCode.CAPITAL_UNSET, "allocated capital is not configured"
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
        limit = capital * cfg.daily_loss_limit_pct
        if day.realized_pnl <= -limit:
            self._lock_day(day.trading_day, "DAILY_LOSS_LIMIT", {"realized_pnl": str(day.realized_pnl), "limit": str(limit)})
            return False, ReasonCode.DAILY_LOSS_LIMIT, f"daily loss {day.realized_pnl} reached limit {-limit}"
        if state.cooldown_until is not None and now < as_utc(state.cooldown_until):
            return False, ReasonCode.COOLDOWN, f"cooling down until {as_utc(state.cooldown_until).isoformat()}"
        if state.consecutive_losses >= cfg.max_consecutive_losses:
            self._start_cooldown(now, state.consecutive_losses, "consecutive-loss guard")
            return False, ReasonCode.CONSECUTIVE_LOSS_LIMIT, "consecutive-loss limit reached"
        if self.ledger.has_active_position(mode.value):
            return False, ReasonCode.ACTIVE_POSITION, "one active position maximum"
        if self.ledger.entries_used(market_id, mode.value) >= cfg.max_entries_per_market:
            return False, ReasonCode.ENTRY_ALREADY_USED, "one entry per market"
        if entries_used is not None and entries_used >= cfg.max_entries_per_market:
            return False, ReasonCode.ENTRY_ALREADY_USED, "one entry per market"

        if planned_loss <= 0:
            return False, ReasonCode.RISK_LIMIT, "planned loss must be positive"
        # Fixed dollar size (trade_size_usd) is the operator's explicit per-trade amount: the
        # %-of-capital cap and its minimum-capital requirement apply only to risk-% sizing.
        if cfg.trade_size_usd is None:
            risk_budget = capital * cfg.risk_per_trade_pct
            if planned_loss > risk_budget:
                return False, ReasonCode.RISK_LIMIT, f"planned loss {planned_loss} exceeds {risk_budget} risk budget"
            if planned_cost > risk_budget:
                return False, ReasonCode.RISK_LIMIT, f"order cost {planned_cost} exceeds {risk_budget} risk budget"

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
    ) -> RiskUpdate:
        now = as_utc(now or self.clock.now())
        cfg = self._cfg()
        capital = self.capital()
        day = self.repo.get_risk_day(self._today(now))
        state = self.repo.get_risk_state()
        events: list[str] = []

        day_pnl = day.realized_pnl + realized_pnl
        trades = day.trades + 1
        wins = day.wins + (1 if realized_pnl > 0 else 0)
        losses = day.losses + (1 if realized_pnl < 0 else 0)
        consecutive = state.consecutive_losses
        if realized_pnl > 0:
            consecutive = 0
        elif realized_pnl < 0:
            consecutive += 1

        cooldown_until = state.cooldown_until
        if realized_pnl < 0 and consecutive >= cfg.max_consecutive_losses:
            cooldown_until = now + timedelta(minutes=cfg.cooldown_minutes)
            events.append("CONSECUTIVE_LOSS_LIMIT")
            self.repo.add_protection_event(
                "CONSECUTIVE_LOSS_LIMIT",
                "WARN",
                market_id=market_id,
                consecutive_losses=consecutive,
                cooldown_until=cooldown_until.isoformat(),
            )

        locked = day.locked
        profit_locked = day.profit_lock_triggered
        if capital is not None:
            loss_limit = capital * cfg.daily_loss_limit_pct
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
            target = capital * cfg.daily_profit_lock_pct if cfg.daily_profit_lock_pct else None
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
        self.repo.update_risk_state(consecutive_losses=consecutive, cooldown_until=cooldown_until)
        return RiskUpdate(
            realized_pnl=realized_pnl,
            daily_pnl=day_pnl,
            consecutive_losses=consecutive,
            cooldown_until=cooldown_until,
            locked=locked,
            kill_switch=state.kill_switch,
            events=tuple(events),
        )

    # ------------------------------------------------------------- protections
    def _lock_day(self, day, event: str, detail: dict) -> None:
        self.repo.update_risk_day(day, locked=True)
        self.repo.add_protection_event(event, "CRITICAL", **detail)

    def _start_cooldown(self, now: datetime, consecutive: int, reason: str) -> None:
        cfg = self._cfg()
        until = now + timedelta(minutes=cfg.cooldown_minutes)
        self.repo.update_risk_state(cooldown_until=until)
        self.repo.add_protection_event(
            "COOLDOWN_STARTED", "WARN", reason=reason, consecutive_losses=consecutive, until=until.isoformat()
        )

    def engage_kill_switch(self, reason: str, *, actor: str = "system", detail: dict | None = None) -> None:
        self.repo.update_risk_state(kill_switch=True, kill_reason=reason)
        self.repo.add_protection_event("KILL_SWITCH", "CRITICAL", reason=reason, **(detail or {}))
        self.repo.audit("KILL_SWITCH_ENGAGED", actor=actor, detail={"reason": reason, **(detail or {})})

    def clear_kill_switch(self, *, actor: str, reason: str) -> None:
        # Clearing the kill switch never clears a losing day or cooldown.
        self.repo.update_risk_state(kill_switch=False, kill_reason="")
        self.repo.add_protection_event("KILL_SWITCH_CLEARED", "WARN", actor=actor, reason=reason)
        self.repo.audit("KILL_SWITCH_CLEARED", actor=actor, detail={"reason": reason})

    def reset_day_lock(self, *, actor: str, reason: str) -> None:
        """Explicit, audited override for a daily lock. Never touches cooldown or kill switch."""
        now = self.clock.now()
        day = self.repo.get_risk_day(self._today(now))
        self.repo.update_risk_day(day.trading_day, locked=False, profit_lock_triggered=False)
        self.repo.audit("DAY_LOCK_RESET", actor=actor, detail={"reason": reason, "day": day.trading_day.isoformat()})
