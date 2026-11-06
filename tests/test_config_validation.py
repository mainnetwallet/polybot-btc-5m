"""Range validation for every numeric RuntimeConfig tunable (dashboard-writable)."""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from btc5m_bot.api.app import CONFIG_WRITABLE
from btc5m_bot.config import RuntimeConfig

ROOT = Path(__file__).resolve().parents[1]

BAD_VALUES = [
    ("cooldown_minutes", 0),
    ("cooldown_minutes", -5),
    ("cooldown_minutes", 1441),
    ("trailing_stop_pct", "0"),
    ("trailing_stop_pct", "-0.1"),
    ("trailing_stop_pct", "1"),
    ("min_depth_multiple", "0"),
    ("min_depth_multiple", "0.5"),
    ("min_depth_multiple", "101"),
    ("min_hold_seconds_before_reversal", -1),
    ("min_hold_seconds_before_reversal", 301),
    ("time_exit_seconds_before_end", -1),
    ("time_exit_seconds_before_end", 301),
    ("min_oracle_coverage_seconds", 0),
    ("min_oracle_coverage_seconds", 301),
    ("live_validation_min_markets", 0),
    ("paper_validation_min_trades", 0),
    ("max_data_age_ms", 0),
    ("max_data_age_ms", 60001),
    ("max_book_age_ms", 0),
    ("max_book_age_ms", 60001),
    ("max_latency_p95_ms", 0),
    ("max_latency_p95_ms", 60001),
    ("taker_delay_ms", -1),
    ("taker_delay_ms", 5001),
    ("max_spread", "1.5"),
    ("max_slippage", "1.5"),
    ("min_net_edge", "1"),
    ("fee_buffer", "1"),
    ("momentum_reversal_z", "-1"),
    ("momentum_reversal_z", "11"),
]


@pytest.mark.parametrize(("field", "value"), BAD_VALUES)
def test_out_of_range_tunables_are_rejected(field, value):
    with pytest.raises(ValidationError):
        RuntimeConfig.model_validate({field: value})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("cooldown_minutes", 1),
        ("cooldown_minutes", 1440),
        ("trailing_stop_pct", "0.5"),
        ("min_depth_multiple", "1"),
        ("min_hold_seconds_before_reversal", 0),
        ("time_exit_seconds_before_end", 0),
        ("min_oracle_coverage_seconds", 1),
        ("taker_delay_ms", 0),
        ("max_spread", "0.05"),
        ("max_slippage", "0"),
    ],
)
def test_sane_boundary_values_are_accepted(field, value):
    RuntimeConfig.model_validate({field: value})


def test_defaults_are_valid():
    cfg = RuntimeConfig()
    assert cfg.cooldown_minutes >= 1


def test_config_example_json_validates_and_is_complete():
    raw = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
    cfg = RuntimeConfig.model_validate(raw)
    assert "auto_approve_rules" in raw
    assert cfg.auto_approve_rules == raw["auto_approve_rules"]


def test_every_writable_key_is_a_real_field():
    assert CONFIG_WRITABLE <= set(RuntimeConfig.model_fields)


# The Settings page is deliberately short: essentials plus a small Advanced box. These keys are still
# accepted by the API and keep their stored/default value, but have no control on the page. A NEW
# writable key must either get a control or be added here on purpose, so nothing is forgotten silently.
NOT_ON_SETTINGS_PAGE = {
    "daily_profit_lock_usd", "max_slippage", "min_depth_multiple", "dynamic_take_profit",
    "dynamic_tp_min_multiple", "trailing_stop_pct", "momentum_reversal_z",
    "min_hold_seconds_before_reversal", "liquidity_exit_depth_drop_pct", "liquidity_exit_spread_multiple",
    "time_exit_seconds_before_end", "min_oracle_coverage_seconds", "strategy_mode", "feature_modes",
    "max_data_age_ms", "max_book_age_ms", "max_ack_latency_ms", "max_latency_p95_ms", "taker_delay_ms",
    "fee_buffer", "auto_approve_rules", "min_order_notional_usd",
}


def test_every_writable_key_is_on_the_settings_page_or_deliberately_left_off():
    js = (ROOT / "btc5m_bot" / "web" / "app.js").read_text(encoding="utf-8")
    on_page = {key for key in CONFIG_WRITABLE if key in js}
    # "trading_enabled" is sent by the Trading switch, the rest by the form.
    unexplained = sorted(CONFIG_WRITABLE - on_page - NOT_ON_SETTINGS_PAGE)
    assert not unexplained, f"writable but neither on the Settings page nor listed as left off: {unexplained}"
    assert NOT_ON_SETTINGS_PAGE <= CONFIG_WRITABLE, "stale entries in NOT_ON_SETTINGS_PAGE"


def test_settings_form_does_not_block_save_with_native_validation():
    # Browser step/min/max validation silently cancels submit for values the server would accept
    # or explain; the server's range checks are the source of truth.
    html = (ROOT / "btc5m_bot" / "web" / "index.html").read_text(encoding="utf-8")
    form_tag = html[html.index('<form id="settings-form"'):].split(">", 1)[0]
    assert "novalidate" in form_tag
