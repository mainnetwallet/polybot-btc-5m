from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from btc5m_bot.config import RuntimeConfig
from btc5m_bot.exits import ExitAction, HybridExitManager
from btc5m_bot.intelligence.probability import ProbabilityEstimate
from btc5m_bot.risk.exposure import ExposureLedger
from btc5m_bot.risk.manager import RiskManager
from btc5m_bot.types import Mode, OrderStatus, Outcome, ReasonCode, Regime, Side

from conftest import START, make_book, make_market

CAPITAL = Decimal("1000")


def config(**overrides) -> RuntimeConfig:
    overrides.setdefault("daily_loss_limit_usd", Decimal("40"))
    return RuntimeConfig(capital_usd=CAPITAL, **overrides)


def manager(clock, repo, cfg: RuntimeConfig | None = None) -> RiskManager:
    cfg = cfg or config()
    return RiskManager(repo=repo, clock=clock, config_provider=lambda: cfg)


def intent_for(repo, market, *, shares="10", side=Side.BUY, status=OrderStatus.SUBMITTING):
    from btc5m_bot.execution.journal import OrderJournal

    journal = OrderJournal(repo)
    intent = journal.new_intent(
        market=market,
        outcome=Outcome.UP,
        side=side,
        shares=Decimal(shares),
        limit_price=Decimal("0.51"),
        mode="PAPER",
    )
    journal.persist_intent(intent, reserve_amount=Decimal(0))
    repo.update_order(intent.client_order_id, status=status.value)
    return intent


def test_planned_loss_is_capped_at_two_percent(clock, repo):
    # the 2% cap applies to risk-% sizing (trade_size_usd = null)
    risk = manager(clock, repo, config(trade_size_usd=None))
    ok = risk.pre_trade_check(
        market_id="mkt-1", mode=Mode.PAPER, planned_loss=Decimal("20"),
        planned_cost=Decimal("20"), balance=CAPITAL, recovery_ready=True,
    )
    assert ok[0] is True
    too_big = risk.pre_trade_check(
        market_id="mkt-1", mode=Mode.PAPER, planned_loss=Decimal("20.01"),
        planned_cost=Decimal("20"), balance=CAPITAL, recovery_ready=True,
    )
    assert too_big[0] is False and too_big[1] is ReasonCode.RISK_LIMIT
    assert "exceeds" in too_big[2]


def test_planned_cost_cannot_exceed_risk_budget(clock, repo):
    risk = manager(clock, repo, config(trade_size_usd=None))
    allowed, reason, _ = risk.pre_trade_check(
        market_id="mkt-1", mode=Mode.PAPER, planned_loss=Decimal("5"),
        planned_cost=Decimal("25"), balance=CAPITAL, recovery_ready=True,
    )
    assert not allowed and reason is ReasonCode.RISK_LIMIT


def test_no_capital_no_trade(clock, repo):
    risk = RiskManager(
        repo=repo, clock=clock,
        config_provider=lambda: RuntimeConfig(capital_usd=None),
    )
    allowed, reason, _ = risk.pre_trade_check(
        market_id="mkt-1", mode=Mode.PAPER, planned_loss=Decimal("1"),
        planned_cost=Decimal("1"), balance=CAPITAL, recovery_ready=True,
    )
    assert not allowed and reason is ReasonCode.CAPITAL_UNSET
    assert risk.status(Mode.PAPER).allowed is False


def test_recovery_not_ready_blocks_entries(clock, repo):
    risk = manager(clock, repo)
    allowed, reason, _ = risk.pre_trade_check(
        market_id="mkt-1", mode=Mode.PAPER, planned_loss=Decimal("5"),
        planned_cost=Decimal("5"), balance=CAPITAL, recovery_ready=False,
    )
    assert not allowed and reason is ReasonCode.RECOVERY_NOT_READY


def test_balance_must_cover_cost_after_reservations(clock, repo):
    risk = manager(clock, repo)
    repo.reserve("other", "cid-1", Decimal("990"))
    allowed, reason, detail = risk.pre_trade_check(
        market_id="mkt-1", mode=Mode.PAPER, planned_loss=Decimal("5"),
        planned_cost=Decimal("15"), balance=CAPITAL, recovery_ready=True,
    )
    assert not allowed and reason is ReasonCode.INSUFFICIENT_BALANCE
    repo.release_reservation("cid-1")
    assert repo.active_reserved_total() == 0
    assert risk.pre_trade_check(
        market_id="mkt-1", mode=Mode.PAPER, planned_loss=Decimal("5"),
        planned_cost=Decimal("15"), balance=CAPITAL, recovery_ready=True,
    )[0]


def test_daily_loss_limit_locks_the_day(clock, repo):
    risk = manager(clock, repo)
    risk.record_result(realized_pnl=Decimal("-40.01"), market_id="mkt-1", mode=Mode.PAPER)
    day = repo.get_risk_day(START.date())
    assert day.locked is True
    allowed, reason, _ = risk.pre_trade_check(
        market_id="mkt-1", mode=Mode.PAPER, planned_loss=Decimal("1"),
        planned_cost=Decimal("1"), balance=CAPITAL, recovery_ready=True,
    )
    assert not allowed and reason is ReasonCode.DAILY_LOSS_LIMIT
    assert risk.status(Mode.PAPER).locked is True


def test_profit_lock_stops_trading_but_is_not_a_loss(clock, repo):
    cfg = config(daily_profit_lock_pct=Decimal("0.05"))
    risk = manager(clock, repo, cfg)
    update = risk.record_result(realized_pnl=Decimal("50"), market_id="mkt-1", mode=Mode.PAPER)
    assert update.locked and "PROFIT_LOCK" in update.events
    allowed, reason, _ = risk.pre_trade_check(
        market_id="mkt-2", mode=Mode.PAPER, planned_loss=Decimal("1"),
        planned_cost=Decimal("1"), balance=CAPITAL, recovery_ready=True,
    )
    assert not allowed and reason is ReasonCode.PROFIT_LOCK


def test_daily_loss_limit_is_a_fixed_usdc_amount(clock, repo):
    # Limit is 1.50 USDC no matter the capital (1000): realized -1.50 locks the day.
    cfg = config(daily_loss_limit_usd=Decimal("1.50"))
    risk = manager(clock, repo, cfg)
    assert risk.daily_loss_limit() == Decimal("1.50")
    risk.record_result(realized_pnl=Decimal("-0.80"), market_id="a", mode=Mode.PAPER)
    allowed, reason, _ = risk.pre_trade_check(
        market_id="b", mode=Mode.PAPER, planned_loss=Decimal("0.2"),
        planned_cost=Decimal("1"), balance=CAPITAL, recovery_ready=True,
    )
    assert allowed or reason is not ReasonCode.DAILY_LOSS_LIMIT
    risk.record_result(realized_pnl=Decimal("-0.70"), market_id="c", mode=Mode.PAPER)
    allowed, reason, _ = risk.pre_trade_check(
        market_id="d", mode=Mode.PAPER, planned_loss=Decimal("0.2"),
        planned_cost=Decimal("1"), balance=CAPITAL, recovery_ready=True,
    )
    assert not allowed and reason in {ReasonCode.DAILY_LOSS_LIMIT, ReasonCode.COOLDOWN}


def test_two_consecutive_losses_start_cooldown(clock, repo):
    risk = manager(clock, repo)
    first = risk.record_result(realized_pnl=Decimal("-1"), market_id="a", mode=Mode.PAPER)
    assert first.consecutive_losses == 1 and first.cooldown_until is None
    second = risk.record_result(realized_pnl=Decimal("-1"), market_id="b", mode=Mode.PAPER)
    assert second.consecutive_losses == 2
    assert second.cooldown_until == START + timedelta(minutes=15)
    allowed, reason, _ = risk.pre_trade_check(
        market_id="c", mode=Mode.PAPER, planned_loss=Decimal("1"),
        planned_cost=Decimal("1"), balance=CAPITAL, recovery_ready=True,
    )
    assert not allowed and reason is ReasonCode.COOLDOWN


def test_win_resets_consecutive_losses(clock, repo):
    risk = manager(clock, repo)
    risk.record_result(realized_pnl=Decimal("-1"), market_id="a", mode=Mode.PAPER)
    update = risk.record_result(realized_pnl=Decimal("2"), market_id="b", mode=Mode.PAPER)
    assert update.consecutive_losses == 0
    assert update.cooldown_until is None


def test_only_one_active_position_and_one_entry_per_market(clock, repo):
    risk = manager(clock, repo)
    market = make_market()
    repo.create_position(
        market_id=market.market_id, condition_id=market.condition_id,
        asset_id=market.asset_id(Outcome.UP), outcome=Outcome.UP.value,
        shares=Decimal("10"), avg_price=Decimal("0.5"), entry_fee=Decimal(0), mode="PAPER",
    )
    allowed, reason, _ = risk.pre_trade_check(
        market_id="mkt-other", mode=Mode.PAPER, planned_loss=Decimal("1"),
        planned_cost=Decimal("1"), balance=CAPITAL, recovery_ready=True,
    )
    assert not allowed and reason is ReasonCode.ACTIVE_POSITION


def test_entry_allowance_is_consumed_only_after_submission(clock, repo):
    ledger = ExposureLedger(repo)
    market = make_market()
    assert ledger.entries_used(market.market_id, "PAPER") == 0
    rejected = intent_for(repo, market, status=OrderStatus.REJECTED)
    assert ledger.entries_used(market.market_id, "PAPER") == 0
    submitted = intent_for(repo, market, shares="6", status=OrderStatus.SUBMITTING)
    assert submitted.idempotency_key != rejected.idempotency_key
    assert ledger.entries_used(market.market_id, "PAPER") == 1


def test_kill_switch_blocks_and_clearing_keeps_cooldown(clock, repo):
    risk = manager(clock, repo)
    risk.engage_kill_switch("operator test", actor="tester")
    allowed, reason, _ = risk.pre_trade_check(
        market_id="mkt-1", mode=Mode.PAPER, planned_loss=Decimal("1"),
        planned_cost=Decimal("1"), balance=CAPITAL, recovery_ready=True,
    )
    assert not allowed and reason is ReasonCode.KILL_SWITCH
    repo.update_risk_state(consecutive_losses=2, cooldown_until=START + timedelta(minutes=30))
    risk.clear_kill_switch(actor="tester", reason="verified")
    state = repo.get_risk_state()
    assert state.kill_switch is False
    assert state.consecutive_losses == 2
    assert state.cooldown_until is not None


def test_reset_day_lock_never_clears_cooldown_or_kill_switch(clock, repo):
    risk = manager(clock, repo)
    repo.update_risk_day(START.date(), locked=True, profit_lock_triggered=True)
    repo.update_risk_state(kill_switch=True, kill_reason="x", consecutive_losses=2,
                           cooldown_until=START + timedelta(minutes=30))
    risk.reset_day_lock(actor="tester", reason="manual review")
    day = repo.get_risk_day(START.date())
    state = repo.get_risk_state()
    assert day.locked is False and day.profit_lock_triggered is False
    assert state.kill_switch is True
    assert state.consecutive_losses == 2
    assert state.cooldown_until is not None


def position(**overrides):
    base = dict(
        id=1,
        market_id="mkt-1",
        asset_id="up-token",
        outcome="UP",
        status="OPEN",
        shares=Decimal("10"),
        avg_price=Decimal("0.50"),
        cost_basis=Decimal("5.00"),
        realized_pnl=Decimal(0),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_exit_priority_emergency_before_profit():
    exits = HybridExitManager()
    market = make_market()
    book = make_book(market.asset_id(Outcome.UP), bid="0.80", bid_size="100")
    decision = exits.evaluate(
        position=position(), market=market, up_book=book, down_book=None,
        probability=None, config=config(), now=START, risk_locked=True,
    )
    assert decision.action is ExitAction.SELL
    assert decision.reason == "EMERGENCY_EXIT"
    assert decision.priority == 1


def test_stop_loss_and_take_profit():
    exits = HybridExitManager()
    market = make_market()
    low = make_book(market.asset_id(Outcome.UP), bid="0.35", bid_size="100")
    stop = exits.evaluate(
        position=position(), market=market, up_book=low, down_book=None,
        probability=None, config=config(), now=START,
    )
    assert stop.action is ExitAction.SELL and stop.reason == "STOP_LOSS"

    high = make_book(market.asset_id(Outcome.UP), bid="0.62", bid_size="100")
    target = exits.evaluate(
        position=position(), market=market, up_book=high, down_book=None,
        probability=None, config=config(), now=START,
    )
    assert target.action is ExitAction.SELL and target.reason == "TAKE_PROFIT"


def test_thin_book_holds_and_never_submits_infeasible_sell():
    exits = HybridExitManager()
    market = make_market()
    thin = make_book(market.asset_id(Outcome.UP), bid="0.35", bid_size="2")
    decision = exits.evaluate(
        position=position(), market=market, up_book=thin, down_book=None,
        probability=None, config=config(), now=START,
    )
    assert decision.action is ExitAction.HOLD
    assert decision.reason == "SELL_NOT_FEASIBLE"
    assert decision.shadow_action is ExitAction.SELL


def test_subminimum_position_is_not_sent_as_an_invalid_market_sell():
    # CLOB market orders reuse the same order construction constraints; a position below
    # the current book minimum cannot be assumed sellable and must not produce a rejected order.
    exits = HybridExitManager()
    market = make_market()
    book = make_book(market.asset_id(Outcome.UP), bid="0.30", bid_size="100")
    decision = exits.evaluate(
        position=position(shares=Decimal("3"), cost_basis=Decimal("1.5")), market=market,
        up_book=book, down_book=None, probability=None, config=config(), now=START,
    )
    assert decision.action is ExitAction.HOLD
    assert decision.reason == "SELL_NOT_FEASIBLE"
    assert decision.shares == Decimal("0")


def test_no_bid_holds_to_resolution():
    exits = HybridExitManager()
    market = make_market()
    decision = exits.evaluate(
        position=position(), market=market, up_book=None, down_book=None,
        probability=None, config=config(), now=START,
    )
    assert decision.action is ExitAction.HOLD
    assert decision.reason == "NO_BID_HOLD_TO_RESOLUTION"


def test_probability_reversal_sells():
    exits = HybridExitManager()
    market = make_market()
    book = make_book(market.asset_id(Outcome.UP), bid="0.50", bid_size="100")
    bearish = ProbabilityEstimate(
        p_up=0.2, p_down=0.8, confidence="HIGH", uncertainty_low=0.1, uncertainty_high=0.3,
        expected_outcome=Outcome.DOWN, expected_final_twap=Decimal("99000"),
        opening_reference=Decimal("100000"), current_twap=Decimal("99500"), sigma=1.0,
        z=-1.0, regime=Regime.TRENDING, signal_quality=0.9, coverage_seconds=60.0,
    )
    decision = exits.evaluate(
        position=position(), market=market, up_book=book, down_book=None,
        probability=bearish, config=config(), now=START,
    )
    assert decision.action is ExitAction.SELL
    assert decision.reason == "PROBABILITY_REVERSAL"


def test_trailing_stop_is_shadow_only_by_default():
    exits = HybridExitManager()
    market = make_market()
    pos = position(avg_price=Decimal("0.68"), cost_basis=Decimal("6.80"))
    first = make_book(market.asset_id(Outcome.UP), bid="0.70", bid_size="100")
    exits.evaluate(
        position=pos, market=market, up_book=first, down_book=None,
        probability=None, config=config(), now=START,
    )
    second = make_book(market.asset_id(Outcome.UP), bid="0.55", bid_size="100")
    decision = exits.evaluate(
        position=pos, market=market, up_book=second, down_book=None,
        probability=None, config=config(), now=START,
    )
    assert decision.action is ExitAction.HOLD
    assert "trailing_shadow" in decision.metadata


def features(**overrides):
    base = dict(
        returns={5: -0.0010, 15: -0.0020, 30: -0.0020, 60: -0.0030, 180: 0.0005},
        directions={5: -1, 15: -1, 30: -1, 60: -1, 180: 1},
        weighted_direction=-1,
        realized_vol_60s=0.0002,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_momentum_reversal_sells_when_short_horizon_flips_against_position():
    exits = HybridExitManager()
    market = make_market()
    book = make_book(market.asset_id(Outcome.UP), bid="0.50", bid_size="100")
    decision = exits.evaluate(
        position=position(opened_at=START - timedelta(seconds=60)), market=market,
        up_book=book, down_book=None, probability=None, config=config(),
        features=features(), now=START, remaining_seconds=120,
    )
    assert decision.action is ExitAction.SELL
    assert decision.reason == "MOMENTUM_REVERSAL"
    assert decision.priority == 5
    assert decision.metadata["momentum_z60"] < 0


def test_momentum_rule_does_not_sell_when_trend_agrees_or_position_is_new():
    exits = HybridExitManager()
    market = make_market()
    book = make_book(market.asset_id(Outcome.UP), bid="0.50", bid_size="100")
    agreeing = features(
        returns={5: 0.001, 15: 0.002, 30: 0.002, 60: 0.003, 180: 0.001},
        directions={5: 1, 15: 1, 30: 1, 60: 1, 180: 1},
        weighted_direction=1,
    )
    aligned = exits.evaluate(
        position=position(opened_at=START - timedelta(seconds=60)), market=market,
        up_book=book, down_book=None, probability=None, config=config(),
        features=agreeing, now=START, remaining_seconds=120,
    )
    assert aligned.action is ExitAction.HOLD

    fresh = exits.evaluate(
        position=position(opened_at=START), market=market,
        up_book=book, down_book=None, probability=None, config=config(),
        features=features(), now=START, remaining_seconds=120,
    )
    assert fresh.action is ExitAction.HOLD


def test_liquidity_deterioration_sells_when_book_thins_and_widens():
    exits = HybridExitManager()
    market = make_market()
    pos = position(opened_at=START - timedelta(seconds=60))
    healthy = make_book(market.asset_id(Outcome.UP), bid="0.48", ask="0.50", bid_size="1000", ask_size="1000")
    exits.evaluate(
        position=pos, market=market, up_book=healthy, down_book=None,
        probability=None, config=config(), now=START, remaining_seconds=180,
    )
    thin = make_book(
        market.asset_id(Outcome.UP), bid="0.47", ask="0.51", bid_size="100", ask_size="100",
        ts=START + timedelta(seconds=5),
    )
    decision = exits.evaluate(
        position=pos, market=market, up_book=thin, down_book=None,
        probability=None, config=config(), now=START + timedelta(seconds=5), remaining_seconds=175,
    )
    assert decision.action is ExitAction.SELL
    assert decision.reason == "LIQUIDITY_DETERIORATION"
    assert decision.priority == 6


def test_liquidity_rule_holds_when_depth_drop_is_not_material():
    exits = HybridExitManager()
    market = make_market()
    pos = position(opened_at=START - timedelta(seconds=60))
    healthy = make_book(market.asset_id(Outcome.UP), bid="0.48", ask="0.50", bid_size="1000", ask_size="1000")
    exits.evaluate(
        position=pos, market=market, up_book=healthy, down_book=None,
        probability=None, config=config(), now=START, remaining_seconds=180,
    )
    thinner = make_book(market.asset_id(Outcome.UP), bid="0.48", ask="0.50", bid_size="600", ask_size="600")
    decision = exits.evaluate(
        position=pos, market=market, up_book=thinner, down_book=None,
        probability=None, config=config(), now=START + timedelta(seconds=5), remaining_seconds=175,
    )
    assert decision.action is ExitAction.HOLD


def probability_estimate(p_up: float, *, expected: Outcome = Outcome.UP) -> ProbabilityEstimate:
    return ProbabilityEstimate(
        p_up=p_up, p_down=1 - p_up, confidence="HIGH", uncertainty_low=0.1,
        uncertainty_high=0.3, expected_outcome=expected, expected_final_twap=Decimal("100100"),
        opening_reference=Decimal("100000"), current_twap=Decimal("100050"), sigma=1.0,
        z=1.0, regime=Regime.TRENDING, signal_quality=0.9, coverage_seconds=60.0,
    )


def test_dynamic_take_profit_never_raises_target_above_base_for_a_strong_favourite():
    exits = HybridExitManager()
    market = make_market()
    book = make_book(market.asset_id(Outcome.UP), bid="0.61", bid_size="100")
    decision = exits.evaluate(
        position=position(opened_at=START - timedelta(seconds=60)), market=market,
        up_book=book, down_book=None, probability=probability_estimate(0.80), config=config(),
        now=START, remaining_seconds=120,
    )
    # +22% ROI with a strong favourite must sell: the target stays at the base 20%.
    assert decision.action is ExitAction.SELL
    assert decision.reason == "TAKE_PROFIT"
    assert decision.metadata["take_profit_target"] == Decimal("0.20")


def test_dynamic_take_profit_locks_a_weak_edge_earlier():
    exits = HybridExitManager()
    market = make_market()
    book = make_book(market.asset_id(Outcome.UP), bid="0.58", bid_size="100")
    decision = exits.evaluate(
        position=position(opened_at=START - timedelta(seconds=60)), market=market,
        up_book=book, down_book=None, probability=probability_estimate(0.52), config=config(),
        now=START, remaining_seconds=120,
    )
    assert decision.action is ExitAction.SELL
    assert decision.reason == "TAKE_PROFIT"
    assert decision.metadata["take_profit_target"] == Decimal("0.15")


def test_fixed_trade_size_cannot_bypass_two_percent_risk_cap(clock, repo):
    cfg = RuntimeConfig(capital_usd=Decimal("20"), trade_size_usd=Decimal("5"))
    risk = manager(clock, repo, cfg)
    allowed, reason, detail = risk.pre_trade_check(
        market_id="mkt-1", mode=Mode.PAPER, planned_loss=Decimal("5.2"),
        planned_cost=Decimal("5.2"), balance=Decimal("20"), recovery_ready=True,
    )
    assert not allowed and reason is ReasonCode.RISK_LIMIT
    assert "0.40" in detail
    allowed, reason, _ = risk.pre_trade_check(
        market_id="mkt-2", mode=Mode.PAPER, planned_loss=Decimal("0.30"),
        planned_cost=Decimal("0.30"), balance=Decimal("20"), recovery_ready=True,
    )
    assert allowed and reason is ReasonCode.OK


def test_capital_follows_wallet_balance_only_when_safe():
    from btc5m_bot.risk.capital import balance_capital_target as target

    D = Decimal
    # flat, no trades yet today: adopt the balance (rounded down to cents)
    assert target(balance=D("87.4567"), current_capital=D("1000"), flat=True, day_trades=0) == D("87.45")
    # unset capital is filled even after trades
    assert target(balance=D("50"), current_capital=None, flat=True, day_trades=3) == D("50.00")
    # open position / reserved order: balance understates the bankroll, leave capital alone
    assert target(balance=D("40"), current_capital=D("100"), flat=False, day_trades=0) is None
    # after the first trade of the day the daily loss limit stays anchored
    assert target(balance=D("90"), current_capital=D("100"), flat=True, day_trades=1) is None
    # nothing to change / bad balance
    assert target(balance=D("100"), current_capital=D("100"), flat=True, day_trades=0) is None
    assert target(balance=None, current_capital=D("100"), flat=True, day_trades=0) is None
    assert target(balance=D("0"), current_capital=D("100"), flat=True, day_trades=0) is None


# --------------------------------------------------------------------------- audit regressions
def test_status_does_not_deadlock_after_cooldown_expires(clock, repo):
    """The engine gates entries on risk.status().allowed, so status() itself must see an
    expired cooldown as served. Otherwise the bot sticks on CONSECUTIVE_LOSS_LIMIT forever."""
    risk = manager(clock, repo)
    risk.record_result(realized_pnl=Decimal("-1"), market_id="a", mode=Mode.PAPER)
    risk.record_result(realized_pnl=Decimal("-1"), market_id="b", mode=Mode.PAPER)
    during = risk.status(Mode.PAPER, balance=CAPITAL)
    assert not during.allowed and during.reason is ReasonCode.COOLDOWN

    clock.advance(15 * 60 + 1)
    after = risk.status(Mode.PAPER, balance=CAPITAL)
    assert after.allowed, (after.reason, after.detail)
    assert after.consecutive_losses == 0 and after.cooldown_until is None
    # and the entry-time check agrees with the gate
    ok, reason, _ = risk.pre_trade_check(
        market_id="c", mode=Mode.PAPER, planned_loss=Decimal("1"),
        planned_cost=Decimal("1"), balance=CAPITAL, recovery_ready=True,
    )
    assert ok and reason is ReasonCode.OK


def test_status_still_blocks_while_cooldown_is_running(clock, repo):
    risk = manager(clock, repo)
    risk.record_result(realized_pnl=Decimal("-1"), market_id="a", mode=Mode.PAPER)
    risk.record_result(realized_pnl=Decimal("-1"), market_id="b", mode=Mode.PAPER)
    clock.advance(14 * 60)
    st = risk.status(Mode.PAPER, balance=CAPITAL)
    assert not st.allowed and st.reason is ReasonCode.COOLDOWN
    assert st.consecutive_losses == 2


def test_cooldown_expiry_does_not_clear_a_daily_lock(clock, repo):
    risk = manager(clock, repo, config(daily_loss_limit_usd=Decimal("1.5")))
    risk.record_result(realized_pnl=Decimal("-1"), market_id="a", mode=Mode.PAPER)
    risk.record_result(realized_pnl=Decimal("-1"), market_id="b", mode=Mode.PAPER)
    clock.advance(15 * 60 + 1)
    st = risk.status(Mode.PAPER, balance=CAPITAL)
    assert not st.allowed and st.reason is ReasonCode.DAILY_LOSS_LIMIT


def test_reset_day_lock_warns_when_loss_limit_will_relock(clock, repo):
    risk = manager(clock, repo, config(daily_loss_limit_usd=Decimal("1.5")))
    risk.record_result(realized_pnl=Decimal("-1.6"), market_id="a", mode=Mode.PAPER)
    assert repo.get_risk_day(START.date()).locked is True
    out = risk.reset_day_lock(actor="tester", reason="review")
    assert out["was_locked"] is True and out["will_relock"] is True
    assert out["daily_pnl"] == "-1.60" or Decimal(out["daily_pnl"]) == Decimal("-1.6")
    # the lock really does come straight back on the next entry check
    allowed, reason, _ = risk.pre_trade_check(
        market_id="b", mode=Mode.PAPER, planned_loss=Decimal("0.2"),
        planned_cost=Decimal("1"), balance=CAPITAL, recovery_ready=True,
    )
    assert not allowed and reason is ReasonCode.DAILY_LOSS_LIMIT


def test_reset_day_lock_after_profit_lock_does_not_relock(clock, repo):
    risk = manager(clock, repo, config(daily_profit_lock_pct=Decimal("0.01")))
    risk.record_result(realized_pnl=Decimal("11"), market_id="a", mode=Mode.PAPER)
    assert repo.get_risk_day(START.date()).profit_lock_triggered is True
    out = risk.reset_day_lock(actor="tester", reason="review")
    assert out["will_relock"] is False


def test_exit_limit_covers_every_bid_level_needed_for_a_full_exit():
    # 10 shares, but the top bid only holds 4: feasibility walks three levels, so the SELL limit
    # must reach the worst level it needs (0.33), not just the best bid (0.35).
    exits = HybridExitManager()
    market = make_market()
    book = make_book(
        market.asset_id(Outcome.UP),
        bids=[("0.35", "4"), ("0.34", "4"), ("0.33", "4")],
    )
    decision = exits.evaluate(
        position=position(), market=market, up_book=book, down_book=None,
        probability=None, config=config(), now=START,
    )
    assert decision.action is ExitAction.SELL and decision.reason == "STOP_LOSS"
    assert decision.limit_price == Decimal("0.33")
    assert decision.feasibility.worst_price == Decimal("0.33")


def test_exit_limit_is_best_bid_when_top_level_is_deep_enough():
    exits = HybridExitManager()
    market = make_market()
    book = make_book(market.asset_id(Outcome.UP), bids=[("0.35", "100"), ("0.30", "100")])
    decision = exits.evaluate(
        position=position(), market=market, up_book=book, down_book=None,
        probability=None, config=config(), now=START,
    )
    assert decision.action is ExitAction.SELL
    assert decision.limit_price == Decimal("0.35")


def test_exit_limit_is_floored_to_the_market_tick():
    exits = HybridExitManager()
    market = make_market()
    book = make_book(market.asset_id(Outcome.UP), bids=[("0.35", "5"), ("0.3349", "5")])
    decision = exits.evaluate(
        position=position(), market=market, up_book=book, down_book=None,
        probability=None, config=config(), now=START,
    )
    assert decision.action is ExitAction.SELL
    assert decision.limit_price == Decimal("0.33")
    assert decision.limit_price % market.tick_size == 0
