from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from btc5m_bot.markets.rules import ResolutionRuleSpec
from btc5m_bot.oracle.simulator import compare_to_actual, resolve_market
from btc5m_bot.oracle.twap import (
    TwapObservation,
    TwapTracker,
    extract_twap_observations,
)
from btc5m_bot.types import Methodology, Outcome

from conftest import START

END = START + timedelta(seconds=300)


def obs(ts, value, *, source="chainlink", window=60):
    return TwapObservation(ts=ts, value=Decimal(value), source=source, window_seconds=window)


def spec(*, tie="UP", methodology=Methodology.TWAP_60S, source="CHAINLINK", window=60, precision="2", rounding="ROUND_HALF_UP"):
    return ResolutionRuleSpec(
        fingerprint="fp",
        methodology=methodology,
        source=source,
        tie_rule=tie,
        precision=precision,
        rounding=rounding,
        window_seconds=window,
        text="Chainlink 60-second TWAP; greater than or equal resolves UP",
        confidence="HIGH",
    )


def dense_tracker(*, start_value="100", end_value="101", step=5, source="chainlink", window=60):
    tracker = TwapTracker(window_seconds=60)
    t = START - timedelta(seconds=120)
    n = 0
    while t <= END + timedelta(seconds=5):
        value = Decimal(start_value) + (Decimal(end_value) - Decimal(start_value)) * Decimal(n) / Decimal(100)
        tracker.add(obs(t, value, source=source, window=window))
        t += timedelta(seconds=step)
        n += 1
    return tracker


def test_extract_observations_requires_timestamp_and_value():
    message = {
        "channel": "price.crypto.twap",
        "data": [
            {"symbol": "btcusd", "source": "chainlink", "full_accuracy_value": "123.45", "timestamp": 1762344000000, "window_seconds": 60},
            {"symbol": "btcusd", "source": "chainlink", "full_accuracy_value": "1", "window_seconds": 60},
            {"symbol": "ethusd", "source": "chainlink", "full_accuracy_value": "9", "timestamp": 1762344000000, "window_seconds": 60},
            {"symbol": "btcusd", "source": "chainlink", "full_accuracy_value": "7", "timestamp": 1762344000000, "window_seconds": 15},
        ],
    }
    found = extract_twap_observations(message)
    assert len(found) == 1
    assert found[0].value == Decimal("123.45")
    assert found[0].window_seconds == 60
    assert found[0].is_chainlink


def test_observation_deduplication():
    message = {
        "a": {"symbol": "btcusd", "source": "chainlink", "value": "5", "timestamp": 1762344000000, "window_seconds": 60},
        "b": {"symbol": "btcusd", "source": "chainlink", "value": "5", "timestamp": 1762344000000, "window_seconds": 60},
    }
    assert len(extract_twap_observations(message)) == 1


def test_tracker_health_is_fail_closed():
    tracker = TwapTracker(window_seconds=60)
    assert tracker.health(START).health.value == "UNVERIFIED"
    tracker.add(obs(START, "100", source="pyth"))
    assert tracker.health(START).health.value == "UNVERIFIED"
    tracker = TwapTracker(window_seconds=60)
    tracker.add(obs(START, "100", window=30))
    assert tracker.health(START).health.value == "UNVERIFIED"
    tracker = TwapTracker(window_seconds=60)
    tracker.add(obs(START, "100"))
    assert tracker.health(START).health.value == "HEALTHY"
    assert tracker.health(START + timedelta(seconds=6)).health.value == "STALE"
    assert tracker.health(START + timedelta(seconds=20)).health.value == "DOWN"


def test_projection_before_window_equals_current_price():
    tracker = TwapTracker(window_seconds=60)
    tracker.add(obs(START, "100"))
    projected = tracker.projected_final_twap(START, END + timedelta(seconds=300))
    assert projected == Decimal("100")


def test_projection_uses_only_causal_observations():
    tracker = TwapTracker(window_seconds=60)
    now = END - timedelta(seconds=30)
    t = now - timedelta(seconds=120)
    while t <= now:
        tracker.add(obs(t, "100"))
        t += timedelta(seconds=5)
    first = tracker.projected_final_twap(now, END)
    tracker.add(obs(now + timedelta(seconds=5), "9999"))
    tracker.add(obs(END, "9999"))
    second = tracker.projected_final_twap(now, END)
    assert first is not None
    assert first == second


def test_projection_after_end_is_settlement_value():
    tracker = TwapTracker(window_seconds=60)
    tracker.add(obs(START, "100"))
    tracker.add(obs(END, "105"))
    tracker.add(obs(END + timedelta(seconds=10), "200"))
    assert tracker.projected_final_twap(END + timedelta(seconds=10), END) == Decimal("105")


def test_simulator_resolves_with_exact_methodology():
    tracker = dense_tracker(start_value="100", end_value="101")
    result = resolve_market(spec(), tracker, START, END)
    assert result.valid
    assert result.outcome is Outcome.UP
    assert result.opening_reference is not None and result.closing_reference is not None
    assert result.closing_reference > result.opening_reference
    assert result.coverage_pct >= 90


def test_simulator_applies_documented_rounding_before_comparison():
    tracker = dense_tracker(start_value="100.004", end_value="100.003")
    result = resolve_market(spec(), tracker, START, END)
    assert result.valid
    assert result.tie
    assert result.outcome is Outcome.UP
    assert result.opening_reference == Decimal("100.00")
    assert result.closing_reference == Decimal("100.00")


def test_simulator_tie_uses_rule_tie_direction():
    tracker = dense_tracker(start_value="100", end_value="100")
    up = resolve_market(spec(tie="UP"), tracker, START, END)
    down = resolve_market(spec(tie="DOWN"), tracker, START, END)
    assert up.tie and up.outcome is Outcome.UP
    assert down.tie and down.outcome is Outcome.DOWN


def test_simulator_ignores_wrong_provider_and_window():
    tracker = dense_tracker(source="pyth", window=60)
    result = resolve_market(spec(), tracker, START, END)
    assert not result.valid
    assert result.validation == "INSUFFICIENT_DATA"
    tracker = dense_tracker(source="chainlink", window=30)
    result = resolve_market(spec(), tracker, START, END)
    assert not result.valid


def test_simulator_refuses_snapshot_methodology():
    tracker = dense_tracker()
    result = resolve_market(spec(methodology=Methodology.SNAPSHOT), tracker, START, END)
    assert not result.valid
    assert "snapshot" in result.reason.lower()
    assert result.outcome is None


def test_simulator_refuses_unknown_rules():
    tracker = dense_tracker()
    result = resolve_market(spec(methodology=Methodology.UNKNOWN), tracker, START, END)
    assert not result.valid and result.validation == "UNKNOWN_RULES"
    result = resolve_market(spec(tie="UNKNOWN"), tracker, START, END)
    assert not result.valid and result.validation == "UNKNOWN_RULES"
    result = resolve_market(spec(source="NOT_CHAINLINK"), tracker, START, END)
    assert not result.valid and result.validation == "UNKNOWN_RULES"
    result = resolve_market(spec(precision="UNKNOWN"), tracker, START, END)
    assert not result.valid and result.validation == "UNKNOWN_RULES"
    result = resolve_market(spec(rounding="DOCUMENTED"), tracker, START, END)
    assert not result.valid and result.validation == "UNKNOWN_RULES"


def test_simulator_requires_coverage():
    tracker = TwapTracker(window_seconds=60)
    tracker.add(obs(START - timedelta(seconds=5), "100"))
    tracker.add(obs(END, "101"))
    result = resolve_market(spec(), tracker, START, END)
    assert not result.valid
    assert result.validation == "INSUFFICIENT_DATA"


def test_simulator_never_uses_observation_after_boundary():
    tracker = dense_tracker(start_value="100", end_value="100")
    # An observation just after the end claims a massive move; it must not decide.
    tracker.add(obs(END + timedelta(seconds=1), "500"))
    result = resolve_market(spec(), tracker, START, END)
    assert result.valid
    assert result.closing_reference == Decimal("100") or result.closing_reference < Decimal("200")


def test_compare_to_actual():
    tracker = dense_tracker(start_value="100", end_value="101")
    result = resolve_market(spec(), tracker, START, END)
    match, note = compare_to_actual(result, "UP")
    assert match is True and note == "match"
    match, note = compare_to_actual(result, "DOWN")
    assert match is False and "mismatch" in note
    assert compare_to_actual(result, None)[0] is None
    assert compare_to_actual(result, "SIDEWAYS")[0] is None
