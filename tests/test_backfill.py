from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx

from btc5m_bot.data import btc_feeds
from btc5m_bot.data.btc_feeds import BinanceFeed, parse_binance_klines

NOW = datetime(2026, 11, 6, 6, 0, 0, tzinfo=timezone.utc)


def _rows(seconds: int = 300, *, open_tail: bool = False) -> list[list]:
    rows = []
    for k in range(seconds):
        close_ts = NOW - timedelta(seconds=seconds - k - 1)  # last row closes exactly at NOW
        close_ms = int(close_ts.timestamp() * 1000)
        price = 100000 + (k % 7) * 3  # small wiggle so volatility is non-zero
        rows.append([close_ms - 999, "0", "0", "0", str(price), "0.5", close_ms, "0", 1, "0", "0", "0"])
    if open_tail:
        future_ms = int((NOW + timedelta(seconds=1)).timestamp() * 1000)
        rows.append([future_ms - 999, "0", "0", "0", "100500", "0.1", future_ms, "0", 1, "0", "0", "0"])
    return rows


def test_parse_klines_skips_open_and_malformed_rows():
    rows = _rows(10, open_tail=True) + [["bad"], None, [0, 0, 0, 0, "x", 0, 1]]
    ticks = parse_binance_klines(rows, NOW)
    assert len(ticks) == 10
    assert all(t.backfilled and t.source == "binance" and t.sequence is None for t in ticks)
    assert [t.ts for t in ticks] == sorted(t.ts for t in ticks)
    assert ticks[-1].ts == NOW


def test_backfill_makes_history_available_immediately(monkeypatch):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=_rows()))
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_client(transport=transport, **kw))

    class _Clock:
        def now(self):
            return NOW

    feed = BinanceFeed(clock=_Clock())
    assert feed.series.return_over(180, NOW) is None  # the cold-start state that gave regime UNKNOWN

    added = asyncio.run(feed.backfill())

    assert added == 300
    assert feed.series.return_over(180, NOW) is not None
    assert feed.series.return_over(60, NOW) is not None
    assert feed.series.realized_vol(60, NOW) is not None
    assert btc_feeds.BACKFILL_SECONDS == 300


def test_backfill_failure_does_not_stop_the_feed(monkeypatch):
    transport = httpx.MockTransport(lambda request: httpx.Response(451))
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_client(transport=transport, **kw))
    feed = BinanceFeed()

    async def scenario():
        # run() swallows backfill errors, then tries the websocket; stop it right away.
        feed._stop.set()
        await feed.run()

    asyncio.run(scenario())
    assert len(feed.series) == 0
