"""Main engine: discovery, intelligence, entry gating, execution, exits, settlement."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import signal
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from .analytics import build_validation_report, compute_performance, daily_performance, loss_breakdown
from .clock import Clock, RealClock
from .config import ConfigStore, RuntimeConfig, Settings, load_settings
from .db.engine import (
    backup_database,
    init_db,
    integrity_check,
    make_engine,
    make_session_factory,
    single_instance_lock,
)
from .db.models import PositionRow
from .db.repo import DuplicateOrderError, Repository
from .exchange.base import Balance, ExchangeAdapter, GeoResult
from .exchange.fees import FeeSchedule, taker_fee
from .exchange.sdk_adapter import PolymarketAdapter
from .execution.executor import ExecutionEngine
from .execution.feasibility import assess_exit_feasibility
from .execution.journal import OrderJournal
from .execution.paper import PaperBroker
from .exits import ExitAction, HybridExitManager
from .heartbeat import HeartbeatWorker, LeaseManager, validate_heartbeat_configuration
from .intelligence.ev import evaluate_edge
from .intelligence.features import FeatureSnapshot, compute_features
from .intelligence.filters import FilterEngine, FilterReport
from .intelligence.probability import ProbabilityEngine, ProbabilityEstimate
from .intelligence.regimes import RegimeDetector, RegimeState
from .intelligence.strategies import StrategyRegistry, StrategySignal
from .markets.lifecycle import MarketStateMachine
from .markets.rules import build_rule_spec
from .oracle.simulator import compare_to_actual, resolve_market
from .oracle.twap import PolyBoltTwapFeed, TwapObservation, TwapTracker
from .recovery import RecoveryManager, RecoveryResult
from .risk import ExposureLedger, RiskManager, RiskStatus
from .risk.capital import balance_capital_target
from .sizing import size_position
from .state import EngineStateStore
from .types import (
    FeatureMode,
    FeedHealth,
    MarketInfo,
    MarketState,
    Mode,
    OrderBookSnapshot,
    OrderStatus,
    Outcome,
    ReasonCode,
    Side,
    floor_to_step,
    utcnow,
)

log = logging.getLogger(__name__)

LOOP_SECONDS = 0.75
LIVE_TICK_SECONDS = 0.1  # display-only feed refresh between engine iterations
DISCOVERY_SECONDS = 15.0
GATE_REFRESH_SECONDS = 60.0
BOOK_RECORD_SECONDS = 2.0
DECISION_LOG_SECONDS = 2.0
HOLD_LOG_SECONDS = 10.0
PAPER_SETTLE_GRACE_SECONDS = 15.0
# A NO_TRADE that repeats the previous reason for the same market is a heartbeat, not news:
# it is logged at most this often. A different reason is still logged right away (see above).
DECISION_REPEAT_LOG_SECONDS = 60.0
BALANCE_REFRESH_SECONDS = 15.0
BACKUP_SECONDS = 6 * 3600
BACKUP_MAX_AGE_DAYS = 7


def _dec(value: Any) -> str | None:
    return None if value is None else format(Decimal(str(value)), "f")


def _outcome(label: str) -> Outcome:
    return Outcome.UP if str(label).upper() == "UP" else Outcome.DOWN


class BotEngine:
    def __init__(
        self,
        *,
        settings: Settings,
        repo: Repository,
        adapter: ExchangeAdapter,
        config_store: ConfigStore,
        clock: Clock,
        feeds: dict[str, Any],
        oracle: PolyBoltTwapFeed | None,
        recorder,
        paper: PaperBroker | None,
        state: EngineStateStore | None = None,
        db_engine=None,
        start_api: bool = True,
    ) -> None:
        self.settings = settings
        self.repo = repo
        self.adapter = adapter
        self.config_store = config_store
        self.clock = clock
        self.feeds = feeds
        self.oracle = oracle
        self.tracker = oracle.tracker if oracle else TwapTracker(60)
        self.recorder = recorder
        self.paper = paper
        self.state = state or EngineStateStore()
        self.db_engine = db_engine
        self.start_api = start_api

        self.ledger = ExposureLedger(repo)
        self.risk = RiskManager(repo=repo, clock=clock, config_provider=self._config, ledger=self.ledger)
        self.journal = OrderJournal(repo)
        self.execution = ExecutionEngine(
            repo=repo,
            journal=self.journal,
            adapter=adapter,
            paper=paper,
            clock=clock,
            mode_provider=lambda: self._effective_mode,
            taker_delay_ms=self._config().taker_delay_ms,
        )
        self.exits = HybridExitManager()
        self.regimes = RegimeDetector()
        self.strategies = StrategyRegistry()
        self.probability_engine = ProbabilityEngine(
            min_coverage_seconds=float(self._config().min_oracle_coverage_seconds),
            twap_window_seconds=60,
        )
        self.recovery = RecoveryManager(
            repo=repo,
            adapter=adapter,
            clock=clock,
            mode=Mode.PAPER,
            integrity_check=(lambda: integrity_check(db_engine)) if db_engine is not None else None,
        )
        self.lease = LeaseManager(repo, clock)

        self.mode: Mode = Mode.PAPER
        self._effective_mode: Mode = Mode.PAPER
        self._recovery_result: RecoveryResult | None = None
        self._machines: dict[str, MarketStateMachine] = {}
        self._infos: dict[str, MarketInfo] = {}
        self._books: dict[str, OrderBookSnapshot] = {}
        self._features: FeatureSnapshot | None = None
        self._probability: ProbabilityEstimate | None = None
        self._regime: RegimeState | None = None
        self._composite: Any = None
        self._balance: Decimal | None = None
        self._balance_at: datetime | None = None
        self._geo: GeoResult | None = None
        self._stop = asyncio.Event()
        self._running = False
        self._fatal = False
        self._last_discovery = 0.0
        self._last_balance = 0.0
        self._last_decision_log: dict[str, tuple[float, str]] = {}
        self._last_book_record: dict[str, float] = {}
        self._last_exit_attempt: dict[str, float] = {}
        self._last_simulation: dict[str, float] = {}
        self._last_resolution_try: dict[str, float] = {}
        self._simulations: dict[str, Any] = {}
        self._settlement_deferred: set[str] = set()
        self._last_hold_log: dict[str, float] = {}
        self._tick_cursor: dict[str, datetime] = {}
        self._twap_cursor: datetime | None = None
        self._feature_record_at = 0.0
        self._last_backup = 0.0
        self._last_analytics = 0.0
        self._gate_report = None
        self._gate_at: datetime | None = None
        self._gate_at_mono: float = 0.0
        self._started_at = self.clock.now()
        self._last_decision: dict[str, Any] | None = None
        self._http_task: asyncio.Task | None = None
        self._server = None

    # ------------------------------------------------------------------ config
    def _config(self) -> RuntimeConfig:
        return self.config_store.snapshot()

    def _active_market(self, now: datetime) -> MarketStateMachine | None:
        candidates = [m for m in self._machines.values() if m.start <= now < m.end]
        return max(candidates, key=lambda m: m.start) if candidates else None

    def _upcoming_market(self, now: datetime) -> MarketStateMachine | None:
        candidates = [m for m in self._machines.values() if m.start > now]
        return min(candidates, key=lambda m: m.start) if candidates else None

    def _market_books(self, market: MarketInfo) -> tuple[OrderBookSnapshot | None, OrderBookSnapshot | None]:
        return (
            self._books.get(market.asset_id(Outcome.UP)),
            self._books.get(market.asset_id(Outcome.DOWN)),
        )

    # -------------------------------------------------------------------- loop
    async def run(self) -> None:
        self._install_signals()
        self._running = True
        self.state.update(started_at=self._started_at.isoformat(), engine_healthy=False, detail="recovering")
        self.lease.renew(engine_pid=os.getpid(), healthy=False, detail="engine starting")
        try:
            self._refresh_mode()
            # A saved activation may resume LIVE after restart, but the gate must
            # still be re-evaluated. Recovery needs the real requested mode first.
            requested_live = (
                self._config().mode is Mode.LIVE
                and self.repo.active_live_activation() is not None
            )
            self.recovery.mode = Mode.LIVE if requested_live else Mode.PAPER
            self._recovery_result = await self.recovery.run()
            self.state.update(
                recovery={
                    "state": self._recovery_result.state.value,
                    "blocks": self._recovery_result.blocks,
                    "notes": self._recovery_result.notes,
                    "reconciled_orders": self._recovery_result.reconciled_orders,
                    "reconciled_positions": self._recovery_result.reconciled_positions,
                }
            )
            if self._config().mode is Mode.LIVE:
                try:
                    await self.refresh_gate()
                except Exception as exc:  # noqa: BLE001 - gate failure must lock LIVE
                    log.error("startup gate evaluation failed: %s", exc)
                    self._gate_report = None
                self._refresh_mode()
            self.recovery.mode = self._effective_mode
            await self._start_feed_tasks()
            if self.start_api:
                await self._start_api()
                self._ticker_task = asyncio.create_task(self._live_ticker())
            await self._maybe_backup(force=True)
            self.state.update(engine_healthy=True, detail="running")
            self.lease.renew(engine_pid=os.getpid(), healthy=True, detail="engine ready")

            while not self._stop.is_set():
                loop_started = self.clock.monotonic()
                try:
                    await self._iteration()
                    self.lease.renew(engine_pid=os.getpid(), healthy=True)
                except Exception as exc:  # noqa: BLE001 - a loop error must not kill silently
                    self._fatal = False
                    log.error("engine iteration failed: %s\n%s", exc, traceback.format_exc())
                    self.state.increment("errors")
                    self.state.update(detail=f"iteration error: {exc}")
                    self.lease.renew(engine_pid=os.getpid(), healthy=False, detail=str(exc)[:300])
                elapsed = self.clock.monotonic() - loop_started
                await self.clock.sleep(max(0.05, LOOP_SECONDS - elapsed))
        finally:
            await self._shutdown()

    async def _iteration(self) -> None:
        now = self.clock.now()
        self._refresh_mode()
        if self._config().mode is Mode.LIVE and (
            self._gate_report is None or self.clock.monotonic() - self._gate_at_mono > GATE_REFRESH_SECONDS
        ):
            try:
                await self.refresh_gate()
            except Exception as exc:  # noqa: BLE001 - a failed gate must not unlock LIVE
                log.error("gate refresh failed: %s", exc)
                self._gate_report = None
            self._refresh_mode()
        await self._maybe_discover(now)
        self._tick_lifecycle(now)
        await self._refresh_balance(now)
        market = self._active_market(now) or self._upcoming_market(now)
        if market is not None:
            await self._refresh_books(market.market, now)
        self._refresh_health(now)
        if market is not None:
            self._refresh_intel(market, now)
        await self._record_data(now)
        await self._manage_position(now)
        await self._evaluate_entry(now)
        await self._settle_windows(now)
        await self._maybe_backup()
        await self._maybe_analytics(now)
        await self._publish(now)

    # ------------------------------------------------------------------- setup
    async def _start_feed_tasks(self) -> None:
        for feed in self.feeds.values():
            await feed.start()
        if self.oracle is not None:
            await self.oracle.start()

    async def _shutdown(self) -> None:
        self._running = False
        self.lease.invalidate("engine shutting down", engine_pid=os.getpid())
        self.state.update(engine_healthy=False, detail="stopping")
        ticker = getattr(self, "_ticker_task", None)
        if ticker is not None:
            ticker.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await ticker
        if self._server is not None:
            self._server.should_exit = True
            await asyncio.sleep(0.2)
        for feed in self.feeds.values():
            with contextlib.suppress(Exception):
                await feed.stop()
        if self.oracle is not None:
            with contextlib.suppress(Exception):
                await self.oracle.stop()
        with contextlib.suppress(Exception):
            self.recorder.close()
        with contextlib.suppress(Exception):
            await self.adapter.close()

    def _install_signals(self) -> None:
        def _handler(signum, frame):  # type: ignore[no-untyped-def]
            log.warning("engine signal %s", signum)
            self._stop.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(ValueError, OSError):
                signal.signal(sig, _handler)

    async def _start_api(self) -> None:
        from .api.app import create_app

        app = create_app(engine=self, repo=self.repo, settings=self.settings, config_store=self.config_store)
        if self.settings.host not in {"127.0.0.1", "localhost", "::1"}:
            raise RuntimeError("dashboard must bind to loopback only")
        try:
            import uvicorn

            config = uvicorn.Config(
                app,
                host=self.settings.host,
                port=self.settings.port,
                log_level="warning",
                access_log=False,
            )
            self._server = uvicorn.Server(config)
            self._http_task = asyncio.create_task(self._server.serve(), name="dashboard")
        except Exception as exc:  # noqa: BLE001
            log.error("dashboard failed to start: %s", exc)
            self.state.update(detail=f"dashboard unavailable: {exc}")

    # -------------------------------------------------------------------- mode
    def _refresh_mode(self) -> None:
        cfg = self._config()
        self.mode = cfg.mode
        activation = self.repo.active_live_activation() is not None
        gate_ok = bool(self._gate_report and self._gate_report.passed)
        if self.mode is Mode.LIVE and activation and gate_ok:
            self._effective_mode = Mode.LIVE
        else:
            self._effective_mode = Mode.PAPER
        if self.mode is Mode.LIVE and (not activation or not gate_ok):
            self.state.update(detail="LIVE requested but locked; running PAPER only", live_locked=True)
        self.execution.mode_provider = lambda: self._effective_mode

    # --------------------------------------------------------------- discovery

    def _maybe_auto_approve_rule(self, fingerprint: str, cfg: Any) -> Any:
        """Approve a rule from env settings so approvals survive restarts on ephemeral disks."""
        pinned = fingerprint.lower() in self.settings.pinned_rule_fingerprints
        auto = self.settings.auto_approve_rules and cfg.mode.value != "LIVE"
        if not (pinned or auto):
            return None
        self.repo.approve_resolution_rule(fingerprint, "env-auto-approve")
        self.repo.audit(
            "RESOLUTION_RULE_APPROVED",
            actor="env-auto-approve",
            detail={"fingerprint": fingerprint, "via": "pinned" if pinned else "auto_paper"},
        )
        return self.repo.get_resolution_rule(fingerprint)
    async def _maybe_discover(self, now: datetime) -> None:
        mono = self.clock.monotonic()
        if mono - self._last_discovery < DISCOVERY_SECONDS and self._machines:
            return
        self._last_discovery = mono
        try:
            markets = await self.adapter.discover_markets(now)
        except Exception as exc:  # noqa: BLE001
            log.warning("market discovery failed: %s", exc)
            self.repo.add_protection_event("DISCOVERY_FAILURE", "WARN", error=str(exc)[:500])
            return
        cfg = self._config()
        for info in markets:
            errors = info.validate()
            if errors:
                log.warning("market %s rejected: %s", info.market_id, errors)
                continue
            self._infos[info.market_id] = info
            self.repo.upsert_market(info)
            self.repo.ensure_window(info.market_id, info.entry_deadline)
            raw_market = info.raw.get("market") if isinstance(info.raw, dict) else None
            if isinstance(raw_market, dict):
                spec = build_rule_spec(raw_market, info.event_start, info.version.value)
                self.repo.upsert_resolution_rule(spec.to_db())
                rule = self.repo.get_resolution_rule(spec.fingerprint)
                if rule is not None and not rule.approved:
                    rule = self._maybe_auto_approve_rule(spec.fingerprint, cfg) or rule
                if rule is not None and cfg.require_rules_approval and not rule.approved:
                    self.repo.set_state(
                        "pending_rule",
                        {"fingerprint": spec.fingerprint, "market_id": info.market_id, "methodology": spec.methodology.value},
                    )
            machine = self._machines.get(info.market_id)
            if machine is None:
                row = self.repo.get_window(info.market_id)
                try:
                    state = MarketState(row.state) if row is not None else MarketState.DISCOVERED
                except ValueError:
                    state = MarketState.DISCOVERED
                machine = MarketStateMachine(info, state)
                self._machines[info.market_id] = machine
            else:
                machine.market = info
        # Drop very old machines (>2h) to bound memory.
        cutoff = now - timedelta(hours=2)
        for market_id in [k for k, m in self._machines.items() if m.end < cutoff and m.state in {MarketState.SETTLED, MarketState.ABORTED}]:
            self._machines.pop(market_id, None)

    def _tick_lifecycle(self, now: datetime) -> None:
        for machine in list(self._machines.values()):
            transition = machine.tick(now)
            if transition is not None:
                self.repo.set_window_state(machine.market.market_id, transition.new.value)
                self.repo.add_protection_event(
                    "MARKET_STATE",
                    "INFO",
                    market_id=machine.market.market_id,
                    frm=transition.old.value,
                    to=transition.new.value,
                )

    # ------------------------------------------------------------------- books
    async def _refresh_books(self, market: MarketInfo, now: datetime) -> None:
        async def fetch(outcome: Outcome) -> None:
            try:
                asset_id = market.asset_id(outcome)
            except ValueError as exc:
                log.warning("asset id unavailable: %s", exc)
                return
            try:
                book = await self.adapter.get_order_book(asset_id)
                self._books[asset_id] = book
                mono = self.clock.monotonic()
                if mono - self._last_book_record.get(asset_id, 0.0) >= BOOK_RECORD_SECONDS:
                    self._last_book_record[asset_id] = mono
                    self.recorder.write(
                        "book_snapshots",
                        {
                            "ts": now.isoformat(),
                            "market_id": market.market_id,
                            "asset_id": asset_id,
                            "outcome": outcome.value,
                            "best_bid": _dec(book.best_bid),
                            "best_ask": _dec(book.best_ask),
                            "spread": _dec(book.spread),
                            "bid_depth": _dec(book.depth(Side.SELL)),
                            "ask_depth": _dec(book.depth(Side.BUY)),
                            "source": book.source,
                        },
                    )
            except Exception as exc:  # noqa: BLE001 - keep last known book; staleness gate will block
                log.warning("book fetch failed for %s: %s", outcome.value, exc)

        outcomes = [Outcome.UP]
        if len(self._infos[market.market_id].outcomes) > 1:
            outcomes.append(Outcome.DOWN)
        await asyncio.gather(*(fetch(o) for o in outcomes))

    def _sync_paper_capital(self) -> None:
        """Fund an unfunded paper broker once capital_usd is configured after startup.

        The broker is created once at boot with the capital known then. If capital was unset, its
        cash stayed 0 and every entry failed with INSUFFICIENT_BALANCE even after the operator set
        capital in the dashboard. Only a pristine broker (no cash, no fills, no positions) is funded,
        so existing paper results are never rewritten.
        """
        target = self._config().capital_usd
        if target is None or target <= 0:
            return
        if self.paper.cash <= 0 and not self.paper.positions and not self.paper.fills:
            self.paper.cash = target

    async def _refresh_balance(self, now: datetime) -> None:
        if self._effective_mode is Mode.PAPER:
            if self.paper is not None:
                self._sync_paper_capital()
            self._balance = self.paper.balance if self.paper is not None else None
            return
        mono = self.clock.monotonic()
        if mono - self._last_balance < BALANCE_REFRESH_SECONDS and self._balance is not None:
            return
        self._last_balance = mono
        try:
            balance = await self.adapter.get_balance()
            self._balance = balance.available
            self._balance_at = now
            self._sync_capital_to_balance(now)
        except Exception as exc:  # noqa: BLE001
            log.warning("balance refresh failed: %s", exc)
            self._balance = None

    def _sync_capital_to_balance(self, now: datetime) -> None:
        """LIVE: let capital_usd follow the real wallet balance (see balance_capital_target)."""
        cfg = self._config()
        if self._effective_mode is not Mode.LIVE or not cfg.capital_from_balance:
            return
        flat = self.repo.active_reserved_total() <= 0 and not self.ledger.has_active_position(Mode.LIVE.value)
        day = self.repo.get_risk_day(now.astimezone(timezone.utc).date())
        target = balance_capital_target(
            balance=self._balance, current_capital=cfg.capital_usd, flat=flat, day_trades=day.trades
        )
        if target is not None:
            self.config_store.set_capital(target)
            log.info("capital set to wallet balance: %s", target)

    # ------------------------------------------------------------------ health
    def _refresh_health(self, now: datetime) -> None:
        from .data.health import CompositeFeedHealth

        if self._effective_mode is Mode.LIVE and self._balance is None:
            self._balance = None
        feed_values = list(self.feeds.values())
        if len(feed_values) < 2:
            self._composite = None
            return
        oracle_status = (
            self.oracle.status(now) if self.oracle is not None else self._empty_oracle_status()
        )
        self._composite = CompositeFeedHealth().evaluate(
            feed_values[0].series, feed_values[1].series, oracle_status, now
        )

    def _feeds_payload(self, now: datetime, composite: Any) -> list[dict[str, Any]]:
        if composite is not None:
            return [
                {
                    "name": s.name,
                    "health": s.health.value,
                    "age_ms": s.age_ms,
                    "last_value": _dec(s.last_value),
                    "last_tick_at": s.last_tick_at.isoformat() if s.last_tick_at else None,
                    "detail": s.detail,
                }
                for s in (composite.primary, composite.secondary, composite.oracle)
            ]
        if self.oracle is not None:
            s = self.oracle.status(now)
            return [
                {
                    "name": s.name,
                    "health": s.health.value,
                    "age_ms": s.age_ms,
                    "last_value": _dec(s.last_value),
                    "last_tick_at": s.last_tick_at.isoformat() if s.last_tick_at else None,
                    "detail": s.detail,
                }
            ]
        return []

    async def _live_ticker(self) -> None:
        """Display only: push fresh feed prices/health to the dashboard ~10x/second.

        Trading logic is untouched: this builds its own local health evaluation and never
        assigns self._composite, so decisions still see exactly what the iteration computed.
        """
        from .data.health import CompositeFeedHealth

        while not self._stop.is_set():
            try:
                now = self.clock.now()
                feed_values = list(self.feeds.values())
                composite = None
                if len(feed_values) >= 2:
                    oracle_status = self.oracle.status(now) if self.oracle is not None else self._empty_oracle_status()
                    composite = CompositeFeedHealth().evaluate(feed_values[0].series, feed_values[1].series, oracle_status, now)
                feeds = self._feeds_payload(now, composite)
                if feeds:
                    self.state.update(
                        feeds=feeds,
                        oracle=next((f for f in feeds if f["name"] == "chainlink_twap"), {"health": "UNVERIFIED", "detail": "unavailable"}),
                    )
            except Exception as exc:  # noqa: BLE001 - display helper must never affect trading
                log.debug("live ticker skipped: %s", exc)
            await asyncio.sleep(LIVE_TICK_SECONDS)

    def _empty_oracle_status(self):
        from .types import FeedStatus

        return FeedStatus(name="chainlink_twap", health=FeedHealth.UNVERIFIED, detail="oracle feed not configured")

    # ------------------------------------------------------------------- intel
    def _refresh_intel(self, machine: MarketStateMachine, now: datetime) -> None:
        if self._composite is None or not self.feeds:
            return
        feed_values = list(self.feeds.values())
        primary = feed_values[0].series
        up_book, down_book = self._market_books(machine.market)
        cfg = self._config()
        opening = self.tracker.opening_reference(machine.start, tolerance_seconds=30.0)
        if opening is not None:
            window = self.repo.get_window(machine.market.market_id)
            if window is not None and window.opening_twap is None:
                self.repo.update_window(machine.market.market_id, opening_twap=opening)
        self._features = compute_features(
            primary,
            self.tracker,
            now=now,
            market_start=machine.start,
            market_end=machine.end,
            up_book=up_book,
            down_book=down_book,
        )
        self._regime = self.regimes.update(self._features, now)
        rule_confidence = str(machine.market.raw.get("rule_spec", {}).get("confidence", "LOW"))
        self._probability = self.probability_engine.estimate(
            self._features,
            self._regime,
            rule_confidence=rule_confidence,
            min_data_age_ms=cfg.max_data_age_ms,
        )

    # --------------------------------------------------------------- recording
    async def _record_data(self, now: datetime) -> None:
        for name, feed in self.feeds.items():
            cursor = self._tick_cursor.get(name)
            ticks = [t for t in feed.series.ticks if (cursor is None or t.ts > cursor) and not t.backfilled]
            if not ticks:
                continue
            self._tick_cursor[name] = ticks[-1].ts
            self.recorder.write_many(
                "btc_ticks",
                [
                    {
                        "ts": t.ts.isoformat(),
                        "recv_ts": (t.recv_ts or t.ts).isoformat(),
                        "source": t.source,
                        "price": _dec(t.price),
                        "qty": _dec(t.qty),
                        "sequence": t.sequence,
                    }
                    for t in ticks[-500:]
                ],
            )
        observations = [
            o for o in self.tracker.observations if self._twap_cursor is None or o.ts > self._twap_cursor
        ]
        if observations:
            self._twap_cursor = observations[-1].ts
            self.recorder.write_many(
                "oracle_twap",
                [
                    {
                        "ts": o.ts.isoformat(),
                        "value": _dec(o.value),
                        "source": o.source,
                        "window_seconds": o.window_seconds,
                        "symbol": o.symbol,
                    }
                    for o in observations[-500:]
                ],
            )
        if self._features is not None and now.timestamp() - self._feature_record_at >= 1.0:
            self._feature_record_at = now.timestamp()
            f = self._features
            self.recorder.write(
                "signal_features",
                {
                    "ts": now.isoformat(),
                    "btc_price": _dec(f.btc_price),
                    "return_5s": f.returns.get(5),
                    "return_15s": f.returns.get(15),
                    "return_60s": f.returns.get(60),
                    "vol_60s": f.realized_vol_60s,
                    "mtf_agreement": f.mtf_agreement,
                    "twap": _dec(f.twap_current),
                    "projected_twap": _dec(f.projected_final_twap),
                    "remaining_seconds": f.remaining_seconds,
                    "implied_up": f.implied_up,
                    "implied_down": f.implied_down,
                },
            )

    # ------------------------------------------------------------------ entry
    _UNRESOLVED_STATUSES = (
        OrderStatus.SUBMITTING.value,
        OrderStatus.ACKED.value,
        OrderStatus.PENDING.value,
        OrderStatus.CANCEL_PENDING.value,
        OrderStatus.UNKNOWN.value,
    )

    def _unresolved_orders(self, market_id: str) -> list:
        """Orders whose final state is unknown. Trading must stop until they are reconciled."""
        return self.repo.list_orders(
            market_id=market_id,
            statuses=self._UNRESOLVED_STATUSES,
            mode=self._effective_mode.value,
            limit=50,
        )

    async def _evaluate_entry(self, now: datetime) -> None:
        machine = self._active_market(now)
        if machine is None:
            return
        market = machine.market
        cfg = self._config()
        if not cfg.trading_enabled:
            self._note(now, "NO_TRADE", ReasonCode.TRADING_OFF, "trading switch is OFF; turn it ON in Settings to allow new entries")
            return
        if not machine.entry_allowed(now):
            if now < machine.start:
                self._note(now, "NO_TRADE", ReasonCode.MARKET_NOT_ACTIVE, f"market starts in {machine.seconds_to_start(now):.0f}s")
            elif now >= machine.end:
                self._note(now, "NO_TRADE", ReasonCode.ENTRY_WINDOW_CLOSED, "market ended")
            else:
                self._note(now, "NO_TRADE", ReasonCode.ENTRY_WINDOW_CLOSED, "120-second entry window closed")
            return
        if self.ledger.has_active_position(self._effective_mode.value):
            return
        if self.ledger.entries_used(market.market_id, self._effective_mode.value) >= cfg.max_entries_per_market:
            return
        if self._recovery_result is None or not self._recovery_result.ready:
            self._note(now, "NO_TRADE", ReasonCode.RECOVERY_NOT_READY, "recovery not READY")
            return
        unresolved = self._unresolved_orders(market.market_id)
        if unresolved:
            order = unresolved[0]
            self._note(
                now,
                "NO_TRADE",
                ReasonCode.RECOVERY_NOT_READY,
                f"order {order.client_order_id} state {order.status} is unresolved; reconciliation required",
            )
            return
        if self._composite is None or not self._composite.can_enter:
            reason = "; ".join(self._composite.reasons) if self._composite else "feeds unavailable"
            self._note(now, "NO_TRADE", ReasonCode.FEED_UNHEALTHY, reason)
            return
        rule = self.repo.get_resolution_rule(market.rules_fingerprint)
        rule_confidence = str(market.raw.get("rule_spec", {}).get("confidence", "LOW"))
        if rule is None or market.methodology.window_seconds == 0 and market.methodology.value != "SINGLE_PRICE_SNAPSHOT":
            self._note(now, "NO_TRADE", ReasonCode.RULES_UNVERIFIED, "resolution rules not registered")
            return
        if rule_confidence not in {"HIGH", "MEDIUM"}:
            self._note(now, "NO_TRADE", ReasonCode.RULES_UNVERIFIED, f"rule confidence {rule_confidence}")
            return
        if cfg.require_rules_approval and not rule.approved:
            self._note(now, "NO_TRADE", ReasonCode.RULES_UNAPPROVED, f"rule {rule.fingerprint[:12]} awaiting user approval")
            return
        if self.risk.status(self._effective_mode, balance=self._balance).allowed is False:
            status = self.risk.status(self._effective_mode, balance=self._balance)
            self._note(now, "NO_TRADE", status.reason, status.detail)
            return
        if self._features is None or self._probability is None or self._regime is None:
            self._note(now, "NO_TRADE", ReasonCode.ORACLE_DATA_INSUFFICIENT, "features/probability unavailable")
            return
        f = self._features
        if f.remaining_seconds <= 0 or f.entry_seconds_remaining <= 0:
            return
        if f.data_age_ms > cfg.max_data_age_ms:
            self._note(now, "NO_TRADE", ReasonCode.STALE_DATA, f"BTC data age {f.data_age_ms}ms")
            return
        if f.book_age_ms > cfg.max_book_age_ms:
            self._note(now, "NO_TRADE", ReasonCode.STALE_BOOK, f"book age {f.book_age_ms}ms")
            return
        p = self._probability
        if not p.tradable:
            self._note(now, "NO_TRADE", ReasonCode.LOW_PROBABILITY, f"probability confidence {p.confidence}", probability=p.p_up)
            return
        outcome = p.expected_outcome
        p_outcome = p.probability(outcome)
        if p_outcome < cfg.min_probability:
            self._note(now, "NO_TRADE", ReasonCode.LOW_PROBABILITY, f"P={p_outcome:.4f} < {cfg.min_probability}", probability=p.p_up)
            return
        if not self._regime.entries_allowed:
            self._note(now, "NO_TRADE", ReasonCode.REGIME_BLOCKED, f"regime {self._regime.regime.value}", probability=p.p_up)
            return
        if f.mtf_conflict:
            self._note(now, "NO_TRADE", ReasonCode.MTF_CONFLICT, "multi-timeframe conflict", probability=p.p_up)
            return
        signal = self.strategies.select(cfg.strategy, cfg.strategy_mode, f, self._regime)
        if cfg.strategy_mode is FeatureMode.ACTIVE and (signal.outcome is not outcome or signal.action != "BUY"):
            self._note(
                now,
                "NO_TRADE",
                ReasonCode.NO_SIGNAL,
                f"strategy {cfg.strategy} did not confirm {outcome.value} (action={signal.action})",
                probability=p.p_up,
                extra={"strategy": self._signal_dict(signal)},
            )
            return
        book = (f.up_book if outcome is Outcome.UP else f.down_book)
        if book is None or book.best_ask is None:
            self._note(now, "NO_TRADE", ReasonCode.INSUFFICIENT_DEPTH, "no ask liquidity", probability=p.p_up)
            return
        if book.spread is None or book.spread > cfg.max_spread:
            self._note(now, "NO_TRADE", ReasonCode.SPREAD_TOO_WIDE, f"spread {book.spread}", probability=p.p_up)
            return
        schedule = FeeSchedule.from_market(market)
        if not market.fee_verified:
            self._note(now, "NO_TRADE", ReasonCode.FEES_UNVERIFIED, "fee schedule missing from market metadata")
            return
        tick = market.tick_size
        raw_limit = min(Decimal(1) - tick, book.best_ask + cfg.max_slippage)
        limit_price = floor_to_step(raw_limit, tick)
        if limit_price < book.best_ask:
            self._note(now, "NO_TRADE", ReasonCode.SLIPPAGE_TOO_HIGH, "limit price below best ask")
            return
        sizing = size_position(
            capital=cfg.capital_usd,
            risk_pct=cfg.risk_per_trade_pct,
            balance=self._balance,
            reserved=self.repo.active_reserved_total(),
            book=book,
            min_order_size=market.min_order_size,
            schedule=schedule,
            fee_buffer=cfg.fee_buffer,
            max_price=limit_price,
            min_notional_usd=cfg.min_order_notional_usd,
            fixed_notional_usd=cfg.trade_size_usd,
        )
        if not sizing.allowed:
            self._note(now, "NO_TRADE", sizing.reason, sizing.detail, probability=p.p_up, extra={"sizing": self._sizing_dict(sizing)})
            return
        edge = evaluate_edge(
            probability=p_outcome,
            book=book,
            outcome=outcome.value,
            shares=sizing.shares,
            schedule=schedule,
            max_slippage=cfg.max_slippage,
            fee_buffer=cfg.fee_buffer,
        )
        if not edge.positive or edge.net_edge < cfg.min_net_edge:
            self._note(
                now,
                "NO_TRADE",
                ReasonCode.INSUFFICIENT_EDGE if edge.failure == "" else ReasonCode.NEGATIVE_EV,
                f"net edge {edge.net_edge} (min {cfg.min_net_edge}); {edge.failure}",
                probability=p.p_up,
                edge=edge.net_edge,
                price=edge.executable_price,
                extra={"edge": self._edge_dict(edge)},
            )
            return
        if cfg.min_order_notional_usd is not None and sizing.shares < market.min_order_size:
            # Dollar-sized entry below the SELL share minimum: the position can never be sold
            # early, so it is held to resolution (DUST_HOLD_TO_RESOLUTION). Loss stays bounded by
            # the order cost, which sizing already capped at the risk budget.
            feasibility = assess_exit_feasibility(book, sizing.shares, Decimal(0), require_full_exit=False)
            feasibility.allowed = True
            feasibility.reasons.append(
                f"{sizing.shares} shares below SELL minimum {market.min_order_size}: resolution-only exit"
            )
        else:
            feasibility = assess_exit_feasibility(book, sizing.shares, market.min_order_size)
        if not feasibility.allowed:
            self._note(
                now,
                "NO_TRADE",
                ReasonCode.SELL_NOT_FEASIBLE,
                "; ".join(feasibility.reasons),
                probability=p.p_up,
            )
            return
        filters = FilterEngine(max_spread=cfg.max_spread, min_depth_multiple=cfg.min_depth_multiple).evaluate(
            f, required_shares=sizing.shares
        )
        if not filters.all_pass:
            unavailable = bool(filters.unavailable)
            self._note(
                now,
                "NO_TRADE",
                ReasonCode.FILTER_UNAVAILABLE if unavailable else ReasonCode.FILTER_FAILED,
                filters.summary,
                probability=p.p_up,
                extra={"filters": self._filter_dict(filters)},
            )
            return
        for kind in ("signal_to_order", "order_to_result"):
            ok, stats = self.execution.latency.p95_ok(kind, cfg.max_latency_p95_ms)
            if not ok:
                self._note(now, "NO_TRADE", ReasonCode.LATENCY_BREACH, f"{kind} p95 {stats.get('p95')}ms", probability=p.p_up)
                return
        if f.entry_seconds_remaining < (cfg.max_ack_latency_ms / 1000.0 + 1.0):
            self._note(now, "NO_TRADE", ReasonCode.ACK_OUTSIDE_WINDOW, "too little time for a verifiable acknowledgement")
            return

        allowed, reason, detail = self.risk.pre_trade_check(
            market_id=market.market_id,
            mode=self._effective_mode,
            planned_loss=sizing.worst_case_loss,
            planned_cost=sizing.capital_required,
            balance=self._balance,
            recovery_ready=True,
            entries_used=self.ledger.entries_used(market.market_id, self._effective_mode.value),
            now=now,
        )
        if not allowed:
            self._note(now, "NO_TRADE", reason, detail, probability=p.p_up)
            return

        intent = self.journal.new_intent(
            market=market,
            outcome=outcome,
            side=Side.BUY,
            shares=sizing.shares,
            limit_price=limit_price,
            order_type="FOK",
            mode=self._effective_mode.value,
            reason=f"{cfg.strategy}|p={p_outcome:.4f}|edge={edge.net_edge}",
            now=now,
            usdc_amount=cfg.trade_size_usd if cfg.trade_size_usd is not None else sizing.notional,
        )
        outcome_result = await self.execution.execute_entry(intent, market, book, signal_ts=f.ts)
        result = outcome_result.result
        if result.filled_shares > 0:
            self.state.increment("entries")
            self._note(
                now,
                "TRADE",
                ReasonCode.OK,
                f"BUY {outcome.value} for {(result.filled_shares * result.avg_price):.2f} USDC = {result.filled_shares} shares @ {result.avg_price} ({result.status.value})",
                probability=p.p_up,
                edge=edge.net_edge,
                price=result.avg_price,
                extra={
                    "outcome": outcome.value,
                    "shares": str(result.filled_shares),
                    "price": str(result.avg_price),
                    "usdt": str((result.filled_shares * result.avg_price).quantize(Decimal("0.01"))),
                },
            )
            self.repo.add_protection_event(
                "ENTRY_FILLED",
                "INFO",
                market_id=market.market_id,
                outcome=outcome.value,
                shares=str(result.filled_shares),
                price=str(result.avg_price),
                mode=self._effective_mode.value,
            )
        else:
            self._note(
                now,
                "NO_TRADE",
                ReasonCode.NO_FILL,
                f"entry not filled: {result.status.value} {result.error_code or result.raw_status or ''}",
                probability=p.p_up,
                edge=edge.net_edge,
            )
        self._publish(now)

    # ------------------------------------------------------------------- exits
    def _log_hold(self, now: datetime, position, decision) -> None:
        """Record why an open position was NOT sold (rate limited) so the Activity log explains it."""
        mono = self.clock.monotonic()
        if mono - self._last_hold_log.get(position.market_id, -1e9) < HOLD_LOG_SECONDS:
            return
        self._last_hold_log[position.market_id] = mono

        def _plain(value):
            if isinstance(value, Decimal):
                return str(value)
            if isinstance(value, (int, float, bool, str)) or value is None:
                return value
            return str(value)

        meta = {k: _plain(v) for k, v in (decision.metadata or {}).items()}
        meta["exit_rule"] = decision.reason
        if decision.shadow_reason:
            meta["would_have_sold_for"] = decision.shadow_reason
        roi = f"{decision.roi:+.2%}" if decision.roi is not None else "n/a"
        target = meta.get("take_profit_target")
        target_txt = f"{Decimal(target):.2%}" if target not in (None, "") else "n/a"
        self._note(
            now,
            "HOLD",
            ReasonCode.OK,
            f"{decision.reason}: {decision.detail} | ROI {roi} vs take-profit target {target_txt}",
            price=decision.mark_price,
            extra=meta,
        )

    async def _manage_position(self, now: datetime) -> None:
        position = self.ledger.active_position(self._effective_mode.value)
        if position is None:
            return
        machine = self._machines.get(position.market_id)
        if machine is None:
            self._note(now, "NO_TRADE", ReasonCode.MARKET_NOT_VALIDATED, "position market metadata missing; cannot manage exit")
            return
        market = machine.market
        await self._refresh_books(market, now)
        up_book, down_book = self._market_books(market)
        status = self.risk.status(self._effective_mode, balance=self._balance, now=now)
        cfg = self._config()
        health = self._composite
        features = self._features if self._features is not None and self._features.ts == now else None
        decision = self.exits.evaluate(
            position=position,
            market=market,
            up_book=up_book,
            down_book=down_book,
            probability=self._probability,
            config=cfg,
            features=features,
            now=now,
            remaining_seconds=machine.seconds_remaining(now),
            risk_locked=status.kill_switch or status.locked or status.reason is ReasonCode.COOLDOWN,
            feed_healthy=bool(health and health.primary.health is FeedHealth.HEALTHY and health.secondary.health is FeedHealth.HEALTHY),
            oracle_healthy=bool(health and health.oracle.health is FeedHealth.HEALTHY),
            recovery_ready=bool(self._recovery_result and self._recovery_result.ready),
            book_max_age_ms=cfg.max_book_age_ms,
        )
        if decision.action is not ExitAction.SELL:
            self._log_hold(now, position, decision)
            return
        if self._unresolved_orders(market.market_id):
            # Never re-submit a SELL while a previous exit order's fate is unknown;
            # a duplicate exit could oversell the position.
            return
        mono = self.clock.monotonic()
        if mono - self._last_exit_attempt.get(position.market_id, 0.0) < 2.0:
            return
        self._last_exit_attempt[position.market_id] = mono
        if decision.limit_price is None:
            return
        intent = self.journal.new_intent(
            market=market,
            outcome=_outcome(position.outcome),
            side=Side.SELL,
            shares=decision.shares,
            limit_price=decision.limit_price,
            order_type="FOK",
            mode=self._effective_mode.value,
            reason=decision.reason,
            now=now,
        )
        try:
            outcome_result = await self.execution.execute_exit(intent, market, up_book if position.outcome.upper() == "UP" else down_book, position)
        except DuplicateOrderError:
            return
        if outcome_result.result.filled_shares > 0:
            self.state.increment("exits")
            self._last_analytics = float("-inf")
            update = self.risk.record_result(
                realized_pnl=outcome_result.realized_pnl,
                market_id=market.market_id,
                mode=self._effective_mode,
                now=now,
            )
            if update.locked:
                self._note(now, "EXIT", ReasonCode.PROFIT_LOCK if update.profit_locked else ReasonCode.DAILY_LOSS_LIMIT, "daily limit reached after exit")
            self._note(
                now,
                "EXIT",
                ReasonCode.OK,
                f"SELL {position.outcome} {outcome_result.result.filled_shares} shares @ {outcome_result.result.avg_price} = {(outcome_result.result.filled_shares * outcome_result.result.avg_price):.2f} USDC reason={decision.reason} pnl={outcome_result.realized_pnl} USDC",
                probability=self._probability.p_up if self._probability else None,
                price=outcome_result.result.avg_price,
                extra={
                    "outcome": position.outcome,
                    "shares": str(outcome_result.result.filled_shares),
                    "price": str(outcome_result.result.avg_price),
                    "usdt": str((outcome_result.result.filled_shares * outcome_result.result.avg_price).quantize(Decimal("0.01"))),
                    "pnl": str(outcome_result.realized_pnl),
                    "rule": decision.reason,
                },
            )
        else:
            self.repo.add_protection_event(
                "EXIT_NOT_FILLED",
                "WARN",
                market_id=market.market_id,
                reason=decision.reason,
                status=outcome_result.result.status.value,
                error=outcome_result.result.error_code,
            )
        if self.repo.get_position(position.market_id, self._effective_mode.value).status == "CLOSED":
            self.exits.forget(position.id)

    # -------------------------------------------------------------- settlement
    async def _settle_windows(self, now: datetime) -> None:
        for machine in list(self._machines.values()):
            if machine.state in {MarketState.SETTLED, MarketState.ABORTED}:
                continue
            if now < machine.end:
                continue
            market = machine.market
            mono = self.clock.monotonic()
            if market.market_id not in self._simulations and mono - self._last_simulation.get(market.market_id, 0.0) > 5:
                self._last_simulation[market.market_id] = mono
                self._attempt_simulation(machine, now)
            actual = None
            if mono - self._last_resolution_try.get(market.market_id, 0.0) > 10:
                self._last_resolution_try[market.market_id] = mono
                try:
                    actual = await self.adapter.get_resolution(market.condition_id, market.outcomes)
                except Exception as exc:  # noqa: BLE001
                    log.debug("resolution fetch failed: %s", exc)
                if actual:
                    self._record_actual(market.market_id, actual)
            provisional = False
            if actual is None and self._effective_mode is Mode.PAPER:
                # On-chain payouts can lag by minutes. PAPER has no funds to redeem, so settle from
                # the oracle simulation after a short grace period instead of leaving the position
                # open (it blocks new entries and keeps win/loss out of Analytics). LIVE never guesses.
                sim = self._simulations.get(market.market_id)
                if (
                    sim is not None
                    and sim[1].valid
                    and sim[1].outcome is not None
                    and (now - machine.end).total_seconds() >= PAPER_SETTLE_GRACE_SECONDS
                ):
                    actual = sim[1].outcome.value
                    provisional = True
            if actual is None:
                continue
            await self._finalize_window(machine, actual, now, provisional=provisional)

    def _attempt_simulation(self, machine: MarketStateMachine, now: datetime) -> None:
        market = machine.market
        raw_market = market.raw.get("market") if isinstance(market.raw, dict) else None
        if not isinstance(raw_market, dict):
            return
        spec = build_rule_spec(raw_market, market.event_start, market.version.value)
        result = resolve_market(
            spec,
            self.tracker,
            machine.start,
            machine.end,
            min_coverage_seconds=self._config().min_oracle_coverage_seconds,
        )
        self._simulations[market.market_id] = (spec, result)
        if result.valid:
            self.repo.update_window(
                market.market_id,
                opening_twap=result.opening_reference,
                closing_twap=result.closing_reference,
                simulated_outcome=result.outcome.value if result.outcome else None,
                oracle_coverage_seconds=int(result.coverage_pct),
            )
            self.repo.save_resolution(
                market_id=market.market_id,
                simulated_outcome=result.outcome.value if result.outcome else None,
                actual_outcome=None,
                opening_ref=result.opening_reference,
                closing_ref=result.closing_reference,
                confidence=result.confidence,
                methodology=result.notional_methodology,
                observations={"used": len(result.observations_used), "coverage_pct": result.coverage_pct},
                validated=False,
            )
            log.info("resolution simulation %s -> %s (coverage %.0f%%)", market.market_id, result.outcome, result.coverage_pct)
        else:
            log.info("resolution simulation %s unavailable: %s", market.market_id, result.reason)

    def _record_actual(self, market_id: str, actual: str) -> None:
        sim = self._simulations.get(market_id)
        if sim is None:
            return
        spec, result = sim
        match, _ = compare_to_actual(result, actual)
        self.repo.save_resolution(
            market_id=market_id,
            simulated_outcome=result.outcome.value if result.outcome else None,
            actual_outcome=actual,
            opening_ref=result.opening_reference,
            closing_ref=result.closing_reference,
            confidence=result.confidence,
            methodology=result.notional_methodology,
            observations={"used": len(result.observations_used), "coverage_pct": result.coverage_pct},
            match=match,
            validated=match is True,
        )
        if match is False:
            self.repo.add_protection_event(
                "RESOLUTION_MISMATCH",
                "CRITICAL",
                market_id=market_id,
                simulated=result.outcome.value if result.outcome else None,
                actual=actual,
            )

    async def _finalize_window(
        self, machine: MarketStateMachine, actual: str, now: datetime, *, provisional: bool = False
    ) -> None:
        market = machine.market
        sim = self._simulations.get(market.market_id)
        if sim is not None and not provisional:
            _, result = sim
            match, _ = compare_to_actual(result, actual)
            self.repo.save_resolution(
                market_id=market.market_id,
                simulated_outcome=result.outcome.value if result.outcome else None,
                actual_outcome=actual,
                opening_ref=result.opening_reference,
                closing_ref=result.closing_reference,
                confidence=result.confidence,
                methodology=result.notional_methodology,
                observations={"used": len(result.observations_used), "coverage_pct": result.coverage_pct},
                match=match,
                validated=match is True,
            )
        position = self.repo.get_position(market.market_id, self._effective_mode.value)
        if position is not None and position.status in {"OPEN", "DUST"}:
            if self._unresolved_orders(market.market_id):
                # The true share count is unknown; booking settlement now could
                # report PnL for shares that were actually sold. Keep the window
                # open and require reconciliation.
                if market.market_id not in self._settlement_deferred:
                    self._settlement_deferred.add(market.market_id)
                    self.repo.add_protection_event(
                        "SETTLEMENT_DEFERRED",
                        "CRITICAL",
                        market_id=market.market_id,
                        note="position not settled: an order outcome is unresolved; reconcile before booking PnL",
                    )
                return
            realized = self.execution.settle_position(position, actual)
            if self.paper is not None and self._effective_mode is Mode.PAPER:
                for settlement in self.paper.settle(market, actual):
                    if settlement.market_id == market.market_id:
                        realized = settlement.realized_pnl
            self.risk.record_result(realized_pnl=realized, market_id=market.market_id, mode=self._effective_mode, now=now)
            self.state.increment("settlements")
            self._last_analytics = float("-inf")  # show the new win/loss right away
            self._note(
                now,
                "SETTLE",
                ReasonCode.OK,
                f"settled {position.outcome} shares={position.shares} outcome={actual} pnl={realized}"
                + (" (PAPER, oracle-simulated outcome; on-chain resolution pending)" if provisional else ""),
                price=Decimal(1) if position.outcome.upper() == actual.upper() else Decimal(0),
                extra={
                    "outcome": position.outcome,
                    "result": actual.upper(),
                    "won": position.outcome.upper() == actual.upper(),
                    "shares": str(position.shares),
                    "pnl": str(realized),
                },
            )
            self.exits.forget(position.id)
        machine.settle(now)
        self.repo.set_window_state(
            market.market_id,
            MarketState.SETTLED.value,
            resolution_outcome=actual.upper(),
            resolution_confirmed=not provisional,
            settlement_ts=now,
        )

    # -------------------------------------------------------------------- risk
    async def _maybe_backup(self, *, force: bool = False) -> None:
        if self.db_engine is None:
            return
        mono = self.clock.monotonic()
        if not force and mono - self._last_backup < BACKUP_SECONDS:
            return
        self._last_backup = mono
        try:
            dest = self.settings.data_path / "backups" / f"polybot-{utcnow().strftime('%Y%m%d-%H%M%S')}.db"
            path = await asyncio.to_thread(backup_database, self.db_engine, dest)
            self.repo.set_state("last_backup", {"path": str(path), "at": utcnow().isoformat()})
            self.repo.add_protection_event("BACKUP_CREATED", "INFO", path=str(path))
        except FileExistsError:
            pass
        except Exception as exc:  # noqa: BLE001
            log.error("backup failed: %s", exc)
            self.repo.add_protection_event("BACKUP_FAILED", "WARN", error=str(exc)[:500])

    async def _maybe_analytics(self, now: datetime) -> None:
        mono = self.clock.monotonic()
        if mono - self._last_analytics < 30.0:
            return
        self._last_analytics = mono
        try:
            trades = self.repo.list_trades(limit=5000)
            paper = [t for t in trades if t.mode == "PAPER"]
            live = [t for t in trades if t.mode == "LIVE"]
            payload = {
                "paper": compute_performance(paper).as_dict(),
                "live": compute_performance(live).as_dict(),
                "daily": daily_performance(paper),
                "loss_breakdown": loss_breakdown(paper),
            }
            self.state.update(analytics=payload)
        except Exception as exc:  # noqa: BLE001
            log.warning("analytics refresh failed: %s", exc)

    def _note(
        self,
        now: datetime,
        decision: str,
        reason: ReasonCode,
        detail: str,
        *,
        probability: float | None = None,
        edge: Decimal | None = None,
        price: Decimal | None = None,
        extra: dict | None = None,
    ) -> None:
        market = self._active_market(now) or self._upcoming_market(now)
        market_id = market.market.market_id if market else "none"
        self._last_decision = {
            "ts": now.isoformat(),
            "decision": decision,
            "reason": reason.value,
            "detail": detail,
            **({"extra": extra} if extra else {}),
        }
        mono = self.clock.monotonic()
        previous = self._last_decision_log.get(market_id)
        if decision == "NO_TRADE" and previous is not None:
            same_reason = previous[1] == reason.value
            min_gap = DECISION_REPEAT_LOG_SECONDS if same_reason else DECISION_LOG_SECONDS
            if mono - previous[0] < min_gap:
                return
        self._last_decision_log[market_id] = (mono, reason.value)
        try:
            self.repo.log_decision(
                market_id=market_id,
                decision=decision,
                reason_code=reason.value,
                reason_detail=detail[:2000],
                probability=Decimal(str(probability)) if probability is not None else None,
                edge=edge,
                price=price,
                regime=self._regime.regime.value if self._regime else None,
                features=extra or {},
                mode=self._effective_mode.value,
                ts=now,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("decision log failed: %s", exc)
        self.state.increment("decisions")

    # ------------------------------------------------------------------ publish
    async def _publish(self, now: datetime) -> None:
        cfg = self._config()
        mode_value = self._effective_mode.value
        risk_status = self.risk.status(self._effective_mode, balance=self._balance, now=now)
        machine = self._active_market(now) or self._upcoming_market(now)
        market_state = None
        market_payload = None
        if machine is not None:
            market = machine.market
            market_state = machine.state.value
            rule = self.repo.get_resolution_rule(market.rules_fingerprint)
            # Display only (dashboard "Price To Beat"): the Chainlink reference at the window start.
            price_to_beat = (
                self.tracker.opening_reference(machine.start, tolerance_seconds=30.0)
                if now >= machine.start
                else None
            )
            market_payload = {
                "market_id": market.market_id,
                "slug": market.slug,
                "question": market.question,
                "version": market.version.value,
                "neg_risk": market.neg_risk,
                "state": machine.state.value,
                "event_start": machine.start.isoformat(),
                "end_date": machine.end.isoformat(),
                "entry_deadline": machine.entry_deadline.isoformat(),
                "price_to_beat": _dec(price_to_beat),
                "seconds_remaining": round(machine.seconds_remaining(now), 1),
                "entry_seconds_remaining": round(machine.entry_seconds_remaining(now), 1),
                "tick_size": _dec(market.tick_size),
                "min_order_size": _dec(market.min_order_size),
                "fees_enabled": market.fees_enabled,
                "fee_rate": _dec(market.fee_rate),
                "methodology": market.methodology.value,
                "rules_fingerprint": market.rules_fingerprint,
                "rules_approved": bool(rule and rule.approved),
                "accepting_orders": market.accepting_orders,
            }
        position = self.ledger.active_position(mode_value)
        position_payload = None
        if position is not None:
            book = self._books.get(position.asset_id)
            mark = book.best_bid if book else None
            position_payload = {
                "market_id": position.market_id,
                "outcome": position.outcome,
                "shares": _dec(position.shares),
                "avg_price": _dec(position.avg_price),
                "cost_basis": _dec(position.cost_basis),
                "status": position.status,
                "mark": _dec(mark),
                "roi": _dec((mark * position.shares - position.cost_basis) / position.cost_basis) if mark and position.cost_basis else None,
                "unrealized_pnl": _dec(mark * position.shares - position.cost_basis) if mark is not None else None,
                "value_now": _dec(mark * position.shares) if mark is not None else None,
            }
        feeds = self._feeds_payload(now, self._composite)
        books = {}
        for outcome in (Outcome.UP, Outcome.DOWN):
            if machine is None:
                break
            try:
                asset_id = machine.market.asset_id(outcome)
            except ValueError:
                continue
            book = self._books.get(asset_id)
            if book is None:
                continue
            books[outcome.value] = {
                "best_bid": _dec(book.best_bid),
                "best_ask": _dec(book.best_ask),
                "spread": _dec(book.spread),
                "bid_depth": _dec(book.depth(Side.SELL)),
                "ask_depth": _dec(book.depth(Side.BUY)),
                "age_ms": int((now - book.timestamp).total_seconds() * 1000),
            }
        heartbeat = self.repo.get_heartbeat_state()
        activation = self.repo.active_live_activation()
        latency = {
            "signal_to_order": self.repo.latency_stats("signal_to_order"),
            "order_to_result": self.repo.latency_stats("order_to_result"),
            "exit_order_to_result": self.repo.latency_stats("exit_order_to_result"),
        }
        self.state.update(
            mode=cfg.mode.value,
            effective_mode=mode_value,
            live_locked=self._effective_mode is not Mode.LIVE,
            engine_healthy=True,
            uptime_seconds=round((now - self._started_at).total_seconds(), 1),
            market=market_payload,
            market_state=market_state,
            books=books,
            feeds=feeds,
            oracle=next((f for f in feeds if f["name"] == "chainlink_twap"), {"health": "UNVERIFIED", "detail": "unavailable"}),
            features=self._features_dict(),
            probability=self._probability_dict(),
            regime={"regime": self._regime.regime.value, "confidence": self._regime.confidence, "reasons": self._regime.reasons} if self._regime else None,
            position=position_payload,
            risk=self._risk_dict(risk_status),
            last_decision=self._last_decision,
            latency=latency,
            live={
                "activation_active": activation is not None,
                "heartbeat": {
                    "armed": heartbeat.armed,
                    "heartbeat_id_present": bool(heartbeat.heartbeat_id),
                    "last_success_at": heartbeat.last_success_at.isoformat() if heartbeat.last_success_at else None,
                    "consecutive_failures": heartbeat.consecutive_failures,
                    "last_error": heartbeat.last_error,
                },
                "gate": self._gate_report.as_dict() if self._gate_report else None,
            },
            config={
                "capital_usd": _dec(cfg.capital_usd),
                "risk_per_trade_pct": _dec(cfg.risk_per_trade_pct),
                "trade_size_usd": _dec(cfg.trade_size_usd),
                "daily_loss_limit_pct": _dec(cfg.daily_loss_limit_pct),
                "max_consecutive_losses": cfg.max_consecutive_losses,
                "cooldown_minutes": cfg.cooldown_minutes,
                "max_active_positions": cfg.max_active_positions,
                "strategy": cfg.strategy,
                "strategy_mode": cfg.strategy_mode.value,
                "require_rules_approval": cfg.require_rules_approval,
                "feature_modes": {k: v.value for k, v in cfg.feature_modes.items()},
            },
        )

    # -------------------------------------------------------------- gate/LIVE
    async def refresh_gate(self) -> Any:
        ctx = await self._gate_context()
        from .gate import LiveGate

        self._gate_report = LiveGate(self.repo).evaluate(ctx)
        self._gate_at = self.clock.now()
        self._gate_at_mono = self.clock.monotonic()
        await self._publish(self._gate_at)
        return self._gate_report

    async def _gate_context(self, *, heartbeat_tested_override: bool | None = None):
        from .gate import GateContext

        cfg = self._config()
        balance: Balance | None = None
        wallet_ok = False
        try:
            balance = await self.adapter.get_balance()
            wallet_ok = True
        except Exception:
            wallet_ok = False
        geo = None
        try:
            geo = await self.adapter.geoblock()
        except Exception:
            geo = None
        activation = self.repo.active_live_activation()
        machine = self._active_market(self.clock.now()) or self._upcoming_market(self.clock.now())
        market = machine.market if machine else None
        rule = self.repo.get_resolution_rule(market.rules_fingerprint) if market else None
        rule_spec = market.raw.get("rule_spec", {}) if market else {}
        recovery_row = self.repo.get_recovery_state()
        risk_status = self.risk.status(self._effective_mode, balance=self._balance)
        heartbeat_state = self.repo.get_heartbeat_state()
        test_state = self.repo.get_state("heartbeat_test", {})
        hb_check = validate_heartbeat_configuration(
            self.settings,
            live_mode=cfg.mode is Mode.LIVE,
            activation_active=activation is not None,
            dedicated_key_confirmed=self.settings.heartbeat_dedicated_key_confirmed,
            test_recorded=bool(test_state.get("ok")),
        )
        last_backup = self.repo.get_state("last_backup", {})
        backup_at = None
        if last_backup.get("at"):
            with contextlib.suppress(ValueError):
                backup_at = datetime.fromisoformat(last_backup["at"])
        backup_ok = bool(backup_at and (utcnow() - backup_at).days < BACKUP_MAX_AGE_DAYS)
        validation = build_validation_report(self.repo, cfg)
        open_orders = self.repo.open_orders(mode=self._effective_mode.value)
        min_buy = False
        min_sell = False
        protocol_ok = False
        rules_verified = False
        rules_approved = False
        if market is not None:
            up_book, down_book = self._market_books(market)
            schedule = FeeSchedule.from_market(market)
            if up_book and up_book.best_ask:
                unit = up_book.best_ask + taker_fee(Decimal(1), up_book.best_ask, schedule) + cfg.fee_buffer
                cost = market.min_order_size * unit
                if cfg.trade_size_usd is not None:
                    # fixed dollar size: no capital minimum, only the exchange's dollar minimum
                    affordable = cfg.trade_size_usd >= (cfg.min_order_notional_usd or Decimal(0))
                else:
                    affordable = cfg.capital_usd is not None and cost <= cfg.capital_usd * cfg.risk_per_trade_pct
                min_buy = (
                    cfg.capital_usd is not None
                    and affordable
                    and up_book.depth(Side.BUY) >= market.min_order_size
                )
            min_sell = bool(
                up_book
                and down_book
                and up_book.depth(Side.SELL) >= market.min_order_size
                and down_book.depth(Side.SELL) >= market.min_order_size
            )
            protocol_ok = not market.validate() and market.version.value in {"v1", "v2"}
            rules_verified = str(rule_spec.get("confidence", "LOW")) in {"HIGH", "MEDIUM"} and bool(rule)
            rules_approved = bool(rule and rule.approved)
        fused = self._composite
        feeds_healthy = bool(fused and fused.can_enter)
        return GateContext(
            activation_requested=cfg.mode is Mode.LIVE,
            geo=geo,
            wallet_auth_verified=wallet_ok,
            signature_type=self.settings.signature_type,
            signature_type_verified=self.settings.signature_type in {0, 1, 2, 3} and wallet_ok,
            funder_verified=bool(
                self.settings.funder_address and re.fullmatch(r"0x[0-9a-fA-F]{40}", self.settings.funder_address)
            ),
            balance=balance,
            capital=cfg.capital_usd,
            allowance_required=cfg.capital_usd,
            allowance_ok=bool(
                balance is not None
                and balance.allowance is not None
                and cfg.capital_usd is not None
                and balance.allowance >= cfg.capital_usd
            ),
            reconciliation_clean=recovery_row.state == "READY" and not open_orders,
            recovery_ready=recovery_row.state == "READY",
            feeds_healthy=feeds_healthy,
            rules_verified=rules_verified,
            rules_approved=rules_approved,
            resolution_simulator_active=self.oracle is not None,
            exposure_monitor_active=True,
            disaster_recovery_active=backup_ok,
            risk_manager_active=True,
            protective_shutdown_resolved=bool(
                not risk_status.kill_switch
                and not risk_status.locked
                and risk_status.cooldown_until is None
                and heartbeat_state.consecutive_failures < 3
            ),
            protocol_version_ok=protocol_ok,
            min_buy_feasible=min_buy,
            min_sell_feasible=min_sell,
            heartbeat_configured=hb_check.ok,
            heartbeat_tested=bool(test_state.get("ok")) if heartbeat_tested_override is None else heartbeat_tested_override,
            statistical_validation_passed=validation.passed,
            final_confirmation=False,
            details={
                "balance": f"available={balance.available if balance else 'unknown'} required={cfg.capital_usd}",
                "allowance": f"allowance={balance.allowance if balance else 'unknown'} required={cfg.capital_usd}",
                "recovery": recovery_row.detail,
                "feeds": "; ".join(fused.reasons) if fused else "feeds unavailable",
                "rules": f"confidence={rule_spec.get('confidence', 'LOW')}",
                "rule_approval": rule.fingerprint if rule and rule.approved else "not approved",
                "heartbeat": "; ".join(hb_check.failures) if hb_check.failures else "configured",
                "validation": "; ".join(validation.failures) if validation.failures else "passed",
                "protective": risk_status.detail,
                "disaster_recovery": f"last backup {last_backup.get('at', 'never')}",
                "protocol": market.version.value if market else "no market",
                "min_buy": f"min cost within risk budget: {min_buy}",
                "min_sell": f"bid depth supports minimum SELL: {min_sell}",
                "wallet_auth": "balance fetch succeeded" if wallet_ok else "balance fetch failed",
                "funder": self.settings.funder_address or "not configured",
            },
        )

    async def activate_live(self, *, typed_phrase: str, actor: str) -> Any:
        from .gate import LiveGate

        ctx = await self._gate_context(heartbeat_tested_override=None)
        report = LiveGate(self.repo).activate(ctx, typed_phrase=typed_phrase, actor=actor, config={"capital": _dec(self._config().capital_usd)})
        if report.passed:
            self.config_store.update({"mode": "LIVE"})
            self._gate_report = report
            self._refresh_mode()
        return report

    async def revoke_live(self, *, actor: str, reason: str) -> None:
        from .gate import LiveGate

        LiveGate(self.repo).revoke(actor=actor, reason=reason)
        self.config_store.update({"mode": "PAPER"})
        self._gate_report = None
        self._refresh_mode()
        with contextlib.suppress(Exception):
            cancelled = await self.adapter.cancel_all()
            self.repo.add_protection_event("LIVE_REVOKED_CANCEL_ALL", "WARN", cancelled=cancelled, actor=actor)

    async def arm_heartbeat_test(self, *, typed_phrase: str, actor: str) -> dict:
        """Explicitly perform the one-way heartbeat arm test.

        Sends exactly one heartbeat with no open orders. This arms the platform's
        key-wide timeout-cancel-all permanently; only the user may trigger it.
        """
        from .gate import LiveGate

        if typed_phrase.strip() != "ARM HEARTBEAT":
            return {"ok": False, "error": "typed confirmation does not match 'ARM HEARTBEAT'"}
        open_orders = await self.adapter.get_open_orders()
        if open_orders:
            return {"ok": False, "error": f"{len(open_orders)} open orders exist; refusing to arm heartbeat"}
        ctx = await self._gate_context(heartbeat_tested_override=True)
        report = LiveGate(self.repo).evaluate(ctx)
        failing = [c for c in report.failed if c.key not in {"final_confirmation", "heartbeat"}]
        if failing:
            return {"ok": False, "error": "gate not ready except heartbeat", "failed": [c.key for c in failing]}
        try:
            result = await self.adapter.send_heartbeat("")
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}
        now = self.clock.now()
        self.repo.update_heartbeat_state(
            heartbeat_id=result.heartbeat_id,
            armed=True,
            last_success_at=now,
            last_attempt_at=now,
            consecutive_failures=0,
            last_error=None,
        )
        self.repo.set_state(
            "heartbeat_test",
            {"ok": True, "at": now.isoformat(), "actor": actor, "note": "one-way heartbeat arm test"},
        )
        self.repo.audit("HEARTBEAT_ARM_TEST", actor=actor, detail={"heartbeat_id_present": True})
        return {"ok": True, "heartbeat_id_present": True, "armed": True}

    # ------------------------------------------------------------- serializers
    def _features_dict(self) -> dict | None:
        f = self._features
        if f is None:
            return None
        return {
            "ts": f.ts.isoformat(),
            "btc_price": _dec(f.btc_price),
            "returns": {str(k): v for k, v in f.returns.items()},
            "momentum": f.momentum,
            "acceleration": f.acceleration,
            "realized_vol_60s": f.realized_vol_60s,
            "mtf_agreement": f.mtf_agreement,
            "mtf_conflict": f.mtf_conflict,
            "weighted_direction": f.weighted_direction,
            "twap_current": _dec(f.twap_current),
            "opening_reference": _dec(f.opening_reference),
            "twap_distance_bps": f.twap_distance_bps,
            "projected_final_twap": _dec(f.projected_final_twap),
            "remaining_seconds": f.remaining_seconds,
            "entry_seconds_remaining": f.entry_seconds_remaining,
            "implied_up": f.implied_up,
            "implied_down": f.implied_down,
            "data_age_ms": f.data_age_ms,
            "book_age_ms": f.book_age_ms,
            "oracle_age_ms": f.oracle_age_ms,
            "oracle_coverage_seconds": f.oracle_coverage_seconds,
        }

    def _probability_dict(self) -> dict | None:
        p = self._probability
        if p is None:
            return None
        return {
            "p_up": p.p_up,
            "p_down": p.p_down,
            "confidence": p.confidence,
            "uncertainty": [p.uncertainty_low, p.uncertainty_high],
            "expected_outcome": p.expected_outcome.value,
            "expected_final_twap": _dec(p.expected_final_twap),
            "opening_reference": _dec(p.opening_reference),
            "current_twap": _dec(p.current_twap),
            "sigma": p.sigma,
            "z": p.z,
            "signal_quality": p.signal_quality,
            "coverage_seconds": p.coverage_seconds,
            "method": p.method,
        }

    @staticmethod
    def _risk_dict(status: RiskStatus) -> dict:
        return {
            "allowed": status.allowed,
            "reason": status.reason.value,
            "detail": status.detail,
            "capital": _dec(status.capital),
            "daily_pnl": _dec(status.daily_pnl),
            "daily_loss_limit": _dec(status.daily_loss_limit),
            "daily_profit_target": _dec(status.daily_profit_target),
            "locked": status.locked,
            "profit_locked": status.profit_locked,
            "kill_switch": status.kill_switch,
            "kill_reason": status.kill_reason,
            "consecutive_losses": status.consecutive_losses,
            "cooldown_until": status.cooldown_until.isoformat() if status.cooldown_until else None,
            "reserved": _dec(status.reserved),
            "open_positions": status.open_positions,
        }

    @staticmethod
    def _sizing_dict(sizing) -> dict:
        return {
            "shares": _dec(sizing.shares),
            "avg_price": _dec(sizing.avg_price),
            "worst_case_loss": _dec(sizing.worst_case_loss),
            "risk_budget": _dec(sizing.risk_budget),
            "max_by_risk": _dec(sizing.max_by_risk),
            "max_by_balance": _dec(sizing.max_by_balance),
            "max_by_depth": _dec(sizing.max_by_depth),
        }

    @staticmethod
    def _edge_dict(edge) -> dict:
        return {
            "net_edge": _dec(edge.net_edge),
            "ev_total": _dec(edge.ev_total),
            "executable_price": _dec(edge.executable_price),
            "slippage": _dec(edge.slippage),
            "fee_per_share": _dec(edge.fee_per_share),
            "sufficient_depth": edge.sufficient_depth,
            "failure": edge.failure,
        }

    @staticmethod
    def _filter_dict(report: FilterReport) -> dict:
        return {r.name: {"verdict": r.verdict.value, "detail": r.detail} for r in report.results}

    @staticmethod
    def _signal_dict(signal: StrategySignal) -> dict:
        return {
            "strategy": signal.strategy,
            "outcome": signal.outcome.value if signal.outcome else None,
            "action": signal.action,
            "confidence": signal.confidence,
            "score": signal.score,
            "reasons": list(signal.reasons),
        }


def build_default_engine(settings: Settings, *, start_api: bool = True) -> tuple[BotEngine, Any]:
    settings.ensure_dirs()
    db_engine = make_engine(settings.db_path)
    init_db(db_engine)
    repo = Repository(make_session_factory(db_engine))
    config_store = ConfigStore(settings.runtime_config_file)
    if settings.default_capital_usd is not None and config_store.snapshot().capital_usd is None:
        config_store.set_capital(settings.default_capital_usd)
        repo.audit(
            "CAPITAL_DEFAULT_APPLIED",
            actor="env",
            detail={"capital_usd": str(settings.default_capital_usd)},
        )
    clock = RealClock()
    adapter = PolymarketAdapter(settings)
    from .data.btc_feeds import feed_from_name

    feeds = {
        "binance": feed_from_name("binance", settings.binance_ws),
        "coinbase": feed_from_name("coinbase", settings.coinbase_ws),
    }
    oracle = PolyBoltTwapFeed(settings)
    from .data.recorder import ParquetRecorder

    recorder = ParquetRecorder(settings.parquet_path)
    cfg = config_store.snapshot()
    paper = PaperBroker(cfg.capital_usd or Decimal(0), clock, taker_delay_ms=cfg.taker_delay_ms)
    engine = BotEngine(
        settings=settings,
        repo=repo,
        adapter=adapter,
        config_store=config_store,
        clock=clock,
        feeds=feeds,
        oracle=oracle,
        recorder=recorder,
        paper=paper,
        db_engine=db_engine,
        start_api=start_api,
    )
    return engine, db_engine


async def run_engine(settings: Settings, *, start_api: bool = True) -> None:
    engine, db_engine = build_default_engine(settings, start_api=start_api)
    try:
        await engine.run()
    finally:
        db_engine.dispose()


def engine_process_main(settings_dict: dict, *, start_api: bool = True) -> None:
    from .logging_setup import setup_logging

    settings = Settings(**settings_dict)
    setup_logging(settings.logs_path, component="engine")
    try:
        asyncio.run(run_engine(settings, start_api=start_api))
    except KeyboardInterrupt:
        pass
    except Exception:
        log.error("engine failed\n%s", traceback.format_exc())
        raise


def main() -> int:
    settings = load_settings()
    from .logging_setup import setup_logging

    setup_logging(settings.logs_path, component="engine")
    lock_path = settings.data_path / settings.single_instance_lock
    with single_instance_lock(lock_path):
        asyncio.run(run_engine(settings))
    return 0


if __name__ == "__main__":
    sys.exit(main())
