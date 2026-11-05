"""Strict 23-condition LIVE gate and explicit activation.

LIVE is never enabled silently. Every condition is evaluated from live state; a
failure returns the exact failed requirement and LIVE stays disabled.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from .db.repo import Repository
from .exchange.base import Balance, GeoResult
from .types import as_utc, utcnow

LIVE_CONFIRMATION_PHRASE = "ACTIVATE LIVE TRADING"


@dataclass(frozen=True)
class GateCondition:
    number: int
    key: str
    name: str
    passed: bool
    detail: str = ""


@dataclass
class GateContext:
    activation_requested: bool = False
    geo: GeoResult | None = None
    wallet_auth_verified: bool = False
    signature_type: int | None = None
    signature_type_verified: bool = False
    funder_verified: bool = False
    balance: Balance | None = None
    capital: Decimal | None = None
    allowance_required: Decimal | None = None
    allowance_ok: bool = False
    reconciliation_clean: bool = False
    recovery_ready: bool = False
    feeds_healthy: bool = False
    rules_verified: bool = False
    rules_approved: bool = False
    resolution_simulator_active: bool = False
    exposure_monitor_active: bool = False
    disaster_recovery_active: bool = False
    risk_manager_active: bool = False
    protective_shutdown_resolved: bool = False
    protocol_version_ok: bool = False
    min_buy_feasible: bool = False
    min_sell_feasible: bool = False
    heartbeat_configured: bool = False
    heartbeat_tested: bool = False
    statistical_validation_passed: bool = False
    final_confirmation: bool = False
    details: dict[str, str] = field(default_factory=dict)

    def detail(self, key: str, fallback: str = "") -> str:
        return self.details.get(key, fallback)


@dataclass
class GateReport:
    conditions: tuple[GateCondition, ...]
    checked_at: datetime

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.conditions)

    @property
    def failed(self) -> list[GateCondition]:
        return [c for c in self.conditions if not c.passed]

    @property
    def passed_count(self) -> int:
        return sum(1 for c in self.conditions if c.passed)

    def as_dict(self) -> dict:
        return {
            "passed": self.passed,
            "passed_count": self.passed_count,
            "total": len(self.conditions),
            "checked_at": self.checked_at.isoformat(),
            "conditions": [
                {"number": c.number, "key": c.key, "name": c.name, "passed": c.passed, "detail": c.detail}
                for c in self.conditions
            ],
        }


class LiveGate:
    def __init__(self, repo: Repository) -> None:
        self.repo = repo

    def evaluate(self, ctx: GateContext) -> GateReport:
        balance_ok = (
            ctx.balance is not None
            and ctx.capital is not None
            and ctx.capital > 0
            and ctx.balance.available >= ctx.capital
        )
        allowance_ok = (
            ctx.allowance_ok
            and ctx.allowance_required is not None
            and ctx.balance is not None
            and ctx.balance.allowance is not None
            and ctx.balance.allowance >= ctx.allowance_required
        )
        geo_ok = ctx.geo is not None and ctx.geo.blocked is False
        geo_detail = (
            "geoblock endpoint did not return an explicit eligible result"
            if ctx.geo is None or ctx.geo.blocked is None
            else f"blocked={ctx.geo.blocked} country={ctx.geo.country or '?'}"
        )
        sig_ok = ctx.signature_type_verified and ctx.signature_type in {0, 1, 2, 3}
        heartbeat_ok = ctx.heartbeat_configured and ctx.heartbeat_tested

        conditions = (
            GateCondition(1, "explicit_activation", "Explicit user activation", ctx.activation_requested, "activation request recorded"),
            GateCondition(2, "geographic_eligibility", "Geographic eligibility passed", geo_ok, geo_detail),
            GateCondition(3, "wallet_auth", "Wallet authentication verified", ctx.wallet_auth_verified, ctx.detail("wallet_auth")),
            GateCondition(4, "signature_type", "Correct signature type verified", sig_ok, f"signature_type={ctx.signature_type}"),
            GateCondition(5, "funder", "Funder address verified", ctx.funder_verified, ctx.detail("funder")),
            GateCondition(6, "pusd_balance", "Sufficient pUSD balance", balance_ok, ctx.detail("balance")),
            GateCondition(7, "allowance", "Correct balance and allowance", allowance_ok, ctx.detail("allowance")),
            GateCondition(8, "clean_reconciliation", "Clean order reconciliation", ctx.reconciliation_clean, ctx.detail("reconciliation")),
            GateCondition(9, "recovery_ready", "Recovery state READY", ctx.recovery_ready, ctx.detail("recovery")),
            GateCondition(10, "feeds_healthy", "Healthy primary and required oracle feeds", ctx.feeds_healthy, ctx.detail("feeds")),
            GateCondition(11, "rules_verified", "Verified market resolution rules", ctx.rules_verified, ctx.detail("rules")),
            GateCondition(12, "rules_approved", "User-approved rule fingerprints", ctx.rules_approved, ctx.detail("rule_approval")),
            GateCondition(13, "simulator_active", "Resolution simulator ACTIVE", ctx.resolution_simulator_active, ""),
            GateCondition(14, "exposure_monitor", "Portfolio exposure monitor ACTIVE", ctx.exposure_monitor_active, ""),
            GateCondition(15, "disaster_recovery", "Disaster recovery ACTIVE", ctx.disaster_recovery_active, ctx.detail("disaster_recovery")),
            GateCondition(16, "risk_manager", "Risk manager ACTIVE", ctx.risk_manager_active, ctx.detail("risk")),
            GateCondition(17, "protective_shutdown", "No unresolved protective shutdown", ctx.protective_shutdown_resolved, ctx.detail("protective")),
            GateCondition(18, "protocol_version", "Correct protocol and market version", ctx.protocol_version_ok, ctx.detail("protocol")),
            GateCondition(19, "min_buy", "Minimum BUY feasibility verified", ctx.min_buy_feasible, ctx.detail("min_buy")),
            GateCondition(20, "min_sell", "Minimum SELL and exit feasibility verified", ctx.min_sell_feasible, ctx.detail("min_sell")),
            GateCondition(21, "heartbeat", "Heartbeat safety configured and tested", heartbeat_ok, ctx.detail("heartbeat")),
            GateCondition(22, "statistical_validation", "Statistical validation passed", ctx.statistical_validation_passed, ctx.detail("validation")),
            GateCondition(23, "final_confirmation", "Explicit final user confirmation", ctx.final_confirmation, ""),
        )
        return GateReport(conditions=conditions, checked_at=as_utc(utcnow()))

    def activation_active(self) -> bool:
        return self.repo.active_live_activation() is not None

    def activate(self, ctx: GateContext, *, typed_phrase: str, actor: str, config: dict | None = None) -> GateReport:
        ctx.final_confirmation = typed_phrase.strip() == LIVE_CONFIRMATION_PHRASE
        report = self.evaluate(ctx)
        if not report.passed:
            self.repo.audit(
                "LIVE_ACTIVATION_REJECTED",
                actor=actor,
                detail={"failed": [c.key for c in report.failed], "typed_confirmation": bool(ctx.final_confirmation)},
            )
            return report
        if self.activation_active():
            self.repo.audit("LIVE_ACTIVATION_REPLAY", actor=actor, detail={"note": "activation already active"})
            return report
        phrase_hash = hashlib.sha256(f"{LIVE_CONFIRMATION_PHRASE}:{actor}".encode()).hexdigest()
        self.repo.create_live_activation(phrase_hash, actor, config or {})
        self.repo.audit("LIVE_ACTIVATED", actor=actor, detail={"conditions_passed": report.passed_count})
        return report

    def revoke(self, *, actor: str, reason: str) -> None:
        self.repo.revoke_live_activation(actor)
        self.repo.audit("LIVE_REVOKED", actor=actor, detail={"reason": reason})
