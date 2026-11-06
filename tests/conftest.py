"""Shared deterministic fixtures. No network, no real funds, no wall-clock dependencies."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from btc5m_bot.clock import FixedClock
from btc5m_bot.config import ConfigStore, Settings
from btc5m_bot.db.engine import init_db, make_engine, make_session_factory
from btc5m_bot.db.models import Base
from btc5m_bot.db.repo import Repository
from btc5m_bot.exchange.fake_adapter import FakeExchangeAdapter
from btc5m_bot.security import hash_password
from btc5m_bot.types import (
    BookLevel,
    MarketInfo,
    MarketVersion,
    Methodology,
    OrderBookSnapshot,
)

START = datetime(2026, 11, 5, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(START)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    s = Settings(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
        config_dir=tmp_path / "config",
        dashboard_password_hash=hash_password("correct-horse-battery"),
        session_secret="test-session-secret-" + "x" * 24,
        clob_api_key="test-key",
        clob_api_secret="test-secret",
        clob_api_passphrase="test-pass",
        default_capital_usd=None,
        database_url=os.environ.get("POLYBOT_TEST_DATABASE_URL") or None,
    )
    s.ensure_dirs()
    return s


@pytest.fixture
def db(settings: Settings):
    # Set POLYBOT_TEST_DATABASE_URL to a throwaway Postgres to run the repository tests against it.
    url = os.environ.get("POLYBOT_TEST_DATABASE_URL")
    engine = make_engine(settings.database_target)
    if url:
        Base.metadata.drop_all(engine)
    init_db(engine)
    try:
        yield engine
    finally:
        if url:
            Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.fixture
def repo(db) -> Repository:
    return Repository(make_session_factory(db))


@pytest.fixture
def config_store(settings: Settings) -> ConfigStore:
    store = ConfigStore(settings.runtime_config_file)
    store.update({"auto_approve_rules": False})
    return store


@pytest.fixture
def adapter() -> FakeExchangeAdapter:
    return FakeExchangeAdapter()


def make_market(
    *,
    start: datetime = START,
    version: MarketVersion = MarketVersion.V1,
    market_id: str = "mkt-1",
    min_order_size: str = "5",
    tick_size: str = "0.01",
    fees_enabled: bool = True,
    fee_rate: str = "0.07",
    methodology: Methodology = Methodology.TWAP_60S,
    outcomes: tuple[str, ...] = ("Up", "Down"),
) -> MarketInfo:
    clob = ("up-token", "down-token")
    positions = ("up-position", "down-position")
    description = (
        "This market resolves UP if the 60-second Chainlink BTC/USD TWAP at the end "
        "is greater than or equal to the opening TWAP, otherwise DOWN. "
        "Prices are rounded to 2 decimal places."
    )
    raw_market = {
        "id": market_id,
        "conditionId": "cond-" + market_id,
        "resolutionSource": "https://data.chain.link/streams/btc-usd",
        "description": description,
        "cryptoMarketConfig": {"twapEnabled": True, "twapLookbackSeconds": 60},
        "outcomes": list(outcomes),
    }
    return MarketInfo(
        market_id=market_id,
        condition_id="cond-" + market_id,
        slug=f"btc-updown-5m-{int(start.timestamp())}",
        question="BTC Up or Down 5m",
        version=version,
        neg_risk=False,
        event_start=start,
        end_date=start + timedelta(seconds=300),
        clob_token_ids=clob,
        position_ids=positions,
        outcomes=outcomes,
        tick_size=Decimal(tick_size),
        min_order_size=Decimal(min_order_size),
        accepting_orders=True,
        fees_enabled=fees_enabled,
        fee_rate=Decimal(fee_rate),
        fee_exponent=1,
        taker_only=True,
        rebate_rate=Decimal("0"),
        resolution_source="https://data.chain.link/streams/btc-usd",
        description=description,
        methodology=methodology,
        rules_fingerprint="fp-" + market_id,
        liquidity=Decimal("100000"),
        fee_verified=True,
        raw={"market": raw_market, "rule_spec": {"confidence": "HIGH"}},
    )


@pytest.fixture
def make_market_info():
    return make_market


def make_book(
    asset_id: str,
    *,
    bid: str = "0.48",
    ask: str = "0.50",
    bid_size: str = "1000",
    ask_size: str = "1000",
    ts: datetime = START,
    **kwargs,
) -> OrderBookSnapshot:
    bids = kwargs.pop("bids", [(bid, bid_size)])
    asks = kwargs.pop("asks", [(ask, ask_size)])
    return OrderBookSnapshot(
        asset_id=asset_id,
        timestamp=ts,
        bids=tuple(BookLevel(Decimal(p), Decimal(s)) for p, s in bids),
        asks=tuple(BookLevel(Decimal(p), Decimal(s)) for p, s in asks),
        source="fake",
        **kwargs,
    )


@pytest.fixture
def market_factory():
    return make_market


@pytest.fixture
def book_factory():
    return make_book
