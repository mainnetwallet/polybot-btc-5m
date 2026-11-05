from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from btc5m_bot.data.btc_feeds import parse_binance, parse_coinbase
from btc5m_bot.data.health import CompositeFeedHealth, FeedHealthMonitor, HealthThresholds
from btc5m_bot.data.recorder import ParquetRecorder
from btc5m_bot.data.series import BtcSeries, BtcTick
from btc5m_bot.exchange.fees import FeeSchedule
from btc5m_bot.intelligence.ev import evaluate_edge
from btc5m_bot.types import FeedHealth, FeedStatus

from conftest import START, make_book

SCHEDULE = FeeSchedule(rate=Decimal("0.07"), exponent=1, enabled=True)


def series(prices, *, source="binance", start=START, step=1.0):
    s = BtcSeries(source)
    for index, price in enumerate(prices):
        s.add(BtcTick(ts=start + timedelta(seconds=index * step), price=Decimal(price),
                      qty=Decimal("0.1"), source=source, recv_ts=start + timedelta(seconds=index * step)))
    return s


def test_feed_parsers_reject_foreign_and_malformed_messages():
    epoch_ms = int(START.timestamp() * 1000)
    binance = parse_binance({"e": "trade", "p": "100.5", "q": "0.2", "T": epoch_ms}, START)
    assert binance is not None
    assert binance.price == Decimal("100.5")
    assert binance.source == "binance"
    assert parse_binance({"e": "depthUpdate", "p": "1"}, START) is None
    assert parse_binance({"e": "trade", "p": "not-a-number", "T": epoch_ms}, START) is None

    coinbase = parse_coinbase(
        {"type": "match", "price": "99.5", "size": "1", "time": START.isoformat(), "sequence": 7}, START
    )
    assert coinbase is not None
    assert coinbase.price == Decimal("99.5")
    assert coinbase.sequence == 7
    assert parse_coinbase({"type": "subscriptions"}, START) is None
    assert parse_coinbase({"type": "match", "price": None}, START) is None


def test_series_rejects_cross_provider_ticks_and_lookahead():
    s = series(["100"])
    with pytest.raises(ValueError):
        s.add(BtcTick(ts=START + timedelta(seconds=1), price=Decimal("101"), source="coinbase"))
    assert s.add(BtcTick(ts=START - timedelta(seconds=1), price=Decimal("99"), source="binance")) is False
    assert len(s) == 1
    assert s.last_at_or_before(START - timedelta(seconds=10)) is None
    assert s.price_at(START + timedelta(seconds=30)) == Decimal("100")


def test_health_is_fresh_stale_then_down():
    s = series(["100"])
    monitor = FeedHealthMonitor()
    fresh = monitor.evaluate("primary", s, START + timedelta(seconds=1))
    assert fresh.health is FeedHealth.HEALTHY
    stale = monitor.evaluate("primary", s, START + timedelta(seconds=3))
    assert stale.health is FeedHealth.STALE
    down = monitor.evaluate("primary", s, START + timedelta(seconds=10))
    assert down.health is FeedHealth.DOWN


def test_health_flags_sequence_gaps_jumps_and_skew():
    gap = BtcSeries("binance")
    gap.add(BtcTick(ts=START, price=Decimal("100"), source="binance", sequence=1, recv_ts=START))
    gap.add(BtcTick(ts=START + timedelta(seconds=1), price=Decimal("100"), source="binance",
                    sequence=3, recv_ts=START + timedelta(seconds=1)))
    status = FeedHealthMonitor().evaluate("primary", gap, START + timedelta(seconds=1))
    assert status.health is FeedHealth.DEGRADED
    assert "sequence gaps" in status.detail

    jump = series(["100", "110"], step=5.0, start=START - timedelta(seconds=5))
    status = FeedHealthMonitor().evaluate("primary", jump, START)
    assert status.health is FeedHealth.DEGRADED

    skewed = BtcSeries("binance")
    skewed.add(BtcTick(ts=START, price=Decimal("100"), source="binance", recv_ts=START + timedelta(seconds=4)))
    status = FeedHealthMonitor().evaluate("primary", skewed, START + timedelta(seconds=4))
    assert status.health is FeedHealth.DEGRADED
    assert "skew" in status.detail


def test_health_flags_frozen_price():
    s = BtcSeries("binance")
    for index in range(12):
        s.add(BtcTick(ts=START + timedelta(seconds=index), price=Decimal("100"), source="binance",
                      recv_ts=START + timedelta(seconds=index)))
    status = FeedHealthMonitor(HealthThresholds(max_frozen_seconds=5)).evaluate(
        "primary", s, START + timedelta(seconds=11)
    )
    assert status.health is FeedHealth.DEGRADED
    assert "frozen" in status.detail


def test_composite_blocks_on_one_stale_leg_or_divergence():
    now = START + timedelta(seconds=1)
    primary = series(["100000"])
    secondary = series(["100100"], source="coinbase")
    oracle = FeedStatus(name="oracle", health=FeedHealth.HEALTHY, last_value=Decimal("100000"))
    ok = CompositeFeedHealth().evaluate(primary, secondary, oracle, now)
    assert ok.can_enter is True
    assert ok.overall is FeedHealth.HEALTHY

    divergent = series(["101000"], source="coinbase")
    bad = CompositeFeedHealth().evaluate(primary, divergent, oracle, now)
    assert bad.can_enter is False
    assert any("diverge" in r for r in bad.reasons)

    stale_oracle = FeedStatus(name="oracle", health=FeedHealth.STALE, detail="age 6000 ms")
    blocked = CompositeFeedHealth().evaluate(primary, secondary, stale_oracle, now)
    assert blocked.can_enter is False
    assert any("oracle" in r for r in blocked.reasons)


def test_edge_model_accounts_for_fees_and_depth():
    book = make_book("up", bid="0.49", ask="0.50", ask_size="1000")
    edge = evaluate_edge(
        probability=0.62, book=book, outcome="UP", shares=Decimal("10"), schedule=SCHEDULE
    )
    assert edge.positive
    assert edge.sufficient_depth
    assert edge.cost_per_share > Decimal("0.50")
    assert float(edge.net_edge) == pytest.approx(0.62 - float(edge.cost_per_share), rel=1e-9)

    thin = evaluate_edge(
        probability=0.62,
        book=make_book("up", bid="0.49", ask="0.50", ask_size="2"),
        outcome="UP", shares=Decimal("10"), schedule=SCHEDULE,
    )
    assert not thin.positive
    assert "insufficient depth" in thin.failure

    no_book = evaluate_edge(probability=0.62, book=None, outcome="UP", shares=Decimal("10"), schedule=SCHEDULE)
    assert not no_book.positive
    assert no_book.failure == "no order book"


def test_edge_rejects_excess_slippage():
    book = make_book("up", asks=[("0.50", "5"), ("0.60", "100")], bid="0.49")
    edge = evaluate_edge(
        probability=0.80, book=book, outcome="UP", shares=Decimal("10"),
        schedule=SCHEDULE, max_slippage=Decimal("0.01"),
    )
    assert not edge.positive
    assert "slippage" in edge.failure


def test_parquet_recorder_writes_and_queries(tmp_path):
    recorder = ParquetRecorder(tmp_path / "parquet")
    recorder.write("btc_ticks", {"ts": START, "price": "100.5", "source": "binance"})
    recorder.write("btc_ticks", {"ts": START + timedelta(seconds=1), "price": "101.0", "source": "binance"})
    with pytest.raises(ValueError):
        recorder.write("private_keys", {"secret": "nope"})
    files = recorder.flush()
    assert files and files[0].suffix == ".parquet"
    rows = recorder.query("btc_ticks", "SELECT count(*), max(price) FROM btc_ticks")
    assert rows[0][0] == 2
    assert float(rows[0][1]) == pytest.approx(101.0)
