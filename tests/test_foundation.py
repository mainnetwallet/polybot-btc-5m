from __future__ import annotations

import json
from decimal import Decimal

import pytest

from btc5m_bot.api.auth import AuthManager
from btc5m_bot.config import ConfigStore, HardLimitError, RuntimeConfig
from btc5m_bot.security import hash_password, redact, verify_password
from btc5m_bot.types import Mode, floor_to_step

from btc5m_bot.exchange.fees import FeeSchedule, maker_fee, taker_fee


def test_risk_limits_are_immutable():
    with pytest.raises(HardLimitError):
        RuntimeConfig(risk_per_trade_pct=Decimal("0.021"))
    with pytest.raises(HardLimitError):
        RuntimeConfig(daily_loss_limit_pct=Decimal("0.041"))
    with pytest.raises(HardLimitError):
        RuntimeConfig(max_active_positions=2)
    with pytest.raises(HardLimitError):
        RuntimeConfig(max_entries_per_market=2)
    with pytest.raises(HardLimitError):
        RuntimeConfig(max_consecutive_losses=3)
    with pytest.raises(HardLimitError):
        RuntimeConfig(entry_window_seconds=121)
    with pytest.raises(HardLimitError):
        RuntimeConfig(max_ack_latency_ms=3001)


def test_defaults_are_conservative():
    cfg = RuntimeConfig()
    assert cfg.mode is Mode.PAPER
    assert cfg.capital_usd is None
    assert cfg.risk_per_trade_pct == Decimal("0.02")
    assert cfg.daily_loss_limit_pct == Decimal("0.04")
    assert cfg.require_rules_approval is True
    assert cfg.max_active_positions == 1


def test_config_store_is_atomic_and_validated(tmp_path):
    store = ConfigStore(tmp_path / "runtime.json")
    old, new = store.update({"min_probability": "0.67", "capital_usd": "250"})
    assert old.min_probability != new.min_probability
    assert store.snapshot().capital_usd == Decimal("250")
    raw = json.loads((tmp_path / "runtime.json").read_text())
    assert raw["min_probability"] == "0.67"
    with pytest.raises(HardLimitError):
        store.update({"risk_per_trade_pct": "0.5"})
    # rejected update leaves the previous file intact
    assert store.snapshot().risk_per_trade_pct == Decimal("0.02")


def test_fee_formula_and_disabled_fees():
    schedule = FeeSchedule(rate=Decimal("0.07"), exponent=1, enabled=True)
    fee = taker_fee(Decimal("100"), Decimal("0.5"), schedule)
    assert fee == Decimal("1.75000")
    disabled = FeeSchedule(enabled=False)
    assert taker_fee(Decimal("100"), Decimal("0.5"), disabled) == 0
    additive = FeeSchedule(rate=Decimal("0.07"), exponent=2, enabled=True)
    assert taker_fee(Decimal("100"), Decimal("0.5"), additive) < fee
    assert maker_fee(Decimal("100"), Decimal("0.5"), schedule) == 0


def test_floor_to_step_never_rounds_up():
    assert floor_to_step(Decimal("5.019"), Decimal("0.01")) == Decimal("5.01")
    assert floor_to_step(Decimal("4.999"), Decimal("0.01")) == Decimal("4.99")
    assert floor_to_step(Decimal("-1"), Decimal("0.01")) == Decimal("0")


def test_password_hashing_and_redaction():
    encoded = hash_password("long-enough-password")
    assert verify_password("long-enough-password", encoded)
    assert not verify_password("wrong", encoded)
    assert not verify_password("long-enough-password", "garbage")
    payload = {
        "private_key": "0x" + "a" * 64,
        "nested": {"api_secret": "abc", "safe": "0x" + "b" * 64},
    }
    redacted = redact(payload)
    assert redacted["private_key"] == "***REDACTED***"
    assert redacted["nested"]["api_secret"] == "***REDACTED***"
    assert "b" * 64 not in redacted["nested"]["safe"]


def test_sessions_are_signed_and_expire(settings):
    manager = AuthManager(settings)
    session = manager.login("admin", "correct-horse-battery", ip="127.0.0.1")
    token = manager.encode(session)
    decoded = manager.decode(token)
    assert decoded is not None and decoded.subject == "admin"
    assert manager.verify_csrf(decoded, session.csrf)
    assert not manager.verify_csrf(decoded, "wrong")
    assert manager.decode(token[:-2] + "aa") is None
    assert manager.decode("not-a-token") is None


def test_login_rate_limits(settings):
    manager = AuthManager(settings)
    for _ in range(5):
        with pytest.raises(Exception):
            manager.login("admin", "wrong", ip="1.2.3.4")
    with pytest.raises(Exception) as exc:
        manager.login("admin", "correct-horse-battery", ip="1.2.3.4")
    assert "too many" in str(exc.value).lower()


def test_auth_requires_configured_password(tmp_path):
    from btc5m_bot.config import Settings

    bare = Settings(project_root=tmp_path, dashboard_password_hash=None)
    with pytest.raises(Exception):
        AuthManager(bare)
