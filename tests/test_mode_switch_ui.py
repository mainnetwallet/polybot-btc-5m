"""The Settings page PAPER/LIVE switch must stay a front door to the gated endpoints, never a bypass.

The behaviour itself (prompt, gate pre-check, confirm, POSTs) was exercised in a real DOM with jsdom;
these tests pin the wiring that the safety argument depends on.
"""
from __future__ import annotations

import re
from pathlib import Path

from btc5m_bot.api.app import CONFIG_WRITABLE

WEB = Path(__file__).resolve().parents[1] / "btc5m_bot" / "web"
HTML = (WEB / "index.html").read_text(encoding="utf-8")
JS = (WEB / "app.js").read_text(encoding="utf-8")


def test_settings_page_has_the_mode_switch():
    settings = HTML[HTML.index('id="page-settings"'):]
    for element_id in ("mode-state", "mode-hint", "mode-paper", "mode-live"):
        assert f'id="{element_id}"' in settings, element_id


def test_mode_is_not_writable_through_the_config_api_or_the_settings_form():
    assert "mode" not in CONFIG_WRITABLE
    assert not re.search(r'patch:\s*\{[^}]*\bmode\b\s*:', JS)
    assert "key: \"mode\"" not in JS  # not one of the generated advanced fields


def _handler(name: str) -> str:
    start = JS.index(f'$("{name}").addEventListener("click"')
    return JS[start:JS.index("\n}));", start)]


def test_live_button_goes_through_the_gate_and_asks_for_the_typed_phrase():
    live = _handler("mode-live")
    assert '"/api/gate"' in live
    assert "window.prompt(" in live and "ACTIVATE LIVE TRADING" in live
    assert 'post("/api/live/activate", { phrase })' in live  # the phrase is what the person typed
    assert live.index("window.prompt(") < live.index("/api/live/activate")


def test_paper_button_confirms_and_uses_the_revoke_endpoint():
    paper = _handler("mode-paper")
    assert "window.confirm(" in paper
    assert paper.index("window.confirm(") < paper.index("/api/live/revoke")
    assert "REVOKE LIVE" in paper
    assert "OPEN position" in paper  # a stranded live position must be called out
