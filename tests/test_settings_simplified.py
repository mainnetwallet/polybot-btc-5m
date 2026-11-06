"""The Settings page is short on purpose; dead settings are gone; impossible sizing is explained."""
from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path

import pytest

from btc5m_bot.api.app import CONFIG_WRITABLE
from btc5m_bot.config import RuntimeConfig
from btc5m_bot.sizing import capital_advice

from test_analytics_api import client, login  # noqa: F401  (fixture + helper)

WEB = Path(__file__).resolve().parents[1] / "btc5m_bot" / "web"
HTML = (WEB / "index.html").read_text(encoding="utf-8")
JS = (WEB / "app.js").read_text(encoding="utf-8")

BASIC_IDS = ("cfg-capital", "cfg-tradesize", "cfg-daily", "cfg-strategy", "cfg-prob", "cfg-tp", "cfg-sl")
ADVANCED_IDS = ("cfg-risk", "cfg-edge", "cfg-spread", "cfg-maxloss", "cfg-cooldown", "cfg-rules", "cfg-capfrombal")
REMOVED = ("entry_window_seconds", "dynamic_tp_max_multiple")


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


# ---- capital advice ---------------------------------------------------------------------------
def _advice(capital, trade):
    return capital_advice(
        capital=Decimal(capital) if capital is not None else None,
        risk_pct=Decimal("0.02"),
        trade_size_usd=Decimal(trade) if trade is not None else None,
    )


def test_default_capital_and_trade_size_are_flagged_as_unable_to_trade():
    cfg = RuntimeConfig()
    advice = capital_advice(
        capital=Decimal("5"), risk_pct=cfg.risk_per_trade_pct, trade_size_usd=cfg.trade_size_usd
    )
    assert advice["ok"] is False
    assert "Raise Capital" in advice["message"]
    assert Decimal(advice["min_capital_usd"]) >= 150


def test_small_fixed_trade_that_cannot_reach_the_five_share_minimum_is_flagged():
    advice = _advice("500", "1")
    assert advice["ok"] is False and "5" in advice["message"]


def test_trade_above_the_risk_cap_is_flagged():
    assert _advice("200", "9")["ok"] is False


@pytest.mark.parametrize(("capital", "trade"), [("200", "3"), ("200", None), ("500", "5")])
def test_workable_combinations_pass(capital, trade):
    assert _advice(capital, trade) ["ok"] is True


def test_capital_unset_is_flagged():
    assert _advice(None, None)["ok"] is False


def test_config_api_returns_the_advice(client):  # noqa: F811
    test_client, _, _ = client
    headers = login(test_client)
    got = test_client.get("/api/config")
    assert got.status_code == 200 and "advice" in got.json()
    saved = test_client.post(
        "/api/config", json={"patch": {"capital_usd": "200", "trade_size_usd": "3"}}, headers=headers
    )
    assert saved.status_code == 200
    assert saved.json()["advice"]["ok"] is True
