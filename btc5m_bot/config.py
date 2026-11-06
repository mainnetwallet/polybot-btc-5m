"""Configuration: immutable safety limits, user runtime settings, and secrets."""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from decimal import Decimal
from pathlib import Path
from typing import Any, ClassVar, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .types import FeatureMode, Mode

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent


class HardLimitError(Exception):
    """Raised when code attempts to relax an immutable capital-protection limit.

    Deliberately not a ValueError: pydantic converts ValueError subclasses into
    ValidationError, which would hide the safety violation behind a generic error.
    """


class RuntimeConfig(BaseModel):
    """User-tunable settings persisted in config/runtime.json.

    The values in `HARD_LIMITS` cannot be relaxed by configuration, the dashboard,
    or any learning process. They are part of the capital-protection contract.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    HARD_MAX_POSITIONS: ClassVar[int] = 1
    HARD_MAX_ENTRIES_PER_MARKET: ClassVar[int] = 1
    HARD_MAX_CONSECUTIVE_LOSSES: ClassVar[int] = 2
    HARD_MAX_ACK_LATENCY_MS: ClassVar[int] = 3000

    mode: Mode = Mode.PAPER
    # Daily loss limit as a fixed USDC amount (trading locks for the day once realized P&L <= -this).
    daily_loss_limit_usd: Decimal = Decimal("1")
    # Optional daily profit lock as a fixed USDC amount (trading locks once realized P&L >= this).
    daily_profit_lock_usd: Decimal | None = None
    max_consecutive_losses: int = 2
    cooldown_minutes: int = 15
    max_active_positions: int = 1
    max_entries_per_market: int = 1

    min_probability: Decimal = Decimal("0.60")
    min_net_edge: Decimal = Decimal("0.05")
    max_spread: Decimal = Decimal("0.03")
    max_slippage: Decimal = Decimal("0.01")
    min_depth_multiple: Decimal = Decimal("3.0")

    take_profit_roi: Decimal = Decimal("0.20")
    dynamic_take_profit: bool = True
    dynamic_tp_min_multiple: Decimal = Decimal("0.75")
    stop_loss_roi: Decimal = Decimal("-0.25")
    trailing_stop_pct: Decimal = Decimal("0.15")
    momentum_reversal_z: Decimal = Decimal("1.5")
    min_hold_seconds_before_reversal: int = 10
    liquidity_exit_depth_drop_pct: Decimal = Decimal("0.60")
    liquidity_exit_spread_multiple: Decimal = Decimal("2.0")
    time_exit_seconds_before_end: int = 30

    min_oracle_coverage_seconds: int = 60
    # Approve every resolution rule automatically (PAPER mode only, never applied in LIVE).
    auto_approve_rules: bool = True
    require_rules_approval: bool = True
    live_validation_min_markets: int = 20
    paper_validation_min_trades: int = 300

    max_data_age_ms: int = 2500
    max_book_age_ms: int = 2000
    max_ack_latency_ms: int = 1500
    max_latency_p95_ms: int = 1200

    strategy: Literal["momentum", "reversal", "breakout", "trend_confirmation"] = "momentum"
    strategy_mode: FeatureMode = FeatureMode.ACTIVE

    feature_modes: dict[str, FeatureMode] = Field(
        default_factory=lambda: {
            "trailing_stop": FeatureMode.SHADOW,
        }
    )

    taker_delay_ms: int = 250
    fee_buffer: Decimal = Decimal("0.002")

    # Optional additional collateral-notional floor. The venue's share minimum from market/book
    # metadata always applies as well; this setting never substitutes for it.
    min_order_notional_usd: Decimal | None = Decimal("1.00")

    # Fixed dollar amount spent on every buy, default $1. This is the per-order size: the venue
    # minimum share quantity, daily loss limit, loss-streak guard and available balance still apply.
    trade_size_usd: Decimal = Decimal("1")

    # Master trading switch. OFF blocks every NEW entry (TRADING_OFF). Open positions keep being
    # managed and exited. Defaults to OFF so a fresh or restarted deploy never trades until the
    # operator turns it on. This is in addition to, not instead of, the risk limits and LIVE gate.
    trading_enabled: bool = False

    @field_validator("min_order_notional_usd")
    @classmethod
    def _min_notional(cls, v: Decimal | None) -> Decimal | None:
        if v is not None and v <= 0:
            raise ValueError("min_order_notional_usd must be positive (or null for share minimums)")
        return v

    @field_validator("trade_size_usd")
    @classmethod
    def _trade_size(cls, v: Decimal) -> Decimal:
        if v <= 0:
            raise ValueError("trade_size_usd must be positive")
        return v

    # Settings that existed in older versions but never changed any behaviour (or were replaced).
    # Saved configs that still contain them must keep loading, so they are dropped, not rejected.
    LEGACY_KEYS: ClassVar[frozenset[str]] = frozenset(
        {
            "daily_loss_limit_pct",
            "entry_window_seconds",
            "dynamic_tp_max_multiple",
            "capital_usd",
            "capital_from_balance",
            "risk_per_trade_pct",
            "daily_profit_lock_pct",
        }
    )
    LEGACY_FEATURES: ClassVar[frozenset[str]] = frozenset(
        {"correlation_shadow", "partial_exits", "ml_model", "feed_failover"}
    )

    @model_validator(mode="before")
    @classmethod
    def _drop_legacy_settings(cls, data: Any) -> Any:
        # daily_loss_limit_pct: older files stored the daily loss limit as a % of capital (now USDC).
        # entry_window_seconds: the 120 s entry cutoff is fixed in markets/lifecycle.py.
        # dynamic_tp_max_multiple: the take-profit target is never raised above take_profit_roi.
        # capital_usd / capital_from_balance / risk_per_trade_pct / daily_profit_lock_pct: Capital was
        # removed; every order is sized by trade_size_usd and the profit lock is now a USDC amount.
        if not isinstance(data, dict):
            return data
        if cls.LEGACY_KEYS & data.keys():
            data = {k: v for k, v in data.items() if k not in cls.LEGACY_KEYS}
        if "trade_size_usd" in data and data["trade_size_usd"] is None:
            # Older configs used null for "size by risk % of capital"; fall back to the default.
            data = {k: v for k, v in data.items() if k != "trade_size_usd"}
        modes = data.get("feature_modes")
        if isinstance(modes, dict) and cls.LEGACY_FEATURES & modes.keys():
            data = {**data, "feature_modes": {k: v for k, v in modes.items() if k not in cls.LEGACY_FEATURES}}
        return data

    @field_validator("daily_loss_limit_usd")
    @classmethod
    def _daily_limit(cls, v: Decimal) -> Decimal:
        if v <= 0:
            raise ValueError("daily_loss_limit_usd must be a positive USDC amount")
        return v

    @field_validator("daily_profit_lock_usd")
    @classmethod
    def _profit_lock(cls, v: Decimal | None) -> Decimal | None:
        if v is not None and v <= 0:
            raise ValueError("daily_profit_lock_usd must be a positive USDC amount or null")
        return v

    @field_validator("max_consecutive_losses")
    @classmethod
    def _consecutive(cls, v: int) -> int:
        if v < 1 or v > cls.HARD_MAX_CONSECUTIVE_LOSSES:
            raise HardLimitError("max_consecutive_losses must be 1 or 2 (immutable cap)")
        return v

    @field_validator("max_active_positions")
    @classmethod
    def _positions(cls, v: int) -> int:
        if v != 1:
            raise HardLimitError("max_active_positions is fixed at 1")
        return v

    @field_validator("max_entries_per_market")
    @classmethod
    def _entries(cls, v: int) -> int:
        if v != 1:
            raise HardLimitError("max_entries_per_market is fixed at 1")
        return v

    @field_validator("max_ack_latency_ms")
    @classmethod
    def _ack(cls, v: int) -> int:
        if v <= 0 or v > cls.HARD_MAX_ACK_LATENCY_MS:
            raise HardLimitError("max_ack_latency_ms must be <= 3000")
        return v

    @field_validator("min_probability")
    @classmethod
    def _prob(cls, v: Decimal) -> Decimal:
        if not (Decimal("0.5") <= v <= Decimal("0.95")):
            raise ValueError("min_probability must be in [0.5, 0.95]")
        return v

    @field_validator("min_net_edge", "max_spread", "max_slippage", "fee_buffer")
    @classmethod
    def _nonnegative(cls, v: Decimal) -> Decimal:
        if v < 0:
            raise ValueError("value must be non-negative")
        return v

    @field_validator("stop_loss_roi")
    @classmethod
    def _stop(cls, v: Decimal) -> Decimal:
        # -1 (a 100% loss) can never trigger for a binary share, so it would silently mean "no stop".
        if v >= 0 or v <= Decimal("-1"):
            raise ValueError("stop_loss_roi must be in (-1, 0)")
        return v

    @field_validator("momentum_reversal_z")
    @classmethod
    def _momentum_z(cls, v: Decimal) -> Decimal:
        if v < 0 or v > 10:
            raise ValueError("momentum_reversal_z must be between 0 and 10")
        return v

    @field_validator("liquidity_exit_depth_drop_pct")
    @classmethod
    def _depth_drop(cls, v: Decimal) -> Decimal:
        if not (Decimal(0) < v < Decimal(1)):
            raise ValueError("liquidity_exit_depth_drop_pct must be in (0, 1)")
        return v

    @field_validator("liquidity_exit_spread_multiple")
    @classmethod
    def _spread_multiple(cls, v: Decimal) -> Decimal:
        if v <= 1:
            raise ValueError("liquidity_exit_spread_multiple must be > 1")
        return v

    @field_validator("take_profit_roi")
    @classmethod
    def _tp(cls, v: Decimal) -> Decimal:
        if v <= 0 or v > 10:
            raise ValueError("take_profit_roi must be in (0, 10]")
        return v

    @field_validator("dynamic_tp_min_multiple")
    @classmethod
    def _tp_min(cls, v: Decimal) -> Decimal:
        if not (Decimal(0) < v <= Decimal(1)):
            raise ValueError("dynamic_tp_min_multiple must be in (0, 1]")
        return v

    # ---- range checks for the remaining numeric tunables (every one is dashboard-writable) ----
    @field_validator("cooldown_minutes")
    @classmethod
    def _cooldown(cls, v: int) -> int:
        # 0 would silently disable the loss-streak protection.
        if v < 1 or v > 1440:
            raise ValueError("cooldown_minutes must be between 1 and 1440")
        return v

    @field_validator("trailing_stop_pct")
    @classmethod
    def _trailing(cls, v: Decimal) -> Decimal:
        if not (Decimal(0) < v < Decimal(1)):
            raise ValueError("trailing_stop_pct must be in (0, 1)")
        return v

    @field_validator("min_depth_multiple")
    @classmethod
    def _depth_multiple(cls, v: Decimal) -> Decimal:
        if v < 1 or v > 100:
            raise ValueError("min_depth_multiple must be between 1 and 100 (book depth must cover the order)")
        return v

    @field_validator("min_hold_seconds_before_reversal", "time_exit_seconds_before_end")
    @classmethod
    def _seconds_within_window(cls, v: int, info: Any) -> int:
        if v < 0 or v > 300:
            raise ValueError(f"{info.field_name} must be between 0 and 300 seconds (a market lasts 300s)")
        return v

    @field_validator("min_oracle_coverage_seconds")
    @classmethod
    def _oracle_coverage(cls, v: int) -> int:
        if v < 1 or v > 300:
            raise ValueError("min_oracle_coverage_seconds must be between 1 and 300")
        return v

    @field_validator("live_validation_min_markets", "paper_validation_min_trades")
    @classmethod
    def _validation_minimums(cls, v: int, info: Any) -> int:
        if v < 1:
            raise ValueError(f"{info.field_name} must be at least 1")
        return v

    @field_validator("max_data_age_ms", "max_book_age_ms", "max_latency_p95_ms")
    @classmethod
    def _age_limits(cls, v: int, info: Any) -> int:
        if v < 1 or v > 60_000:
            raise ValueError(f"{info.field_name} must be between 1 and 60000 ms")
        return v

    @field_validator("taker_delay_ms")
    @classmethod
    def _taker_delay(cls, v: int) -> int:
        if v < 0 or v > 5000:
            raise ValueError("taker_delay_ms must be between 0 and 5000")
        return v

    @field_validator("max_spread", "max_slippage", "min_net_edge", "fee_buffer")
    @classmethod
    def _price_ratio_upper(cls, v: Decimal, info: Any) -> Decimal:
        # Prices live in [0, 1], so a spread/slippage/edge/fee above 1 is meaningless.
        limit = Decimal(1)
        if v > limit or (info.field_name in {"min_net_edge", "fee_buffer"} and v >= limit):
            raise ValueError(f"{info.field_name} must be below 1 (prices are in [0, 1])")
        return v

    @field_validator("feature_modes")
    @classmethod
    def _feature_modes(cls, v: dict[str, FeatureMode]) -> dict[str, FeatureMode]:
        forbidden = {"risk", "kill_switch", "exposure", "daily_loss", "live_gate"}
        if forbidden & {k.lower() for k in v}:
            raise HardLimitError("risk protections cannot be configured as experimental features")
        return v

    @model_validator(mode="after")
    def _live_mode_needs_activation(self) -> "RuntimeConfig":
        # LIVE is representable but the engine will refuse it without a persisted,
        # user-confirmed activation and a fully passing LIVE gate.
        return self

    def feature(self, name: str) -> FeatureMode:
        return self.feature_modes.get(name, FeatureMode.OFF)

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class Settings(BaseSettings):
    """Infrastructure and secret settings loaded from environment / .env."""

    model_config = SettingsConfigDict(
        env_prefix="POLYBOT_",
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        # An empty "POLYBOT_X=" line means "not set". Without this it loads as an empty
        # string, so the "is None" guards (e.g. the dashboard password check) never fire.
        env_ignore_empty=True,
        # Settings.model_dump() is handed to the engine subprocess as Settings(**dict); fields that
        # declare an alias must also be accepted under their own name there.
        populate_by_name=True,
    )

    project_root: Path = BASE_DIR
    data_dir: Path | None = None
    logs_dir: Path | None = None
    config_dir: Path | None = None
    db_filename: str = "polybot.db"
    parquet_dirname: str = "parquet"

    host: str = "127.0.0.1"
    port: int = 8765
    dashboard_username: str = "admin"
    dashboard_password_hash: SecretStr | None = None
    session_secret: SecretStr | None = None
    session_max_age_seconds: int = 8 * 3600
    allow_insecure_dashboard: bool = False

    gamma_url: str = "https://gamma-api.polymarket.com"
    clob_url: str = "https://clob.polymarket.com"
    data_api_url: str = "https://data-api.polymarket.com"
    polybolt_url: str = "wss://ws-live-v2.polymarket.com/ws"
    geo_url: str = "https://polymarket.com/api/geoblock"

    private_key: SecretStr | None = None
    signature_type: int = 0
    funder_address: str | None = None
    clob_api_key: SecretStr | None = None
    clob_api_secret: SecretStr | None = None
    clob_api_passphrase: SecretStr | None = None

    # Durable database for EVERYTHING the bot stores: dashboard settings, orders, positions, paper
    # trades, resolution rules, risk state, audit log. On hosts without a persistent disk (Render
    # free) local files are wiped on every restart, so point this at any always-on Postgres
    # (Render Postgres, Neon, Supabase, ...). POLYBOT_DATABASE_URL, or the standard DATABASE_URL that
    # Render injects when a Postgres is linked. Unset = local SQLite file + runtime.json only.
    # POLYBOT_SETTINGS_DATABASE_URL is the old name and is still read so existing deploys keep working.
    database_url: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "POLYBOT_DATABASE_URL", "POLYBOT_SETTINGS_DATABASE_URL", "DATABASE_URL", "database_url"
        ),
    )

    # Persistent defaults. On hosts without a persistent disk (Render free) runtime.json and the
    # database are wiped on every restart, so rule approvals would have to be set again by hand.
    # These values are re-applied on every start instead.
    # POLYBOT_PAPER_START_BALANCE_USD (default 100): virtual cash a fresh PAPER account starts with.
    paper_start_balance_usd: Decimal = Field(default=Decimal("100"), gt=0)
    # POLYBOT_APPROVED_RULE_FINGERPRINTS: comma separated fingerprints that are approved as soon
    # as they are seen, in any mode.
    approved_rule_fingerprints: str | None = None

    @property
    def pinned_rule_fingerprints(self) -> set[str]:
        raw = self.approved_rule_fingerprints or ""
        return {part.strip().lower() for part in raw.split(",") if part.strip()}

    binance_ws: str = "wss://stream.binance.com:9443/ws/btcusdt@trade"
    coinbase_ws: str = "wss://ws-feed.exchange.coinbase.com"

    heartbeat_enabled: bool = False
    heartbeat_dedicated_key_confirmed: bool = False
    heartbeat_interval_seconds: int = 5
    heartbeat_lease_seconds: int = 8
    supervisor_max_restarts_per_hour: int = 5
    single_instance_lock: str = "polybot.lock"

    # Public health listener for Render / UptimeRobot (btc5m_bot.healthserver). It is separate
    # from the loopback-only dashboard and exposes liveness/readiness only. Off unless a port is
    # configured: set POLYBOT_HEALTH_PORT, or run on Render, which injects PORT.
    health_host: str = "0.0.0.0"
    health_port: int | None = Field(default=None, ge=1, le=65535)
    health_max_lease_age_seconds: float = Field(default=60.0, gt=0)
    # Serve the (password-protected) operator dashboard through the public listener by proxying
    # it to the loopback dashboard. None = automatic: on when running on Render AND a dashboard
    # password hash is configured. POLYBOT_PUBLIC_DASHBOARD=false forces health-only.
    public_dashboard: bool | None = None

    @property
    def public_dashboard_enabled(self) -> bool:
        """True when the public listener should proxy non-health paths to the dashboard.

        Never on without a dashboard password hash: the dashboard is the only thing standing
        between the internet and the operator controls.
        """
        if self.dashboard_password_hash is None:
            return False
        if self.public_dashboard is not None:
            return self.public_dashboard
        return bool(os.environ.get("RENDER"))

    @property
    def health_listen_port(self) -> int | None:
        """Explicit POLYBOT_HEALTH_PORT, else Render's injected PORT, else disabled."""
        if self.health_port is not None:
            return self.health_port
        port = os.environ.get("PORT", "")
        if os.environ.get("RENDER") and port.isdigit() and 1 <= int(port) <= 65535:
            return int(port)
        return None

    @property
    def data_path(self) -> Path:
        return Path(self.data_dir) if self.data_dir else self.project_root / "data"

    @property
    def logs_path(self) -> Path:
        return Path(self.logs_dir) if self.logs_dir else self.project_root / "logs"

    @property
    def config_path(self) -> Path:
        return Path(self.config_dir) if self.config_dir else self.project_root / "config"

    @property
    def db_path(self) -> Path:
        return self.data_path / self.db_filename

    @property
    def external_database_url(self) -> str | None:
        """The configured external database URL (normalised for SQLAlchemy), or None."""
        if self.database_url is None:
            return None
        raw = self.database_url.get_secret_value().strip()
        return normalize_database_url(raw) if raw else None

    @property
    def uses_external_database(self) -> bool:
        return self.external_database_url is not None

    @property
    def database_target(self) -> "Path | str":
        """What db.engine.make_engine takes: the external URL, else the local SQLite path."""
        return self.external_database_url or self.db_path

    @property
    def sqlalchemy_database_url(self) -> str:
        """Same target as a SQLAlchemy URL string (used by Alembic)."""
        return self.external_database_url or f"sqlite:///{self.db_path.as_posix()}"

    @property
    def database_label(self) -> str:
        """Human readable location with credentials removed, safe for logs and the CLI."""
        url = self.external_database_url
        if url is None:
            return str(self.db_path)
        from sqlalchemy.engine import make_url

        return make_url(url).render_as_string(hide_password=True)

    @property
    def parquet_path(self) -> Path:
        return self.data_path / self.parquet_dirname

    @property
    def runtime_config_file(self) -> Path:
        return self.config_path / "runtime.json"

    @property
    def live_flag_file(self) -> Path:
        return self.config_path / "live.flag"

    def ensure_dirs(self) -> None:
        for path in (self.data_path, self.logs_path, self.config_path, self.parquet_path):
            path.mkdir(parents=True, exist_ok=True)


def normalize_database_url(url: str) -> str:
    """Accept the URL forms hosts hand out and return one SQLAlchemy can use.

    Render and Heroku give `postgres://...`; SQLAlchemy 2 needs a dialect name, and we ship psycopg 3.
    """
    url = url.strip()
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


class SettingsDatabase:
    """Durable key/value row for RuntimeConfig, kept in an external database.

    Only the dashboard settings live in this table (one small JSON document). It sits in the same
    external database as the trading tables (POLYBOT_DATABASE_URL) but uses its own connection pool,
    so a slow settings save never holds up order handling.
    """

    KEY = "runtime_config"
    TABLE = "polybot_settings"

    def __init__(self, url: str) -> None:
        from sqlalchemy import Column, DateTime, MetaData, String, Table, Text, create_engine

        self.url = normalize_database_url(url)
        kwargs: dict[str, Any] = {"future": True, "pool_pre_ping": True, "pool_recycle": 240}
        if self.url.startswith("sqlite"):
            kwargs["connect_args"] = {"timeout": 30, "check_same_thread": False}
        self.engine = create_engine(self.url, **kwargs)
        self._meta = MetaData()
        self._table = Table(
            self.TABLE,
            self._meta,
            Column("key", String(64), primary_key=True),
            Column("value", Text, nullable=False),
            Column("updated_at", DateTime, nullable=False),
        )
        self._ready = False

    def _ensure(self) -> None:
        if not self._ready:
            self._meta.create_all(self.engine)
            self._ready = True

    def _retry(self, fn: Any, attempts: int = 4) -> Any:
        # Free-tier databases sleep and need a few seconds to wake; a short backoff covers that.
        last: Exception | None = None
        for i in range(attempts):
            try:
                self._ensure()
                return fn()
            except Exception as exc:  # noqa: BLE001
                last = exc
                self._ready = False
                if i + 1 < attempts:
                    time.sleep(min(2.0 * (i + 1), 6.0))
        raise RuntimeError(f"settings database unavailable: {type(last).__name__}") from last

    def load(self) -> str | None:
        from sqlalchemy import select

        def _do() -> str | None:
            with self.engine.connect() as conn:
                return conn.execute(select(self._table.c.value).where(self._table.c.key == self.KEY)).scalar_one_or_none()

        return self._retry(_do)

    def save(self, payload: str) -> None:
        from datetime import datetime, timezone

        from sqlalchemy import insert, update

        def _do() -> None:
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            with self.engine.begin() as conn:
                res = conn.execute(
                    update(self._table).where(self._table.c.key == self.KEY).values(value=payload, updated_at=now)
                )
                if res.rowcount == 0:
                    conn.execute(insert(self._table).values(key=self.KEY, value=payload, updated_at=now))

        self._retry(_do, attempts=3)

    def dispose(self) -> None:
        self.engine.dispose()


class ConfigStore:
    """Thread-safe, atomic persistence for RuntimeConfig.

    runtime.json is always written (the supervisor and heartbeat worker read it). When a settings
    database URL is configured, that database is the source of truth: every update is committed there
    BEFORE it is applied, so a save the dashboard reports as "Saved" survives a restart even on a host
    with no persistent disk. If the database write fails the update is rejected and nothing changes.
    """

    def __init__(self, path: Path, database_url: str | None = None) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._db = SettingsDatabase(database_url) if database_url else None
        self._revision = 0
        self._config = self._load()

    @classmethod
    def from_settings(cls, settings: "Settings") -> "ConfigStore":
        return cls(settings.runtime_config_file, database_url=settings.external_database_url)

    @property
    def storage(self) -> dict[str, Any]:
        """Where saves go, for the dashboard: durable = survives a restart/redeploy."""
        return {"backend": "database" if self._db is not None else "file", "durable": self._db is not None}

    def _load(self) -> RuntimeConfig:
        if self._db is not None:
            stored = self._db.load()
            if stored is not None:
                try:
                    cfg = RuntimeConfig.model_validate(json.loads(stored))
                except Exception as exc:
                    raise RuntimeError(f"invalid runtime config in settings database: {exc}") from exc
                self._write_file(cfg)  # keep the local mirror (supervisor/heartbeat read it) in sync
                log.info("runtime settings loaded from database")
                return cfg
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                cfg = RuntimeConfig.model_validate(raw)
            except Exception as exc:
                raise RuntimeError(f"invalid runtime config at {self.path}: {exc}") from exc
            if self._db is not None:
                self._db.save(self._payload(cfg))  # first run with a database: adopt the existing file
            return cfg
        cfg = RuntimeConfig()
        self._write(cfg)
        return cfg

    @staticmethod
    def _payload(cfg: RuntimeConfig) -> str:
        return json.dumps(cfg.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"

    def _write_file(self, cfg: RuntimeConfig) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = self._payload(cfg)
        fd, tmp = tempfile.mkstemp(prefix=".runtime-", suffix=".json", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def _write(self, cfg: RuntimeConfig) -> None:
        # Database first: if it fails, the exception propagates and neither copy changes.
        if self._db is not None:
            self._db.save(self._payload(cfg))
        self._write_file(cfg)

    def snapshot(self) -> RuntimeConfig:
        with self._lock:
            return self._config.model_copy(deep=True)

    @property
    def revision(self) -> int:
        with self._lock:
            return self._revision

    def update(self, patch: dict[str, Any]) -> tuple[RuntimeConfig, RuntimeConfig]:
        with self._lock:
            old = self._config
            merged = old.model_dump(mode="python")
            merged.update(patch)
            new = RuntimeConfig.model_validate(merged)
            self._write(new)
            self._config = new
            self._revision += 1
            return old, new

    def close(self) -> None:
        if self._db is not None:
            self._db.dispose()


def load_settings() -> Settings:
    settings = Settings()
    settings.ensure_dirs()
    return settings
