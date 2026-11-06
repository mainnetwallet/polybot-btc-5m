from __future__ import annotations

from types import SimpleNamespace

import pytest

from btc5m_bot.exchange.sdk_adapter import ExchangeError, _verify_wallet_identity


def test_wallet_identity_requires_detected_signature_type_and_funder_match(settings):
    funder = "0x" + "1" * 40
    configured = settings.model_copy(update={"signature_type": 1, "funder_address": funder})
    client = SimpleNamespace(wallet_type="POLY_PROXY", wallet=funder)
    _verify_wallet_identity(client, configured)


def test_wallet_identity_rejects_signature_type_mismatch(settings):
    configured = settings.model_copy(update={"signature_type": 0})
    client = SimpleNamespace(wallet_type="DEPOSIT_WALLET", wallet="0x" + "1" * 40)
    with pytest.raises(ExchangeError, match="does not match"):
        _verify_wallet_identity(client, configured)


def test_wallet_identity_rejects_funder_mismatch(settings):
    configured = settings.model_copy(update={"signature_type": 1, "funder_address": "0x" + "1" * 40})
    client = SimpleNamespace(wallet_type="POLY_PROXY", wallet="0x" + "2" * 40)
    with pytest.raises(ExchangeError, match="does not match configured funder"):
        _verify_wallet_identity(client, configured)


def test_wallet_identity_rejects_unknown_wallet_type(settings):
    client = SimpleNamespace(wallet_type="UNKNOWN", wallet="0x" + "1" * 40)
    with pytest.raises(ExchangeError, match="unknown wallet type"):
        _verify_wallet_identity(client, settings)
