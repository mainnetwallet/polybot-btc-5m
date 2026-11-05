from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from btc5m_bot.exchange.gamma import DiscoveryError, parse_market
from btc5m_bot.markets.lifecycle import MarketStateMachine
from btc5m_bot.markets.rules import build_rule_spec, methodology_from_metadata
from btc5m_bot.types import MarketState, MarketVersion, Methodology

from conftest import START, make_market


def _raw_market(**overrides):
    raw = {
        "id": "123",
        "conditionId": "0xabc",
        "slug": "btc-updown-5m-1",
        "question": "BTC Up or Down?",
        "version": "1",
        "eventStartTime": "2026-11-05T12:00:00Z",
        "endDate": "2026-11-05T12:05:00Z",
        "acceptingOrders": True,
        "clobTokenIds": '["up1","down1"]',
        "outcomes": '["Up","Down"]',
        "orderPriceMinTickSize": "0.01",
        "orderMinSize": "5",
        "feesEnabled": True,
        "feeSchedule": {"rate": "0.07", "exponent": 1},
        "resolutionSource": "https://data.chain.link/streams/btc-usd",
        "description": (
            "Resolves UP when the 60-second Chainlink BTC/USD TWAP at the end is "
            "greater than or equal to the opening value. Prices rounded to 2 decimal places."
        ),
        "cryptoMarketConfig": {"twapEnabled": True, "twapLookbackSeconds": 60},
    }
    raw.update(overrides)
    return raw


def _event(market):
    return {
        "startTime": market.get("eventStartTime"),
        "endDate": market.get("endDate"),
        "version": market.get("version"),
    }


def test_parse_v1_uses_clob_token_ids():
    info = parse_market(_event(_raw_market()), _raw_market())
    assert info.version is MarketVersion.V1
    from btc5m_bot.types import Outcome

    assert info.asset_id(Outcome.UP) == "up1"
    assert info.asset_id(Outcome.DOWN) == "down1"
    assert info.min_order_size == Decimal("5")
    assert info.methodology is Methodology.TWAP_60S
    assert info.fee_verified is True


def test_parse_v2_uses_position_ids():
    market = _raw_market(version="2", positionIds='["p-up","p-down"]')
    info = parse_market(_event(market), market)
    assert info.version is MarketVersion.V2
    from btc5m_bot.types import Outcome

    assert info.asset_id(Outcome.UP) == "p-up"
    assert info.asset_id(Outcome.DOWN) == "p-down"


def test_parse_rejects_missing_ids_and_window():
    market = _raw_market(conditionId="")
    with pytest.raises(DiscoveryError):
        parse_market(_event(market), market)
    short = _raw_market(endDate="2026-11-05T12:02:00Z")
    info = parse_market(_event(short), short)
    assert any("5 minutes" in e for e in info.validate())


def test_parse_unknown_methodology_keeps_unknown():
    market = _raw_market(
        cryptoMarketConfig=None,
        description="Resolves when the price goes up.",
        resolutionSource="https://example.com",
    )
    info = parse_market(_event(market), market)
    assert info.methodology is Methodology.UNKNOWN
    assert any("methodology" in e for e in info.validate())


def test_rule_spec_parses_chainlink_tie_precision_and_rounding():
    spec = build_rule_spec(_raw_market(), START, "1")
    assert spec.methodology is Methodology.TWAP_60S
    assert spec.source == "CHAINLINK"
    assert spec.tie_rule == "UP"
    assert spec.precision == "2"
    assert spec.rounding == "DOCUMENTED"
    assert spec.confidence == "HIGH"


def test_rule_spec_strict_greater_than_is_down_tie():
    market = _raw_market(
        description=(
            "Resolves UP when the 60-second Chainlink BTC/USD TWAP at the end is "
            "strictly greater than the opening value. Rounded to 2 decimal places."
        )
    )
    spec = build_rule_spec(market, START, "1")
    assert spec.tie_rule == "DOWN"


def test_rule_fingerprint_changes_when_rules_change():
    a = build_rule_spec(_raw_market(), START, "1")
    b = build_rule_spec(_raw_market(), START, "2")
    changed_rule = _raw_market(
        description=(
            "Resolves UP when the 60-second Chainlink BTC/USD TWAP at the end is "
            "strictly greater than the opening value. Prices rounded to 2 decimal places."
        )
    )
    c = build_rule_spec(changed_rule, START, "1")
    assert a.fingerprint != b.fingerprint
    assert a.fingerprint != c.fingerprint
    assert a.fingerprint == build_rule_spec(_raw_market(), START, "1").fingerprint


def test_methodology_never_guesses_from_twap_wording():
    assert methodology_from_metadata({"description": "uses a twap"}) is Methodology.UNKNOWN
    assert (
        methodology_from_metadata({"cryptoMarketConfig": {"twapEnabled": True, "twapLookbackSeconds": 99}})
        is Methodology.UNKNOWN
    )


def test_lifecycle_enforces_120_second_cutoff():
    market = make_market()
    machine = MarketStateMachine(market)
    assert machine.state is MarketState.DISCOVERED
    assert machine.tick(START) is not None
    assert machine.state is MarketState.ACTIVE
    assert machine.entry_allowed(START + timedelta(seconds=119))
    assert not machine.entry_allowed(START + timedelta(seconds=120))
    machine.tick(START + timedelta(seconds=120))
    assert machine.state is MarketState.ENTRY_CLOSED
    machine.tick(START + timedelta(seconds=300))
    assert machine.state is MarketState.RESOLVING
    machine.settle(START + timedelta(seconds=301))
    assert machine.state is MarketState.SETTLED
    assert machine.tick(START + timedelta(seconds=400)) is None


def test_lifecycle_never_moves_backwards():
    machine = MarketStateMachine(make_market())
    machine.tick(START + timedelta(seconds=200))
    assert machine.state is MarketState.ENTRY_CLOSED
    machine.tick(START + timedelta(seconds=10))
    assert machine.state is MarketState.ENTRY_CLOSED
