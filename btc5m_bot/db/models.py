"""SQLAlchemy models. Financial values are stored as exact decimal text."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import BigInteger, Boolean, Date, DateTime, ForeignKey, Index, Integer, JSON, String, Text, TypeDecorator, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc)


class DecimalText(TypeDecorator):
    impl = String(64)
    cache_ok = True

    def process_bind_param(self, value: Decimal | int | float | str | None, dialect: Any) -> str | None:
        if value is None:
            return None
        return format(Decimal(str(value)), "f")

    def process_result_value(self, value: str | None, dialect: Any) -> Decimal | None:
        if value is None:
            return None
        return Decimal(value)


class Base(DeclarativeBase):
    pass


class MarketRow(Base):
    __tablename__ = "markets"

    market_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    condition_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    slug: Mapped[str] = mapped_column(String(200), index=True)
    question: Mapped[str] = mapped_column(Text, default="")
    version: Mapped[str] = mapped_column(String(8))
    neg_risk: Mapped[bool] = mapped_column(Boolean, default=False)
    event_start: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    end_date: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    tick_size: Mapped[Decimal] = mapped_column(DecimalText)
    min_order_size: Mapped[Decimal] = mapped_column(DecimalText)
    accepting_orders: Mapped[bool] = mapped_column(Boolean, default=False)
    fees_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    fee_rate: Mapped[Decimal] = mapped_column(DecimalText, default=Decimal(0))
    fee_exponent: Mapped[int] = mapped_column(Integer, default=1)
    taker_only: Mapped[bool] = mapped_column(Boolean, default=True)
    rebate_rate: Mapped[Decimal] = mapped_column(DecimalText, default=Decimal(0))
    clob_token_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    position_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    outcomes: Mapped[list[str]] = mapped_column(JSON, default=list)
    resolution_source: Mapped[str] = mapped_column(Text, default="")
    description: Mapped[str] = mapped_column(Text, default="")
    methodology: Mapped[str] = mapped_column(String(32), default="UNKNOWN")
    rules_fingerprint: Mapped[str] = mapped_column(String(64), index=True, default="")
    liquidity: Mapped[Decimal] = mapped_column(DecimalText, default=Decimal(0))
    raw: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    discovered_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_utcnow, onupdate=_utcnow)


class MarketWindowRow(Base):
    __tablename__ = "market_windows"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    market_id: Mapped[str] = mapped_column(String(128), ForeignKey("markets.market_id"), unique=True, index=True)
    state: Mapped[str] = mapped_column(String(24), default="DISCOVERED", index=True)
    entry_deadline: Mapped[datetime] = mapped_column(UTCDateTime)
    entered: Mapped[bool] = mapped_column(Boolean, default=False)
    opening_twap: Mapped[Decimal | None] = mapped_column(DecimalText, nullable=True)
    closing_twap: Mapped[Decimal | None] = mapped_column(DecimalText, nullable=True)
    oracle_coverage_seconds: Mapped[int] = mapped_column(Integer, default=0)
    simulated_outcome: Mapped[str | None] = mapped_column(String(16), nullable=True)
    resolution_outcome: Mapped[str | None] = mapped_column(String(16), nullable=True)
    settlement_ts: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    resolution_confirmed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    mismatch_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_utcnow, onupdate=_utcnow)


class OrderRow(Base):
    __tablename__ = "orders"

    client_order_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    exchange_order_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    market_id: Mapped[str] = mapped_column(String(128), index=True)
    condition_id: Mapped[str] = mapped_column(String(128))
    asset_id: Mapped[str] = mapped_column(String(128), index=True)
    side: Mapped[str] = mapped_column(String(8))
    outcome: Mapped[str] = mapped_column(String(8))
    price: Mapped[Decimal] = mapped_column(DecimalText)
    shares: Mapped[Decimal] = mapped_column(DecimalText)
    filled_shares: Mapped[Decimal] = mapped_column(DecimalText, default=Decimal(0))
    avg_price: Mapped[Decimal] = mapped_column(DecimalText, default=Decimal(0))
    fee_usd: Mapped[Decimal] = mapped_column(DecimalText, default=Decimal(0))
    order_type: Mapped[str] = mapped_column(String(8), default="FOK")
    mode: Mapped[str] = mapped_column(String(8), default="PAPER")
    status: Mapped[str] = mapped_column(String(24), default="INTENDED", index=True)
    strategy_version: Mapped[str] = mapped_column(String(32), default="BOT_BASELINE")
    reason: Mapped[str] = mapped_column(Text, default="")
    intent_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    raw_response: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_utcnow, index=True)
    submitted_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    acked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    filled_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    __table_args__ = (Index("ix_orders_market_status", "market_id", "status"),)


class PositionRow(Base):
    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    market_id: Mapped[str] = mapped_column(String(128), index=True)
    condition_id: Mapped[str] = mapped_column(String(128))
    asset_id: Mapped[str] = mapped_column(String(128), index=True)
    outcome: Mapped[str] = mapped_column(String(8))
    shares: Mapped[Decimal] = mapped_column(DecimalText)
    avg_price: Mapped[Decimal] = mapped_column(DecimalText)
    entry_fee: Mapped[Decimal] = mapped_column(DecimalText, default=Decimal(0))
    cost_basis: Mapped[Decimal] = mapped_column(DecimalText)
    opened_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="OPEN", index=True)
    exit_price: Mapped[Decimal | None] = mapped_column(DecimalText, nullable=True)
    exit_fee: Mapped[Decimal] = mapped_column(DecimalText, default=Decimal(0))
    realized_pnl: Mapped[Decimal] = mapped_column(DecimalText, default=Decimal(0))
    resolution_outcome: Mapped[str | None] = mapped_column(String(16), nullable=True)
    dust_shares: Mapped[Decimal] = mapped_column(DecimalText, default=Decimal(0))
    mode: Mapped[str] = mapped_column(String(8), default="PAPER")
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_utcnow, onupdate=_utcnow)

    __table_args__ = (UniqueConstraint("market_id", "mode", name="uq_position_market_mode"),)


class ExposureReservationRow(Base):
    __tablename__ = "exposure_reservations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    market_id: Mapped[str] = mapped_column(String(128), index=True)
    client_order_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    amount: Mapped[Decimal] = mapped_column(DecimalText)
    status: Mapped[str] = mapped_column(String(16), default="ACTIVE", index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_utcnow)
    released_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")


class RiskDayRow(Base):
    __tablename__ = "risk_days"

    trading_day: Mapped[date] = mapped_column(Date, primary_key=True)
    realized_pnl: Mapped[Decimal] = mapped_column(DecimalText, default=Decimal(0))
    fees_paid: Mapped[Decimal] = mapped_column(DecimalText, default=Decimal(0))
    trades: Mapped[int] = mapped_column(Integer, default=0)
    wins: Mapped[int] = mapped_column(Integer, default=0)
    losses: Mapped[int] = mapped_column(Integer, default=0)
    profit_lock_triggered: Mapped[bool] = mapped_column(Boolean, default=False)
    locked: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_utcnow, onupdate=_utcnow)


class RiskStateRow(Base):
    __tablename__ = "risk_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    consecutive_losses: Mapped[int] = mapped_column(Integer, default=0)
    cooldown_until: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    kill_switch: Mapped[bool] = mapped_column(Boolean, default=False)
    kill_reason: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_utcnow, onupdate=_utcnow)


class ProtectionEventRow(Base):
    __tablename__ = "protection_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_type: Mapped[str] = mapped_column(String(48), index=True)
    severity: Mapped[str] = mapped_column(String(16), default="INFO")
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_utcnow, index=True)


class HeartbeatStateRow(Base):
    __tablename__ = "heartbeat_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    heartbeat_id: Mapped[str] = mapped_column(String(128), default="")
    armed: Mapped[bool] = mapped_column(Boolean, default=False)
    last_success_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    last_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_utcnow, onupdate=_utcnow)


class RecoveryStateRow(Base):
    __tablename__ = "recovery_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    state: Mapped[str] = mapped_column(String(24), default="STARTING")
    detail: Mapped[str] = mapped_column(Text, default="")
    last_check_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    reconciled_orders: Mapped[int] = mapped_column(Integer, default=0)
    reconciled_positions: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_utcnow, onupdate=_utcnow)


class ResolutionRuleRow(Base):
    __tablename__ = "resolution_rules"

    fingerprint: Mapped[str] = mapped_column(String(64), primary_key=True)
    methodology: Mapped[str] = mapped_column(String(32))
    source: Mapped[str] = mapped_column(Text, default="")
    tie_rule: Mapped[str] = mapped_column(String(32), default="UP")
    precision: Mapped[int] = mapped_column(Integer, default=2)
    rounding: Mapped[str] = mapped_column(String(32), default="UNKNOWN")
    window_seconds: Mapped[int] = mapped_column(Integer, default=0)
    text: Mapped[str] = mapped_column(Text, default="")
    approved: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    approved_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    approved_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_utcnow)


class ResolutionRow(Base):
    __tablename__ = "resolutions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    market_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    simulated_outcome: Mapped[str | None] = mapped_column(String(16), nullable=True)
    actual_outcome: Mapped[str | None] = mapped_column(String(16), nullable=True)
    match: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    opening_ref: Mapped[Decimal | None] = mapped_column(DecimalText, nullable=True)
    closing_ref: Mapped[Decimal | None] = mapped_column(DecimalText, nullable=True)
    confidence: Mapped[str] = mapped_column(String(16), default="LOW")
    methodology: Mapped[str] = mapped_column(String(32), default="UNKNOWN")
    observations: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    validated: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_utcnow, index=True)


class DecisionLogRow(Base):
    __tablename__ = "decision_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    market_id: Mapped[str] = mapped_column(String(128), index=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime, default=_utcnow, index=True)
    decision: Mapped[str] = mapped_column(String(16))
    reason_code: Mapped[str] = mapped_column(String(48), index=True)
    reason_detail: Mapped[str] = mapped_column(Text, default="")
    probability: Mapped[Decimal | None] = mapped_column(DecimalText, nullable=True)
    edge: Mapped[Decimal | None] = mapped_column(DecimalText, nullable=True)
    price: Mapped[Decimal | None] = mapped_column(DecimalText, nullable=True)
    regime: Mapped[str | None] = mapped_column(String(32), nullable=True)
    features: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    mode: Mapped[str] = mapped_column(String(8), default="PAPER")


class LatencySampleRow(Base):
    __tablename__ = "latency_samples"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    ms: Mapped[int] = mapped_column(Integer)
    market_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime, default=_utcnow, index=True)


class TradeRow(Base):
    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    market_id: Mapped[str] = mapped_column(String(128), index=True)
    client_order_id: Mapped[str] = mapped_column(String(64), index=True)
    side: Mapped[str] = mapped_column(String(8))
    outcome: Mapped[str] = mapped_column(String(8))
    price: Mapped[Decimal] = mapped_column(DecimalText)
    shares: Mapped[Decimal] = mapped_column(DecimalText)
    fee: Mapped[Decimal] = mapped_column(DecimalText, default=Decimal(0))
    realized_pnl: Mapped[Decimal] = mapped_column(DecimalText, default=Decimal(0))
    kind: Mapped[str] = mapped_column(String(16), default="FILL")
    mode: Mapped[str] = mapped_column(String(8), default="PAPER")
    ts: Mapped[datetime] = mapped_column(UTCDateTime, default=_utcnow, index=True)


class LiveActivationRow(Base):
    __tablename__ = "live_activations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    activated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_utcnow)
    phrase_hash: Mapped[str] = mapped_column(String(128))
    actor: Mapped[str] = mapped_column(String(64), default="local")
    config_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    revoked_by: Mapped[str | None] = mapped_column(String(64), nullable=True)


class AuditLogRow(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime, default=_utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(64), default="system")
    action: Mapped[str] = mapped_column(String(64), index=True)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)


class AppStateRow(Base):
    __tablename__ = "app_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_utcnow, onupdate=_utcnow)


class EquitySnapshotRow(Base):
    __tablename__ = "equity_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime, default=_utcnow, index=True)
    balance: Mapped[Decimal] = mapped_column(DecimalText)
    equity: Mapped[Decimal] = mapped_column(DecimalText)
    exposure: Mapped[Decimal] = mapped_column(DecimalText, default=Decimal(0))
    daily_pnl: Mapped[Decimal] = mapped_column(DecimalText, default=Decimal(0))
    mode: Mapped[str] = mapped_column(String(8), default="PAPER")


class TwapObservationRow(Base):
    """Recent Chainlink TWAP observations, so a restart does not blank the oracle history.

    Without these, a restart in the middle of a 5-minute window loses the opening reference and the
    coverage the resolution simulation needs, and that market can no longer be validated. Only a
    short rolling window is kept (see ``BotEngine.TWAP_RETAIN_MINUTES``).
    """

    __tablename__ = "twap_observations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    value: Mapped[Decimal] = mapped_column(DecimalText)
    source: Mapped[str] = mapped_column(String(64), default="")
    symbol: Mapped[str] = mapped_column(String(32), default="btcusd")
    window_seconds: Mapped[int] = mapped_column(Integer, default=60)
    sequence: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
