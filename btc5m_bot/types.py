"""Shared enums and value objects. No I/O or SDK imports live here."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Iterable


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class Mode(str, Enum):
    PAPER = "PAPER"
    LIVE = "LIVE"


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class Outcome(str, Enum):
    UP = "UP"
    DOWN = "DOWN"

    @property
    def gamma_label(self) -> str:
        return "Up" if self is Outcome.UP else "Down"


class MarketVersion(str, Enum):
    V1 = "v1"
    V2 = "v2"

    @classmethod
    def parse(cls, value: Any) -> "MarketVersion":
        raw = str(value or "").strip().lower()
        if raw in {"v1", "1", "ctf"}:
            return cls.V1
        if raw in {"v2", "2", "position"}:
            return cls.V2
        raise ValueError(f"unsupported market version: {value!r}")


class MarketState(str, Enum):
    DISCOVERED = "DISCOVERED"
    ACTIVE = "ACTIVE"
    ENTRY_CLOSED = "ENTRY_CLOSED"
    RESOLVING = "RESOLVING"
    SETTLED = "SETTLED"
    ABORTED = "ABORTED"


class FeedHealth(str, Enum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    STALE = "STALE"
    DOWN = "DOWN"
    UNVERIFIED = "UNVERIFIED"

    @property
    def allows_entries(self) -> bool:
        return self is FeedHealth.HEALTHY


class FilterVerdict(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNAVAILABLE = "UNAVAILABLE"


class Regime(str, Enum):
    TRENDING = "TRENDING"
    SIDEWAYS = "SIDEWAYS"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    SUDDEN_REVERSAL = "SUDDEN_REVERSAL"
    UNKNOWN = "UNKNOWN"

    @property
    def entries_allowed(self) -> bool:
        return self in {Regime.TRENDING, Regime.SIDEWAYS}


class FeatureMode(str, Enum):
    OFF = "OFF"
    SHADOW = "SHADOW"
    ACTIVE = "ACTIVE"


class Methodology(str, Enum):
    SNAPSHOT = "SINGLE_PRICE_SNAPSHOT"
    TWAP_30S = "TWAP_30S"
    TWAP_60S = "TWAP_60S"
    UNKNOWN = "UNKNOWN"

    @property
    def window_seconds(self) -> int:
        return {Methodology.SNAPSHOT: 0, Methodology.TWAP_30S: 30, Methodology.TWAP_60S: 60}.get(self, 0)


class OrderStatus(str, Enum):
    INTENDED = "INTENDED"
    SUBMITTING = "SUBMITTING"
    ACKED = "ACKED"
    PENDING = "PENDING"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    CANCEL_PENDING = "CANCEL_PENDING"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"

    @property
    def is_terminal(self) -> bool:
        return self in {OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED}


class PositionStatus(str, Enum):
    OPEN = "OPEN"
    DUST = "DUST"
    CLOSED = "CLOSED"
    SETTLED = "SETTLED"
    REDEEMED = "REDEEMED"


class RecoveryState(str, Enum):
    STARTING = "STARTING"
    RECOVERING = "RECOVERING"
    READY = "READY"
    BLOCKED = "BLOCKED"
    STOPPING = "STOPPING"


class ReasonCode(str, Enum):
    OK = "OK"
    SYSTEM_BLOCKED = "SYSTEM_BLOCKED"
    NOT_ARMED = "NOT_ARMED"
    CAPITAL_UNSET = "CAPITAL_UNSET"
    CAPITAL_BELOW_MINIMUM = "CAPITAL_BELOW_MINIMUM"
    MARKET_NOT_ACTIVE = "MARKET_NOT_ACTIVE"
    MARKET_NOT_VALIDATED = "MARKET_NOT_VALIDATED"
    MARKET_VERSION_UNSUPPORTED = "MARKET_VERSION_UNSUPPORTED"
    ENTRY_WINDOW_CLOSED = "ENTRY_WINDOW_CLOSED"
    ENTRY_ALREADY_USED = "ENTRY_ALREADY_USED"
    ACTIVE_POSITION = "ACTIVE_POSITION"
    RECOVERY_NOT_READY = "RECOVERY_NOT_READY"
    FEED_UNHEALTHY = "FEED_UNHEALTHY"
    ORACLE_UNHEALTHY = "ORACLE_UNHEALTHY"
    ORACLE_UNVERIFIED = "ORACLE_UNVERIFIED"
    ORACLE_DATA_INSUFFICIENT = "ORACLE_DATA_INSUFFICIENT"
    RULES_UNVERIFIED = "RULES_UNVERIFIED"
    RULES_UNAPPROVED = "RULES_UNAPPROVED"
    STALE_DATA = "STALE_DATA"
    STALE_BOOK = "STALE_BOOK"
    SPREAD_TOO_WIDE = "SPREAD_TOO_WIDE"
    SLIPPAGE_TOO_HIGH = "SLIPPAGE_TOO_HIGH"
    NO_SIGNAL = "NO_SIGNAL"
    LOW_PROBABILITY = "LOW_PROBABILITY"
    INSUFFICIENT_EDGE = "INSUFFICIENT_EDGE"
    NEGATIVE_EV = "NEGATIVE_EV"
    REGIME_BLOCKED = "REGIME_BLOCKED"
    FILTER_FAILED = "FILTER_FAILED"
    FILTER_UNAVAILABLE = "FILTER_UNAVAILABLE"
    MTF_CONFLICT = "MTF_CONFLICT"
    INSUFFICIENT_DEPTH = "INSUFFICIENT_DEPTH"
    NO_FILL = "NO_FILL"
    SELL_NOT_FEASIBLE = "SELL_NOT_FEASIBLE"
    BELOW_MIN_ORDER = "BELOW_MIN_ORDER"
    RISK_LIMIT = "RISK_LIMIT"
    DAILY_LOSS_LIMIT = "DAILY_LOSS_LIMIT"
    PROFIT_LOCK = "PROFIT_LOCK"
    CONSECUTIVE_LOSS_LIMIT = "CONSECUTIVE_LOSS_LIMIT"
    COOLDOWN = "COOLDOWN"
    KILL_SWITCH = "KILL_SWITCH"
    LATENCY_BREACH = "LATENCY_BREACH"
    ACK_OUTSIDE_WINDOW = "ACK_OUTSIDE_WINDOW"
    INSUFFICIENT_BALANCE = "INSUFFICIENT_BALANCE"
    DUPLICATE_ORDER = "DUPLICATE_ORDER"
    FEES_UNVERIFIED = "FEES_UNVERIFIED"
    GEO_BLOCKED = "GEO_BLOCKED"
    LIVE_GATE_FAILED = "LIVE_GATE_FAILED"
    UNKNOWN_ERROR = "UNKNOWN_ERROR"


@dataclass(frozen=True)
class BookLevel:
    price: Decimal
    size: Decimal


@dataclass
class OrderBookSnapshot:
    asset_id: str
    timestamp: datetime
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]
    tick_size: Decimal = Decimal("0.01")
    min_order_size: Decimal = Decimal("5")
    sequence: str | None = None
    source: str = "unknown"

    def __post_init__(self) -> None:
        object.__setattr__(self, "bids", tuple(sorted(self.bids, key=lambda l: l.price, reverse=True)))
        object.__setattr__(self, "asks", tuple(sorted(self.asks, key=lambda l: l.price)))

    @property
    def best_bid(self) -> Decimal | None:
        for level in self.bids:
            if level.size > 0:
                return level.price
        return None

    @property
    def best_ask(self) -> Decimal | None:
        for level in self.asks:
            if level.size > 0:
                return level.price
        return None

    @property
    def mid(self) -> Decimal | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / Decimal(2)

    @property
    def spread(self) -> Decimal | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return self.best_ask - self.best_bid

    @property
    def spread_bps(self) -> Decimal | None:
        mid = self.mid
        if mid in (None, 0) or self.spread is None:
            return None
        return self.spread / mid * Decimal(10000)

    def depth(self, side: Side) -> Decimal:
        levels = self.asks if side is Side.BUY else self.bids
        return sum((l.size for l in levels), Decimal(0))

    def depth_notional(self, side: Side) -> Decimal:
        levels = self.asks if side is Side.BUY else self.bids
        return sum((l.price * l.size for l in levels), Decimal(0))

    def walk(self, side: Side, shares: Decimal) -> tuple[Decimal, Decimal, Decimal]:
        """Walk the book for `shares` on the given taker side.

        Returns (filled_shares, notional, worst_price). Partial fills are represented
        by filled_shares < requested shares.
        """
        levels = self.asks if side is Side.BUY else self.bids
        remaining = max(shares, Decimal(0))
        filled = Decimal(0)
        notional = Decimal(0)
        worst = None
        for level in levels:
            if remaining <= 0:
                break
            if level.size <= 0:
                continue
            take = min(level.size, remaining)
            filled += take
            notional += take * level.price
            worst = level.price
            remaining -= take
        return filled, notional, (worst if worst is not None else Decimal(0))

    def avg_exec_price(self, side: Side, shares: Decimal) -> tuple[Decimal, Decimal]:
        filled, notional, _ = self.walk(side, shares)
        if filled <= 0:
            return Decimal(0), Decimal(0)
        return notional / filled, filled

    def is_stale(self, now: datetime, max_age_ms: int) -> bool:
        age = (as_utc(now) - as_utc(self.timestamp)).total_seconds()
        return age * 1000 > max_age_ms


@dataclass
class MarketInfo:
    market_id: str
    condition_id: str
    slug: str
    question: str
    version: MarketVersion
    neg_risk: bool
    event_start: datetime
    end_date: datetime
    clob_token_ids: tuple[str, ...]
    position_ids: tuple[str, ...]
    outcomes: tuple[str, ...]
    tick_size: Decimal
    min_order_size: Decimal
    accepting_orders: bool
    fees_enabled: bool
    fee_rate: Decimal
    fee_exponent: int
    taker_only: bool
    rebate_rate: Decimal
    resolution_source: str
    description: str
    methodology: Methodology
    rules_fingerprint: str
    liquidity: Decimal
    fee_verified: bool = True
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def entry_deadline(self) -> datetime:
        from datetime import timedelta

        return self.event_start + timedelta(seconds=120)

    def outcome_index(self, outcome: Outcome) -> int:
        label = outcome.gamma_label.lower()
        for i, name in enumerate(self.outcomes):
            if name.lower() == label:
                return i
        raise ValueError(f"outcome {outcome} not present in {self.outcomes!r}")

    def asset_id(self, outcome: Outcome) -> str:
        idx = self.outcome_index(outcome)
        if self.version is MarketVersion.V1:
            ids = self.clob_token_ids
        elif self.version is MarketVersion.V2:
            ids = self.position_ids
        else:  # pragma: no cover - enum is exhaustive
            raise ValueError("unsupported market version")
        if idx >= len(ids) or not ids[idx]:
            raise ValueError(f"market {self.market_id} missing asset id for {outcome.value}")
        return str(ids[idx])

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.market_id or not self.condition_id:
            errors.append("missing market/condition id")
        try:
            MarketVersion.parse(self.version.value)
        except ValueError as exc:
            errors.append(str(exc))
        if self.end_date <= self.event_start:
            errors.append("end_date must be after event_start")
        elif abs((self.end_date - self.event_start).total_seconds() - 300) > 10:
            errors.append("market window is not 5 minutes")
        if self.tick_size <= 0:
            errors.append("tick size must be positive")
        if self.min_order_size <= 0:
            errors.append("minimum order size must be positive")
        if len(self.clob_token_ids) < 2 and self.version is MarketVersion.V1:
            errors.append("v1 market missing clob token ids")
        if len(self.position_ids) < 2 and self.version is MarketVersion.V2:
            errors.append("v2 market missing position ids")
        if self.methodology is Methodology.UNKNOWN:
            errors.append("unknown resolution methodology")
        if not self.accepting_orders:
            errors.append("market is not accepting orders")
        return errors


@dataclass
class OrderIntent:
    client_order_id: str
    idempotency_key: str
    market_id: str
    condition_id: str
    asset_id: str
    outcome: Outcome
    side: Side
    shares: Decimal
    limit_price: Decimal
    order_type: str = "FOK"
    mode: Mode = Mode.PAPER
    strategy_version: str = "BOT_BASELINE"
    created_at: datetime = field(default_factory=utcnow)
    reason: str = ""


@dataclass
class OrderResult:
    client_order_id: str
    status: OrderStatus
    exchange_order_id: str | None = None
    filled_shares: Decimal = Decimal(0)
    avg_price: Decimal = Decimal(0)
    fee_usd: Decimal = Decimal(0)
    latency_ms: int = 0
    error_code: str | None = None
    error_message: str | None = None
    raw_status: str | None = None
    transactions: tuple[str, ...] = ()

    @property
    def accepted(self) -> bool:
        return self.status in {OrderStatus.ACKED, OrderStatus.PENDING, OrderStatus.PARTIAL, OrderStatus.FILLED}

    @property
    def filled(self) -> bool:
        return self.status is OrderStatus.FILLED and self.filled_shares > 0


@dataclass(frozen=True)
class GateResult:
    name: str
    passed: bool
    reason: ReasonCode = ReasonCode.OK
    detail: str = ""


@dataclass
class FilterResult:
    name: str
    verdict: FilterVerdict
    detail: str = ""
    score: float = 0.0


@dataclass
class FeedStatus:
    name: str
    health: FeedHealth
    last_tick_at: datetime | None = None
    last_value: Decimal | None = None
    age_ms: int | None = None
    sequence: int | None = None
    detail: str = ""

    @property
    def healthy(self) -> bool:
        return self.health is FeedHealth.HEALTHY


@dataclass
class RiskDecision:
    allowed: bool
    reason: ReasonCode
    detail: str
    max_shares: Decimal = Decimal(0)
    planned_loss: Decimal = Decimal(0)
    fee_estimate: Decimal = Decimal(0)
    capital_required: Decimal = Decimal(0)


def floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    if value <= 0:
        return Decimal(0)
    return (value / step).to_integral_value(rounding="ROUND_FLOOR") * step


def clamp(value: Decimal, low: Decimal, high: Decimal) -> Decimal:
    return max(low, min(high, value))


def quantize_money(value: Decimal, places: int = 6) -> Decimal:
    quant = Decimal(1).scaleb(-places)
    return value.quantize(quant)


def mean(values: Iterable[float]) -> float | None:
    vals = list(values)
    if not vals:
        return None
    return sum(vals) / len(vals)
