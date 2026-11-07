"""The Settings page is short on purpose; dead settings are gone; impossible sizing is explained."""
from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path

import pytest

from btc5m_bot.api.app import CONFIG_WRITABLE
from btc5m_bot.config import RuntimeConfig
from btc5m_bot.sizing import trade_size_advice

from test_analytics_api import client, login  # noqa: F401  (fixture + helper)

WEB = Path(__file__).resolve().parents[1] / "btc5m_bot" / "web"
HTML = (WEB / "index.html").read_text(encoding="utf-8")
JS = (WEB / "app.js").read_text(encoding="utf-8")

BASIC_IDS = ("cfg-tradesize", "cfg-daily", "cfg-strategy", "cfg-prob", "cfg-tp", "cfg-sl")
ADVANCED_IDS = ("cfg-edge", "cfg-spread", "cfg-maxpos", "cfg-maxentries")
REMOVED = (
    "entry_window_seconds", "dynamic_tp_max_multiple",
    "capital_usd", "capital_from_balance", "risk_per_trade_pct", "daily_profit_lock_pct",
)


def test_settings_page_has_only_the_basic_and_advanced_controls():
    start = HTML.index('<form id="settings-form"')
    form = HTML[start:HTML.index("</form>", start)]
    ids = set(re.findall(r'id="((?:cfg|adv|feat)-[a-z-]+)"', form))
    assert ids == set(BASIC_IDS) | set(ADVANCED_IDS) | {"cfg-advice"}
    assert "<details" in form  # the Advanced box is collapsed by default
    assert 'id="g-advanced" open' not in form


def test_generated_advanced_and_experimental_blocks_are_gone():
    for leftover in ("ADV_FIELDS", "FEATURE_LABELS", "collectFeatureFields", "cfg-features", "cfg-advanced"):
        assert leftover not in JS and leftover not in HTML, leftover


@pytest.mark.parametrize("key", REMOVED)
def test_dead_settings_are_not_writable_or_fields(key):
    assert key not in CONFIG_WRITABLE
    assert key not in RuntimeConfig.model_fields


def test_only_wired_feature_flags_remain():
    assert set(RuntimeConfig().feature_modes) == {"trailing_stop"}


def test_old_saved_configs_with_removed_settings_still_load():
    cfg = RuntimeConfig.model_validate(
        {
            "entry_window_seconds": 90,
            "dynamic_tp_max_multiple": "2",
            "daily_loss_limit_pct": "0.1",
            "feature_modes": {"trailing_stop": "ACTIVE", "ml_model": "SHADOW", "partial_exits": "SHADOW"},
            "min_probability": "0.7",
        }
    )
    assert cfg.min_probability == Decimal("0.7")
    assert {k: v.value for k, v in cfg.feature_modes.items()} == {"trailing_stop": "ACTIVE"}


def test_api_rejects_removed_settings(client):  # noqa: F811
    test_client, _, _ = client
    headers = login(test_client)
    response = test_client.post("/api/config", json={"patch": {"entry_window_seconds": 60}}, headers=headers)
    assert response.status_code == 400


def test_old_saved_configs_with_capital_settings_still_load():
    cfg = RuntimeConfig.model_validate(
        {
            "capital_usd": "250",
            "capital_from_balance": False,
            "risk_per_trade_pct": "0.01",
            "daily_profit_lock_pct": "0.5",
            "trade_size_usd": None,  # old "size by risk %" mode
            "min_probability": "0.7",
        }
    )
    assert cfg.min_probability == Decimal("0.7")
    assert cfg.trade_size_usd == RuntimeConfig().trade_size_usd
    assert cfg.daily_profit_lock_usd is None


def test_trade_size_must_be_positive():
    with pytest.raises(ValueError):
        RuntimeConfig(trade_size_usd=Decimal("0"))


# ---- trade size advice ------------------------------------------------------------------------
def _advice(trade):
    return trade_size_advice(trade_size_usd=Decimal(trade))


def test_unset_trade_size_is_flagged_as_no_trading():
    advice = trade_size_advice(trade_size_usd=RuntimeConfig().trade_size_usd)
    assert advice["ok"] is False
    assert "not set" in advice["message"]


def test_below_dollar_minimum_is_flagged_and_workable_sizes_pass():
    low = trade_size_advice(trade_size_usd=Decimal("0.50"))
    assert low["ok"] is False and "$1" in low["message"]
    for trade in ("1", "3", "5", "20"):
        assert trade_size_advice(trade_size_usd=Decimal(trade))["ok"] is True


def test_config_api_returns_the_advice(client):  # noqa: F811
    test_client, _, _ = client
    headers = login(test_client)
    got = test_client.get("/api/config")
    assert got.status_code == 200 and "advice" in got.json()
    saved = test_client.post("/api/config", json={"patch": {"trade_size_usd": "3"}}, headers=headers)
    assert saved.status_code == 200
    assert saved.json()["advice"]["ok"] is True


# ---- daily loss limit vs trade size -----------------------------------------------------------
def _limit_advice(trade, limit, *, entries=3, positions=2):
    return trade_size_advice(
        trade_size_usd=Decimal(trade), daily_loss_limit_usd=Decimal(limit),
        max_entries_per_market=entries, max_active_positions=positions,
    )


def test_default_limit_with_a_dollar_trade_warns_that_one_loss_ends_the_day():
    advice = _limit_advice("1", "1")
    assert advice["ok"] is True  # a warning, never a refusal
    (warning,) = advice["warnings"]
    assert "One full loss reaches it" in warning
    assert "$2" in warning  # suggested minimum: 2x the trade size
    assert "$6" in warning  # worst case: $1 x 3 entries x 2 positions
    assert advice["worst_case_exposure_usd"] == "6.00"


def test_limit_under_two_trades_warns_about_two_losses():
    (warning,) = _limit_advice("3", "5")["warnings"]
    assert "less than 2 trades" in warning and "$18" in warning


def test_a_roomy_limit_has_no_warning_but_still_reports_the_exposure():
    advice = _limit_advice("3", "6")
    assert advice["warnings"] == []
    assert advice["worst_case_exposure_usd"] == "18.00"


def test_exposure_uses_the_configured_caps():
    assert _limit_advice("2", "100", entries=1, positions=1)["worst_case_exposure_usd"] == "2.00"
    assert _limit_advice("2", "100", entries=2, positions=2)["worst_case_exposure_usd"] == "8.00"


def test_no_loss_warning_when_the_trade_size_is_unset():
    advice = trade_size_advice(trade_size_usd=None, daily_loss_limit_usd=Decimal("1"))
    assert advice["warnings"] == [] and advice["worst_case_exposure_usd"] is None


def test_config_api_warns_but_does_not_change_the_users_values(client):  # noqa: F811
    test_client, _, _ = client
    headers = login(test_client)
    saved = test_client.post("/api/config", json={"patch": {"trade_size_usd": "3"}}, headers=headers)
    body = saved.json()
    assert body["advice"]["ok"] is True and body["advice"]["warnings"]
    assert body["config"]["daily_loss_limit_usd"] == "1"  # untouched
