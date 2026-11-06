from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from btc5m_bot.data.series import BtcSeries, BtcTick
from btc5m_bot.intelligence.features import FeatureSnapshot, compute_features
from btc5m_bot.intelligence.filters import FilterEngine
from btc5m_bot.intelligence.probability import ProbabilityEngine
from btc5m_bot.intelligence.regimes import RegimeDetector, RegimeState
from btc5m_bot.intelligence.strategies import StrategyRegistry
from btc5m_bot.oracle.twap import TwapObservation, TwapTracker
from btc5m_bot.types import FeatureMode, Outcome, Regime

from conftest import START, make_book

NOW = START + timedelta(seconds=60)


def series(now=NOW, *, step=1.0, start_price=Decimal("100000"), growth=Decimal("0.5"), source="binance"):
    s = BtcSeries(source)
    t = now - timedelta(seconds=240)
    price = start_price
    n = 0
    while t <= now:
        s.add(BtcTick(ts=t, price=price, qty=Decimal("0.01"), source=source, recv_ts=t))
        t += timedelta(seconds=step)
        n += 1
        price += growth
    return s


def tracker(now=NOW):
    tr = TwapTracker(window_seconds=60)
    t = now - timedelta(seconds=120)
    while t <= now:
        tr.add(TwapObservation(ts=t, value=Decimal("100030"), source="chainlink", window_seconds=60))
        t += timedelta(seconds=5)
    return tr


def features(**overrides) -> FeatureSnapshot:
    up = make_book("up", bid="0.55", ask="0.56", ts=NOW)
    down = make_book("down", bid="0.44", ask="0.45", ts=NOW)
    base = FeatureSnapshot(
        ts=NOW,
        btc_price=Decimal("100000"),
        returns={15: 0.0010, 30: 0.0018, 60: 0.0030, 180: 0.0040},
        directions={15: 1, 30: 1, 60: 1, 180: 1},
        momentum=0.0030,
        acceleration=0.00002,
        realized_vol_60s=0.0002,
        realized_vol_180s=0.0002,
        volume_30s=Decimal("20"),
        volume_180s=Decimal("120"),
        tick_rate_30s=2.0,
        mtf_agreement=0.9,
        mtf_conflict=False,
        weighted_direction=1,
        twap_current=Decimal("100050"),
        opening_reference=Decimal("100000"),
        twap_distance_bps=5.0,
        projected_final_twap=Decimal("100060"),
        remaining_seconds=240.0,
        entry_seconds_remaining=60.0,
        up_book=up,
        down_book=down,
        up_best_bid=up.best_bid,
        up_best_ask=up.best_ask,
        down_best_bid=down.best_bid,
        down_best_ask=down.best_ask,
        up_spread=up.spread,
        down_spread=down.spread,
        up_depth=Decimal("1000"),
        down_depth=Decimal("1000"),
        implied_up=0.55,
        implied_down=0.45,
        drift_per_second=0.00001,
        data_age_ms=100,
        book_age_ms=100,
        oracle_age_ms=100,
        oracle_coverage_seconds=60.0,
    )
    return replace(base, **overrides)


def test_compute_features_never_reads_the_future():
    primary = series()
    tr = tracker()
    up, down = make_book("up", ts=NOW), make_book("down", ts=NOW)
    first = compute_features(
        primary, tr, now=NOW, market_start=START, market_end=START + timedelta(seconds=300),
        up_book=up, down_book=down,
    )
    primary.add(BtcTick(ts=NOW + timedelta(seconds=1), price=Decimal("200000"), source="binance"))
    second = compute_features(
        primary, tr, now=NOW, market_start=START, market_end=START + timedelta(seconds=300),
        up_book=up, down_book=down,
    )
    assert first.btc_price == second.btc_price == Decimal(first.btc_price)
    assert first.returns == second.returns
    assert first.data_age_ms == second.data_age_ms


def test_regime_classification_and_safety():
    detector = RegimeDetector(hysteresis_ticks=1)
    high = detector.update(features(realized_vol_60s=0.005))
    assert high.regime is Regime.HIGH_VOLATILITY
    assert not high.entries_allowed
    unknown = RegimeDetector().update(features(realized_vol_60s=None))
    assert unknown.regime is Regime.UNKNOWN
    assert not unknown.entries_allowed
    trend = RegimeDetector(hysteresis_ticks=1).update(features())
    assert trend.regime is not Regime.UNKNOWN


def test_strategies_require_direction_and_agreement():
    registry = StrategyRegistry()
    regime = RegimeState(Regime.TRENDING, 0.8, ["test"], changed_at=NOW)
    sig = registry.momentum(features(), regime)
    assert sig.action == "BUY" and sig.outcome is Outcome.UP
    conflict = features(mtf_conflict=True)
    assert registry.momentum(conflict, regime).action == "NO_SIGNAL"
    shadow = registry.select("momentum", FeatureMode.SHADOW, features(), regime)
    assert shadow.action == "SHADOW" and shadow.outcome is None


def test_filters_fail_closed():
    engine = FilterEngine(max_spread=Decimal("0.03"))
    report = engine.evaluate(features(), required_shares=Decimal("10"))
    assert report.all_pass, report.summary
    wide = features(up_spread=Decimal("0.10"), down_spread=Decimal("0.10"))
    assert not engine.evaluate(wide, required_shares=Decimal("10")).all_pass
    missing = features(up_book=None, down_book=None, up_spread=None, down_spread=None)
    report = engine.evaluate(missing, required_shares=Decimal("10"))
    assert report.unavailable and not report.all_pass
    thin = features(up_depth=Decimal("1"), down_depth=Decimal("1"))
    assert not engine.evaluate(thin, required_shares=Decimal("10")).all_pass


def test_filter_detects_oracle_divergence():
    engine = FilterEngine(max_oracle_basis_bps=30)
    divergent = features(twap_current=Decimal("99000"))
    report = engine.evaluate(divergent, required_shares=Decimal("1"))
    assert any(r.name == "oracle_divergence" and r.verdict.value == "FAIL" for r in report.results)


def test_probability_shrinks_without_evidence():
    engine = ProbabilityEngine(min_coverage_seconds=60)
    regime = RegimeState(Regime.TRENDING, 0.9, [], changed_at=NOW)
    good = engine.estimate(features(), regime, rule_confidence="HIGH")
    assert good is not None and good.p_up > 0.5
    assert good.p_up + good.p_down == pytest.approx(1.0)
    stale = engine.estimate(features(oracle_coverage_seconds=10), regime, rule_confidence="HIGH")
    assert stale is None
    no_twap = engine.estimate(features(twap_current=None, opening_reference=None), regime)
    assert no_twap is None


def test_probability_never_certain():
    engine = ProbabilityEngine(min_coverage_seconds=60)
    regime = RegimeState(Regime.TRENDING, 1.0, [], changed_at=NOW)
    extreme = engine.estimate(
        features(projected_final_twap=Decimal("200000"), realized_vol_60s=1e-9),
        regime,
        rule_confidence="HIGH",
    )
    assert extreme is not None
    assert 0.0 < extreme.p_up < 1.0
    assert extreme.p_up <= 0.995


def test_probability_confidence_reflects_quality():
    engine = ProbabilityEngine(min_coverage_seconds=60)
    high_regime = RegimeState(Regime.TRENDING, 1.0, [], changed_at=NOW)
    high = engine.estimate(features(), high_regime, rule_confidence="HIGH")
    assert high is not None and high.confidence in {"MEDIUM", "HIGH"}
    low = engine.estimate(
        features(data_age_ms=2400, mtf_agreement=0.5),
        RegimeState(Regime.UNKNOWN, 0.0, [], changed_at=NOW),
        rule_confidence="LOW",
    )
    assert low is not None and low.confidence == "LOW"


@pytest.mark.parametrize("rem", [30.0, 60.0, 90.0, 180.0, 240.0, 300.0])
def test_sigma_matches_the_variance_of_a_future_60s_twap(rem):
    """sigma is the sd of the final 60 s TWAP of a random walk. For rem > 60 the whole window is still
    ahead, so Var = rem - 2*60/3 (verified by Monte Carlo); the old constant sqrt(60/3) was ~3x too small."""
    engine = ProbabilityEngine(min_coverage_seconds=60)
    regime = RegimeState(Regime.TRENDING, 0.9, [], changed_at=NOW)
    vol, price = 0.0002, 100000.0
    est = engine.estimate(features(remaining_seconds=rem, realized_vol_60s=vol), regime, rule_confidence="HIGH")
    assert est is not None
    expected = rem ** 1.5 / 3 ** 0.5 / 60 if rem <= 60 else (rem - 2 * 60 / 3) ** 0.5
    assert est.sigma == pytest.approx(price * vol * expected, rel=1e-9)


def test_sigma_grows_with_remaining_time_and_is_continuous_at_the_window_edge():
    engine = ProbabilityEngine(min_coverage_seconds=60)
    regime = RegimeState(Regime.TRENDING, 0.9, [], changed_at=NOW)

    def sigma(rem):
        return engine.estimate(features(remaining_seconds=rem), regime, rule_confidence="HIGH").sigma

    values = [sigma(r) for r in (45, 60, 61, 120, 180, 300)]
    assert values == sorted(values) and len(set(values)) == len(values)
    assert sigma(60.0001) == pytest.approx(sigma(60.0), rel=1e-4)


def test_early_in_the_window_the_model_is_less_confident_than_late():
    """Same distance from the opening reference: more time left means more uncertainty."""
    engine = ProbabilityEngine(min_coverage_seconds=60)
    regime = RegimeState(Regime.TRENDING, 1.0, [], changed_at=NOW)
    early = engine.estimate(features(remaining_seconds=290.0), regime, rule_confidence="HIGH")
    late = engine.estimate(features(remaining_seconds=70.0), regime, rule_confidence="HIGH")
    assert early.details["raw_p"] < late.details["raw_p"]
