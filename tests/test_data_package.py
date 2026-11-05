"""Regression tests for the btc5m_bot.data package (series, feeds, health, recorder)."""
from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from decimal import Decimal

import pytest
import websockets

from btc5m_bot.data.btc_feeds import (
    BinanceFeed,
    CoinbaseFeed,
    feed_from_name,
    parse_binance,
    parse_coinbase,
)
from btc5m_bot.data.health import CompositeFeedHealth, FeedHealthMonitor
from btc5m_bot.data.recorder import ParquetRecorder
from btc5m_bot.data.series import BtcSeries, BtcTick
from btc5m_bot.types import FeedHealth, FeedStatus

from conftest import START


def _series(prices, *, source="binance", step=1.0, qty="0.5"):
    s = BtcSeries(source)
    for i, price in enumerate(prices):
        ts = START + timedelta(seconds=i * step)
        s.add(BtcTick(ts=ts, price=Decimal(str(price)), qty=Decimal(qty), source=source, recv_ts=ts))
    return s


def test_series_return_volume_and_counts_are_causal():
    s = _series([100 + i for i in range(61)])  # 100 .. 160 over 60 s
    now = START + timedelta(seconds=60)
    assert s.return_over(60, now) == pytest.approx(0.6)
    assert s.return_over(30, now) == pytest.approx((160 - 130) / 130)
    assert s.tick_count(10, now) == 10          # half-open window (now-10, now]
    assert s.volume(10, now) == Decimal("5.0")
    # Queries at an earlier time ignore later ticks.
    assert s.return_over(10, START + timedelta(seconds=30)) == pytest.approx((130 - 120) / 120)
    assert s.return_over(60, START + timedelta(seconds=30)) is None  # no tick 60 s before


def test_realized_vol_needs_coverage_and_scales_with_noise():
    now = START + timedelta(seconds=120)
    flat = _series([100.0] * 121)
    assert flat.realized_vol(60, now) == 0.0
    choppy = _series([100.0 if i % 2 else 100.1 for i in range(121)])
    assert choppy.realized_vol(60, now) > 0
    sparse = _series([100.0] * 5)
    assert sparse.realized_vol(60, START + timedelta(seconds=4)) is None


def test_series_prunes_old_ticks_and_rejects_bad_prices():
    s = BtcSeries("binance", retention_seconds=120)
    for i in range(0, 600):
        ts = START + timedelta(seconds=i)
        s.add(BtcTick(ts=ts, price=Decimal("100"), source="binance", recv_ts=ts))
    assert len(s) < 600
    assert s.first().ts >= START + timedelta(seconds=600 - 1 - 120 - 61)
    assert s.add(BtcTick(ts=START + timedelta(seconds=601), price=Decimal("0"), source="binance")) is False
    assert s.add(BtcTick(ts=START + timedelta(seconds=602), price=Decimal("NaN"), source="binance")) is False


def test_parsers_handle_wrapped_and_hostile_messages():
    wrapped = {"stream": "btcusdt@trade", "data": {"e": "trade", "p": "100", "q": "1", "T": 1, "t": 5}}
    tick = parse_binance(wrapped, START)
    assert tick is not None and tick.sequence == 5
    for bad in (None, [], "x", {"e": "trade", "p": "NaN"}, {"e": "trade", "p": "-1"},
                {"e": "trade", "p": "1", "q": "-3"}, {"e": "trade", "p": "Infinity"}):
        assert parse_binance(bad, START) is None
    match = {"type": "match", "price": "100", "size": "0.1", "time": "2026-11-05T12:00:00.123456Z",
             "trade_id": 42, "sequence": 9000, "product_id": "BTC-USD"}
    parsed = parse_coinbase(match, START)
    assert parsed is not None and parsed.sequence == 42  # trade_id, not the non-contiguous sequence
    assert parse_coinbase({**match, "product_id": "ETH-USD"}, START) is None
    assert parse_coinbase({**match, "price": "oops"}, START) is None


def test_feed_from_name_and_url_override():
    assert isinstance(feed_from_name("binance"), BinanceFeed)
    assert isinstance(feed_from_name("Coinbase"), CoinbaseFeed)
    assert feed_from_name("binance", "ws://localhost:1/x").url == "ws://localhost:1/x"
    with pytest.raises(ValueError):
        feed_from_name("kraken")


def test_feed_ingests_from_websocket_and_reconnects():
    async def scenario() -> None:
        connections = 0

        async def handler(ws) -> None:  # noqa: ANN001
            nonlocal connections
            connections += 1
            for i in range(3):
                await ws.send(json.dumps({"e": "trade", "p": f"{100 + i}", "q": "0.1",
                                          "T": 1_700_000_000_000 + i * 100, "t": 10 + i + connections * 100}))
            await ws.send("not json")
            await ws.send(json.dumps({"e": "depthUpdate"}))
            if connections == 1:
                await ws.close()           # force a reconnect
            else:
                await asyncio.sleep(5)

        async with websockets.serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            feed = BinanceFeed(f"ws://127.0.0.1:{port}")
            await feed.start()
            for _ in range(100):
                if connections >= 2 and len(feed.series) >= 6 - 3:
                    break
                await asyncio.sleep(0.1)
            await feed.stop()
        assert connections >= 2
        assert feed.accepted >= 3
        assert feed.rejected >= 1          # the non-JSON frame
        assert feed.connected is False

    asyncio.run(scenario())


def test_health_requires_observation_time_before_flagging_frozen_price():
    short = _series([100.0] * 8)  # flat but only observed for 7 s: not enough to call it frozen
    assert FeedHealthMonitor().evaluate("p", short, START + timedelta(seconds=7)).health is FeedHealth.HEALTHY
    long_flat = _series([100.0] * 40)
    status = FeedHealthMonitor().evaluate("p", long_flat, START + timedelta(seconds=39))
    assert status.health is FeedHealth.DEGRADED and "frozen" in status.detail


def test_health_empty_series_is_down_and_composite_blocks():
    empty = BtcSeries("binance")
    assert FeedHealthMonitor().evaluate("p", empty, START).health is FeedHealth.DOWN
    ok_oracle = FeedStatus(name="chainlink_twap", health=FeedHealth.HEALTHY, last_value=Decimal("100"))
    result = CompositeFeedHealth().evaluate(empty, _series([100], source="coinbase"), ok_oracle, START)
    assert result.can_enter is False and result.overall is FeedHealth.DOWN


def test_recorder_batches_partitions_by_day_and_enforces_retention(tmp_path):
    rec = ParquetRecorder(tmp_path / "pq", retention_days=30)
    rec.write_many("btc_ticks", [
        {"ts": START.isoformat(), "price": "100.5", "qty": "0.1", "sequence": 1, "source": "binance"},
        {"ts": (START + timedelta(days=1)).isoformat(), "price": "99.5", "source": "binance"},
    ])
    rec.write("book_snapshots", {"ts": START, "asset_id": "7" * 70, "best_bid": "0.4"})
    files = rec.flush()
    assert {f.parent.name for f in files} >= {f"date={START.date()}", f"date={(START + timedelta(days=1)).date()}"}
    assert rec.query("btc_ticks", "SELECT min(price) FROM btc_ticks")[0][0] == pytest.approx(99.5)
    assert rec.query("book_snapshots", "SELECT asset_id FROM book_snapshots")[0][0] == "7" * 70  # no float mangling
    assert rec.query("paper_fills", "SELECT count(*) FROM paper_fills") == [(0,)]
    # 31 days later the cutoff is START+1 day: the two START-day partitions expire, the next day's survives.
    removed = rec.enforce_retention(today=START.date() + timedelta(days=31))
    assert removed == 2
    survivors = sorted(p.name for p in (tmp_path / "pq").glob("*/date=*"))
    assert survivors == [f"date={(START + timedelta(days=1)).date()}"]
    assert rec.query("btc_ticks", "SELECT count(*) FROM btc_ticks") == [(1,)]


def test_recorder_query_is_read_only_and_rejects_unknown_datasets(tmp_path):
    rec = ParquetRecorder(tmp_path / "pq")
    rec.write("btc_ticks", {"ts": START, "price": "1", "source": "binance"})
    for sql in ("DROP VIEW btc_ticks", "SELECT 1; SELECT 2", "COPY btc_ticks TO 'x.csv'",
                "SELECT * FROM read_csv('/etc/passwd')"):
        with pytest.raises(Exception):
            rec.query("btc_ticks", sql)
    with pytest.raises(ValueError):
        rec.query("secrets", "SELECT 1")
    with pytest.raises(ValueError):
        rec.write("secrets", {"x": 1})
