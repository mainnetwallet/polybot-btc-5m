"""Settings saved on the dashboard must survive a restart, including on hosts that wipe the disk."""
from __future__ import annotations

import json
from decimal import Decimal

import pytest

from btc5m_bot.config import ConfigStore, RuntimeConfig, Settings, SettingsDatabase, normalize_database_url
from btc5m_bot.api.app import CONFIG_WRITABLE


def _store(tmp_path, db_name="settings.db", file_name="runtime.json"):
    return ConfigStore(tmp_path / file_name, database_url=f"sqlite:///{(tmp_path / db_name).as_posix()}")


def test_saved_settings_survive_wiped_runtime_json(tmp_path):
    """Render free plan: runtime.json is gone after a restart, the database is not."""
    store = _store(tmp_path)
    store.update({"min_probability": "0.67", "capital_usd": "250", "trading_enabled": True})
    store.close()

    (tmp_path / "runtime.json").unlink()  # the host wiped the local disk
    again = _store(tmp_path)
    cfg = again.snapshot()
    assert cfg.min_probability == Decimal("0.67")
    assert cfg.capital_usd == Decimal("250")
    assert cfg.trading_enabled is True
    # the local mirror is rebuilt for the supervisor / heartbeat worker
    assert json.loads((tmp_path / "runtime.json").read_text())["min_probability"] == "0.67"


def test_every_dashboard_writable_field_round_trips_through_the_database(tmp_path):
    store = _store(tmp_path)
    patch = {
        "trading_enabled": True,
        "capital_usd": "12.5",
        "capital_from_balance": False,
        "trade_size_usd": "2",
        "daily_loss_limit_usd": "3",
        "daily_profit_lock_pct": "0.5",
        "max_consecutive_losses": 1,
        "cooldown_minutes": 30,
        "min_probability": "0.7",
        "min_net_edge": "0.06",
        "max_spread": "0.02",
        "max_slippage": "0.02",
        "min_depth_multiple": "4",
        "take_profit_roi": "0.3",
        "dynamic_take_profit": False,
        "dynamic_tp_min_multiple": "0.5",
        "stop_loss_roi": "-0.3",
        "trailing_stop_pct": "0.2",
        "momentum_reversal_z": "2",
        "min_hold_seconds_before_reversal": 20,
        "liquidity_exit_depth_drop_pct": "0.5",
        "liquidity_exit_spread_multiple": "3",
        "time_exit_seconds_before_end": 40,
        "min_oracle_coverage_seconds": 70,
        "require_rules_approval": False,
        "strategy": "breakout",
        "strategy_mode": "SHADOW",
        "feature_modes": {"trailing_stop": "ACTIVE"},
        "max_data_age_ms": 3000,
        "max_book_age_ms": 2500,
        "max_ack_latency_ms": 2000,
        "max_latency_p95_ms": 1500,
        "taker_delay_ms": 300,
        "fee_buffer": "0.003",
        "auto_approve_rules": False,
        "min_order_notional_usd": "1.5",
        "risk_per_trade_pct": "0.01",
    }
    assert set(patch) >= CONFIG_WRITABLE, f"test does not cover: {sorted(CONFIG_WRITABLE - set(patch))}"
    saved = store.update({k: v for k, v in patch.items() if k in CONFIG_WRITABLE or k == "risk_per_trade_pct"})[1]
    store.close()

    (tmp_path / "runtime.json").unlink()
    loaded = _store(tmp_path).snapshot()
    assert loaded == saved


def test_database_wins_over_a_stale_runtime_json(tmp_path):
    store = _store(tmp_path)
    store.update({"min_probability": "0.7"})
    store.close()
    stale = RuntimeConfig(min_probability=Decimal("0.55"))
    (tmp_path / "runtime.json").write_text(json.dumps(stale.model_dump(mode="json")))
    assert _store(tmp_path).snapshot().min_probability == Decimal("0.7")


def test_first_run_with_a_database_adopts_the_existing_file(tmp_path):
    ConfigStore(tmp_path / "runtime.json").update({"min_probability": "0.66"})
    store = _store(tmp_path)
    assert store.snapshot().min_probability == Decimal("0.66")
    store.close()
    (tmp_path / "runtime.json").unlink()
    assert _store(tmp_path).snapshot().min_probability == Decimal("0.66")


def test_rejected_update_changes_neither_copy(tmp_path):
    store = _store(tmp_path)
    store.update({"min_probability": "0.7"})
    with pytest.raises(Exception):
        store.update({"min_probability": "0.99"})
    assert store.snapshot().min_probability == Decimal("0.7")
    store.close()
    (tmp_path / "runtime.json").unlink()
    assert _store(tmp_path).snapshot().min_probability == Decimal("0.7")


def test_database_failure_rejects_the_save_instead_of_pretending(tmp_path, monkeypatch):
    store = _store(tmp_path)
    store.update({"min_probability": "0.7"})
    monkeypatch.setattr("btc5m_bot.config.time.sleep", lambda _s: None)

    def boom(*_a, **_k):
        raise OSError("db down")

    monkeypatch.setattr(store._db.engine.pool, "connect", boom, raising=False)
    monkeypatch.setattr(store._db, "_ensure", boom)
    with pytest.raises(RuntimeError, match="settings database unavailable"):
        store.update({"min_probability": "0.8"})
    assert store.snapshot().min_probability == Decimal("0.7")
    assert json.loads((tmp_path / "runtime.json").read_text())["min_probability"] == "0.7"


def test_without_a_database_behaviour_is_unchanged(tmp_path):
    store = ConfigStore(tmp_path / "runtime.json")
    assert store.storage == {"backend": "file", "durable": False}
    assert _store(tmp_path, "x.db", "other.json").storage == {"backend": "database", "durable": True}


def test_database_url_forms_and_env(monkeypatch, tmp_path):
    assert normalize_database_url("postgres://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert normalize_database_url("postgresql://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert normalize_database_url("sqlite:///x.db") == "sqlite:///x.db"

    for name in ("POLYBOT_DATABASE_URL", "POLYBOT_SETTINGS_DATABASE_URL", "DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgres://u:p@h/db")
    s = Settings(data_dir=tmp_path, logs_dir=tmp_path, config_dir=tmp_path)
    assert s.database_url.get_secret_value() == "postgres://u:p@h/db"
    assert "p@h" not in repr(s)  # the password never shows up in logs
    assert "p@h" not in s.database_label  # nor in the CLI / log label
    assert s.database_target == "postgresql+psycopg://u:p@h/db"
    # the supervisor hands model_dump() to the engine subprocess: the field must survive that trip
    assert Settings(**s.model_dump()).database_url.get_secret_value() == "postgres://u:p@h/db"
    # the old variable name still works so existing deployments do not silently lose persistence
    monkeypatch.setenv("POLYBOT_SETTINGS_DATABASE_URL", "postgres://legacy/db3")
    assert Settings(data_dir=tmp_path).database_url.get_secret_value() == "postgres://legacy/db3"
    # POLYBOT_DATABASE_URL wins over both
    monkeypatch.setenv("POLYBOT_DATABASE_URL", "postgres://other/db2")
    assert Settings(data_dir=tmp_path).database_url.get_secret_value() == "postgres://other/db2"


def test_database_target_defaults_to_the_local_sqlite_file(monkeypatch, tmp_path):
    for name in ("POLYBOT_DATABASE_URL", "POLYBOT_SETTINGS_DATABASE_URL", "DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)
    s = Settings(data_dir=tmp_path, logs_dir=tmp_path, config_dir=tmp_path)
    assert not s.uses_external_database
    assert s.database_target == s.db_path
    assert s.sqlalchemy_database_url.startswith("sqlite:///")


def test_settings_database_is_a_single_upserted_row(tmp_path):
    db = SettingsDatabase(f"sqlite:///{(tmp_path / 's.db').as_posix()}")
    assert db.load() is None
    db.save('{"a": 1}')
    db.save('{"a": 2}')
    assert db.load() == '{"a": 2}'
