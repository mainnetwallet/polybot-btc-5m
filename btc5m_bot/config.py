"""Configuration: immutable safety limits, user runtime settings, and secrets."""
from __future__ import annotations

import json
import os
import tempfile
import threading
from decimal import Decimal
from pathlib import Path
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .types import FeatureMode, Mode

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

    HARD_RISK_PER_TRADE: ClassVar[Decimal] = Decimal("0.02")
    HARD_DAILY_LOSS: ClassVar[Decimal] = Decimal("0.04")
    HARD_MAX_POSITIONS: ClassVar[int] = 1
    HARD_MAX_ENTRIES_PER_MARKET: ClassVar[int] = 1
    HARD_MAX_CONSECUTIVE_LOSSES: ClassVar[int] = 2
    HARD_ENTRY_WINDOW_SECONDS: ClassVar[int] = 120
    HARD_MAX_ACK_LATENCY_MS: ClassVar[int] = 3000

    mode: Mode = Mode.PAPER
    capital_usd: Decimal | None = None
    risk_per_trade_pct: Decimal = Decimal("0.02")
    daily_loss_limit_pct: Decimal = Decimal("0.04")
    daily_profit_lock_pct: Decimal | None = None
    max_consecutive_losses: int = 2
    cooldown_minutes: int = 15
    max_active_positions: int = 1
    max_entries_per_market: int = 1
    entry_window_seconds: int = 120

    min_probability: Decimal = Decimal("0.60")
    min_net_edge: Decimal = Decimal("0.05")
    max_spread: Decimal = Decimal("0.03")
    max_slippage: Decimal = Decimal("0.01")
    min_depth_multiple: Decimal = Decimal("3.0")

    take_profit_roi: Decimal = Decimal("0.20")
    dynamic_take_profit: bool = True
    dynamic_tp_min_multiple: Decimal = Decimal("0.75")
    dynamic_tp_max_multiple: Decimal = Decimal("1.50")
    stop_loss_roi: Decimal = Decimal("-0.25")
    trailing_stop_pct: Decimal = Decimal("0.15")
    momentum_reversal_z: Decimal = Decimal("1.5")
    min_hold_seconds_before_reversal: int = 10
    liquidity_exit_depth_drop_pct: Decimal = Decimal("0.60")
    liquidity_exit_spread_multiple: Decimal = Decimal("2.0")
    time_exit_seconds_before_end: int = 30

    min_oracle_coverage_seconds: int = 60
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
            "correlation_shadow": FeatureMode.SHADOW,
            "partial_exits": FeatureMode.SHADOW,
            "trailing_stop": FeatureMode.SHADOW,
            "ml_model": FeatureMode.OFF,
            "feed_failover": FeatureMode.OFF,
        }
    )

    taker_delay_ms: int = 250
    fee_buffer: Decimal = Decimal("0.002")

    @field_validator("capital_usd")
    @classmethod
    def _capital_positive(cls, v: Decimal | None) -> Decimal | None:
        if v is not None and v <= 0:
            raise ValueError("capital_usd must be positive when set")
        return v

    @field_validator("risk_per_trade_pct")
    @classmethod
    def _risk_limit(cls, v: Decimal) -> Decimal:
        if v <= 0 or v > cls.HARD_RISK_PER_TRADE:
            raise HardLimitError("risk_per_trade_pct must be > 0 and <= 0.02 (immutable 2% cap)")
        return v

    @field_validator("daily_loss_limit_pct")
    @classmethod
    def _daily_limit(cls, v: Decimal) -> Decimal:
        if v <= 0 or v > cls.HARD_DAILY_LOSS:
            raise HardLimitError("daily_loss_limit_pct must be > 0 and <= 0.04 (immutable 4% cap)")
        return v

    @field_validator("daily_profit_lock_pct")
    @classmethod
    def _profit_lock(cls, v: Decimal | None) -> Decimal | None:
        if v is not None and (v <= 0 or v > Decimal("1")):
            raise ValueError("daily_profit_lock_pct must be in (0, 1] or null")
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

    @field_validator("entry_window_seconds")
    @classmethod
    def _window(cls, v: int) -> int:
        if v <= 0 or v > cls.HARD_ENTRY_WINDOW_SECONDS:
            raise HardLimitError("entry_window_seconds must be <= 120 (immutable cap)")
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
        if v >= 0 or v < Decimal("-1"):
            raise ValueError("stop_loss_roi must be in [-1, 0)")
        return v

    @field_validator("momentum_reversal_z")
    @classmethod
    def _momentum_z(cls, v: Decimal) -> Decimal:
        if v < 0:
            raise ValueError("momentum_reversal_z must be non-negative")
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
        if v <= 0:
            raise ValueError("take_profit_roi must be positive")
        return v

    @field_validator("dynamic_tp_min_multiple")
    @classmethod
    def _tp_min(cls, v: Decimal) -> Decimal:
        if not (Decimal(0) < v <= Decimal(1)):
            raise ValueError("dynamic_tp_min_multiple must be in (0, 1]")
        return v

    @field_validator("dynamic_tp_max_multiple")
    @classmethod
    def _tp_max(cls, v: Decimal) -> Decimal:
        if v < 1:
            raise ValueError("dynamic_tp_max_multiple must be >= 1")
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

    @model_validator(mode="after")
    def _dynamic_tp_bounds(self) -> "RuntimeConfig":
        if self.dynamic_tp_min_multiple > self.dynamic_tp_max_multiple:
            raise ValueError("dynamic_tp_min_multiple must not exceed dynamic_tp_max_multiple")
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


class ConfigStore:
    """Thread-safe, atomic persistence for RuntimeConfig."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._config = self._load()
        self._revision = 0

    def _load(self) -> RuntimeConfig:
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                return RuntimeConfig.model_validate(raw)
            except Exception as exc:
                raise RuntimeError(f"invalid runtime config at {self.path}: {exc}") from exc
        cfg = RuntimeConfig()
        self._write(cfg)
        return cfg

    def _write(self, cfg: RuntimeConfig) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(cfg.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
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

    def set_capital(self, capital: Decimal | None) -> tuple[RuntimeConfig, RuntimeConfig]:
        return self.update({"capital_usd": capital})


def load_settings() -> Settings:
    settings = Settings()
    settings.ensure_dirs()
    return settings
