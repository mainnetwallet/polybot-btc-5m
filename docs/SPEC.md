# POLYMARKET BTC 5-MINUTE AUTONOMOUS TRADING AGENT
## FINAL MASTER BUILD SPECIFICATION
### Quantitative Trading | Autonomous Execution | Profit Optimization | Risk Protection

# 1. ROLE AND MISSION

Act as a senior quantitative trading engineer, Python architect, algorithmic trading developer, data engineer and security engineer.

Build a complete, production-grade autonomous trading agent for international Polymarket BTC 5-minute UP/DOWN markets.

The primary objective is to identify high-quality trading opportunities and pursue sustainable positive net expectancy through:

- Quantitative BTC analysis.
- Accurate probability estimation.
- Adaptive market intelligence.
- Dynamic entry optimization.
- Profit optimization.
- Intelligent hybrid exits.
- Advanced order execution.
- Capital protection.
- Strategy performance intelligence.
- Automated recovery.

This is not a simple prediction bot or price-alert script.

Build an actual working trading application.

Never promise guaranteed profit. Never force trades to increase trading frequency.

The correct decision when no genuine trading edge exists is NO TRADE.

# 2. DEVELOPMENT GROUND RULES

- Inspect the actual repository and environment first.
- Do not assume a previous implementation exists.
- Reuse existing components when available.
- If the workspace is empty, propose a clean project structure.
- Never fabricate repository audit results.
- Avoid unnecessary files and duplicate functionality.
- No fake implementations or placeholder methods in core functionality.
- Preserve existing user data and trading records.
- Verify current official Polymarket documentation before implementation.
- Official documentation takes precedence over assumptions in this specification.
- Report only actual test and execution results.
- Never invent backtesting or profitability statistics.

Official documentation:
https://docs.polymarket.com/

Official Python SDK:
https://github.com/Polymarket/py-sdk

## Internal naming convention

Use:

- BOT_BASELINE: Initial trading system.
- BOT_FINAL: Complete enhanced trading system.
- POLYMARKET_CLOB_V2: External exchange protocol.

Do not confuse internal bot versions with Polymarket protocol versions.

# 3. TECHNOLOGY STACK

Use:

- Python 3.11+
- asyncio
- httpx
- websockets
- Official unified Polymarket Python SDK.
- SQLite + SQLAlchemy + WAL for transactional state.
- Alembic for schema migrations.
- Parquet + DuckDB for high-frequency market analytics.
- FastAPI for backend.
- Lightweight responsive frontend.
- pytest and pytest-asyncio.
- Pydantic Settings.

## SDK Requirements

Investigate the current official unified Python SDK.

Current package name:

polymarket-client

Python import:

polymarket

Verify the latest stable release directly from the official GitHub repository and package metadata.

Pin the exact verified version.

Do not use deprecated legacy SDKs.

Keep exchange integration behind a small adapter layer so future SDK changes do not require rewriting the trading engine.

Do not arbitrarily restrict the number of modules. Additional modules must have clearly justified responsibilities.

# 4. USER TRADING CONFIGURATION

| Setting | Configuration |
|---|---|
| Platform | International Polymarket |
| Market | BTC 5-minute |
| Direction | UP / DOWN |
| Wallet | User's existing Polymarket wallet |
| Collateral | Verified current pUSD |
| Capital | User-configurable |
| Risk per trade | Maximum 2% |
| Entry | Dynamic |
| Entry window | First 120 seconds |
| Exit | Hybrid |
| Target operating mode | LIVE |
| Initial application mode | PAPER |

Capital must be configurable.

Do not assume a fixed starting balance.

LIVE trading must remain disabled until all activation requirements pass and the user explicitly activates it.

# 5. IMMUTABLE RISK LIMITS

The risk manager has absolute authority over every order.

Hard limits:

- Maximum planned loss per trade: 2% of allocated capital.
- Maximum daily loss: 4% of starting daily capital.
- Maximum consecutive losses: 2, followed by configurable cooldown.
- Maximum one initial entry per market.
- Maximum one active position by default.
- No Martingale.
- No revenge trading.
- No averaging down.
- No unrestricted scaling.
- No automatic risk increase after wins.
- No bypassing risk checks.

For binary markets, assume the complete position cost can be lost.

Include applicable fees and execution costs in the planned risk budget.

Do not use stop-loss assumptions to increase position size.

Adaptive strategies and machine learning must never modify hard risk limits.

# 6. CURRENT POLYMARKET PROTOCOL

Verify all current protocol specifications before coding.

## CLOB V2

Support the current production CLOB V2 protocol.

Verify:

- Order signing structure.
- Exchange contracts.
- Authentication.
- Wallet signature type.
- Funder address.
- Collateral.
- Allowances.
- Order lifecycle.
- Settlement.
- Redemption.

## pUSD

Use verified current CLOB V2 collateral.

Do not assume USDC.e remains the active collateral.

Implement:

- pUSD balance checking.
- Required allowance verification.
- Collateral reconciliation.
- Secure approval handling.

Never grant unlimited token allowances.

Only request the required amount with explicit authorization for approval transactions.

## Market Version

Read the actual market version.

For V2 markets, use the correct position identifiers.

For V1 markets, use the correct CTF token identifiers.

Preserve all available market identifiers.

Never assume both versions have identical asset routing.

Unsupported or ambiguous markets must be rejected.

## Data API

Use the current supported Data API V2 contract.

Do not depend on obsolete V1 Data API endpoints.

# 7. MARKET DISCOVERY AND LIFECYCLE

Find active BTC 5-minute UP/DOWN markets dynamically.

Never hardcode market IDs, token IDs, position IDs or slugs.

Validate:

- Market status.
- Start time.
- End time.
- Market version.
- Outcomes.
- Resolution rules.
- Minimum order size.
- Tick size.
- Fee schedule.
- Order-book availability.
- Geographic eligibility.

Lifecycle:

DISCOVERED
ACTIVE_ENTRY_WINDOW
ENTRY_CLOSED
RESOLVING
SETTLED

Rules:

1. Detect exact market boundaries.
2. Synchronize with server time.
3. Scan immediately when a market becomes active.
4. Allow new entries only in the first 120 seconds.
5. After 120 seconds, absolutely prohibit new entries.
6. Continue managing existing positions.
7. Reset entry eligibility for each new market.
8. Prevent duplicate market execution.
9. Persist market state across restarts.

# 8. BTC MARKET DATA

Collect:

- Current BTC/USD price.
- 5-second returns.
- 15-second returns.
- 30-second returns.
- 1-minute returns.
- 3-minute returns.
- Trading volume.
- Realized volatility.
- Momentum.
- Price acceleration.
- Short-term direction.
- Support/resistance context.

Polymarket data:

- UP price.
- DOWN price.
- Best bid.
- Best ask.
- Order-book depth.
- Spread.
- Liquidity.
- Recent trades.
- Market-implied probability.
- Expected execution price.

Record timestamps and source identity.

Never make decisions using stale or inconsistent data.

# 9. CHAINLINK TWAP AND ORACLE RESOLUTION

This component is critical.

The prediction model must understand the actual variable used to settle each market.

Never assume Binance or Coinbase spot price equals the official resolution price.

## Historical resolution methodologies

Maintain separate historical methodology versions:

- Before August 7, 2026: Single-price snapshot.
- August 7–13, 2026: 30-second TWAP.
- August 14, 2026 onward: 60-second TWAP.

These are known historical transitions, not substitutes for actual market-rule verification.

Use market metadata and resolution text to determine the applicable methodology.

Never mix different settlement methodologies in validation.

## PolyBolt

Investigate the official PolyBolt WebSocket:

wss://ws-live-v2.polymarket.com/ws

Investigate:

- price.crypto
- price.crypto.twap

Verify:

- Authentication requirements.
- Chainlink source.
- BTC/USD symbol.
- TWAP window.
- Observation timestamps.
- Data latency.
- Reconnection.
- Subscription behaviour.

Prefer the currently supported PolyBolt interface for TWAP data.

Treat legacy RTDS reference-price channels as deprecated where applicable.

## Resolution Rules Capture

For every market, store:

- Original resolution text.
- Data source.
- Opening reference.
- Closing reference.
- TWAP methodology.
- Boundary timestamps.
- Tie conditions.
- Precision.
- Rounding.
- Rules fingerprint.

Require explicit user approval of resolution-rule fingerprints before LIVE trading.

## Resolution Simulator

Implement a deterministic resolve() function.

It must:

- Reproduce the captured market rules.
- Identify opening and closing observations.
- Select correct timestamps.
- Calculate the outcome.
- Detect missing observations.
- Detect ambiguous rules.
- Compare simulated versus actual resolution.

Never invent missing oracle values.

Never substitute exchange prices for missing settlement data.

Unknown or unverified rules:

NO TRADE — UNVERIFIED_RULES.

Repeated mismatches must pause trading and require manual re-enablement.

# 10. DATA FEED REDUNDANCY

Implement:

- Primary BTC feed.
- Secondary BTC feed.
- Separate Chainlink oracle feed.

Monitor:

- Staleness.
- Connection health.
- Sequence gaps.
- Timestamp skew.
- Frozen prices.
- Abnormal price jumps.
- Provider divergence.
- Oracle-to-exchange basis.

Health states:

HEALTHY
DEGRADED
STALE
DOWN
UNVERIFIED

Default failover mode:

BLOCK.

Automatic verified failover is optional and disabled by default.

Never silently switch providers.

Never combine trailing calculations from different providers.

After switching providers, rebuild the required feature window before trading.

The secondary feed cannot replace the official resolution oracle.

# 11. ADAPTIVE MARKET REGIME DETECTION

Implement:

- TRENDING
- SIDEWAYS
- HIGH_VOLATILITY
- SUDDEN_REVERSAL
- UNKNOWN

Analyze:

- Trend strength.
- Realized volatility.
- Momentum agreement.
- Price acceleration.
- Directional conflict.
- Sudden reversals.

Use hysteresis to prevent unstable classifications.

Regime-specific parameters require sufficient out-of-sample evidence.

HIGH_VOLATILITY, SUDDEN_REVERSAL and UNKNOWN default to NO TRADE until validated.

Adaptive thresholds may only change within user-approved research bounds.

Never modify hard risk limits.

# 12. FALSE SIGNAL DETECTION

Implement:

- Fake Breakout Detection.
- Momentum Exhaustion.
- Sudden Reversal.
- Volume Confirmation.
- Oracle Divergence.
- Abnormal Order Book Activity.
- Spread Expansion.
- Liquidity Withdrawal.
- Price Acceleration Failure.

Each filter must return:

PASS
FAIL
UNAVAILABLE

UNAVAILABLE must never silently pass.

Log every filter result, including rejected signals.

Do not claim to detect manipulation without evidence.

All filters default to SHADOW until validated.

# 13. MULTI-TIMEFRAME CONFIRMATION

Analyze:

- 5 seconds.
- 15 seconds.
- 30 seconds.
- 1 minute.
- 3 minutes.

Calculate:

- Normalized returns.
- Direction.
- Momentum.
- Acceleration.
- Agreement score.
- Conflict flags.

Use transparent weighted confirmation.

Only use information available at decision time.

Insufficient data must be flagged.

These features must complement, not override, official oracle-based probability.

# 14. PROBABILITY ENGINE

Build a TWAP-aware calibrated statistical model.

Inputs:

- Opening oracle reference.
- Current TWAP.
- Remaining TWAP window.
- BTC spot movement.
- Realized volatility.
- Momentum.
- Multi-timeframe agreement.
- Order-book imbalance.
- Remaining market time.
- Market regime.

Outputs:

- UP probability.
- DOWN probability.
- Confidence.
- Uncertainty interval.
- Signal quality.
- Market regime.

Do not blindly apply a simple instantaneous-price Gaussian formula to a TWAP-resolved market.

Model the distribution of the actual settlement variable.

Evaluate:

- Brier score.
- Log loss.
- Calibration.
- Reliability curves.
- Market-implied probability benchmark.

Use only historical information available at each decision time.

If the model cannot demonstrate out-of-sample value, NO TRADE.

# 15. CORRELATION INTELLIGENCE

Secondary inputs:

- ETH price movement.
- Broader crypto-market movement.
- BTC volume.

Correlation features must:

- Default to SHADOW.
- Use trailing observations only.
- Have configurable influence caps.
- Be tested out-of-sample.
- Never independently create a trading signal.
- Never override direct BTC/oracle evidence.
- Never bypass EV or risk checks.

Weak or unstable correlations must have zero influence.

# 16. DYNAMIC ENTRY ENGINE

Initial research parameters:

| Parameter | Initial Value |
|---|---:|
| Minimum probability | 60% |
| Minimum net edge | 5 percentage points |
| Maximum spread | $0.03 |
| Maximum estimated slippage | $0.01 |
| Entry window | 120 seconds |
| Maximum initial entries | 1 per market |

All parameters must be configurable and validated.

These are experimental starting values, not proven profitable thresholds.

Evaluate opportunities every configurable 1–2 seconds.

Analyze:

- UP/DOWN probability.
- BTC momentum.
- Oracle distance.
- TWAP behaviour.
- Multi-timeframe agreement.
- Market regime.
- False-signal filters.
- Liquidity.
- Spread.
- Remaining time.
- Total execution cost.

Only enter when all mandatory conditions pass.

Use expected executable prices, not midpoint prices.

Never chase moving prices.

Never enter after 120 seconds.

No sufficiently positive expected value:

NO TRADE.

# 17. PROFIT OPTIMIZATION ENGINE

Implement:

- Dynamic entry-price evaluation.
- Net expected-value calculation.
- Fee-aware sizing.
- Liquidity-aware execution.
- Slippage prediction.
- Maker versus taker cost comparison.
- Entry timing optimization.
- Signal quality ranking.
- Opportunity rejection.

Calculate expected return after:

- Entry price.
- Entry fees.
- Exit costs.
- Spread.
- Slippage.
- Expected settlement payout.

Optimize for long-term net expectancy and risk-adjusted returns, not win rate alone.

Do not optimize with future data.

# 18. MINIMUM ORDER AND CAPITAL COMPATIBILITY

Never assume a universal share minimum.

Read actual market metadata:

- minimumOrderSize.
- orderMinSize.
- minimumTickSize.
- Order precision.
- Valid BUY constraints.
- Valid SELL constraints.

Determine the exact unit of each field from the official API/SDK contract.

Verify whether the minimum is expressed in shares, collateral notional, or another unit.

Before every trade, calculate the smallest valid complete order cost.

Include applicable fees and execution costs.

### Capital Compatibility Report

Test share prices:

$0.40
$0.45
$0.50
$0.55
$0.60
$0.65
$0.70

For each scenario report:

- Minimum valid shares.
- Minimum valid order cost.
- Fees.
- Worst-case binary loss.
- Required 2% risk budget.
- Minimum compatible capital.

Formula:

Minimum Required Capital =
Minimum Valid Worst-Case Order Cost / 0.02.

Use actual live market constraints rather than hardcoded assumptions.

Do not automatically increase capital or order size.

If minimum valid order cost exceeds the 2% risk budget:

NO TRADE — BELOW_MIN_ORDER.

Phase 0 must report whether the user's configured capital can trade legally within the specified risk limit.

# 19. DYNAMIC POSITION SIZING

Calculate size using:

- Allocated capital.
- Available pUSD.
- Maximum 2% risk.
- Worst-case settlement cost.
- Share price.
- Fees.
- Expected slippage.
- Liquidity.
- Minimum order size.
- Pending exposure.
- Maximum order value.

Position size must not exceed any of these independent limits:

1. Risk budget.
2. Available balance.
3. Liquidity cap.
4. Maximum order value.
5. Remaining exposure budget.

Round down to valid increments.

If the minimum valid order cannot fit:

NO TRADE.

Optional signal-quality-based size reduction is permitted.

No automatic size increase after wins.

Risk manager independently recalculates and vetoes discrepancies.

# 20. FEE AND TAKER DELAY

Read actual:

feesEnabled
feeSchedule

Never hardcode fee rates.

Account for:

- Taker fees.
- Maker fees.
- Entry fees.
- Early-exit fees.
- Historical fee changes.
- Confirmed rebates only.

## Taker Delay

Verify the current crypto taker delay from official market metadata and documentation.

The September 4, 2026 changelog reported 150ms for crypto markets.

Do not blindly assume all markets permanently use the same value.

Include the verified delay in:

- Execution modelling.
- Expected slippage.
- Latency.
- Expected value.
- Paper simulation.

Use conservative fallback assumptions when metadata is unavailable.

If required cost information is missing, block LIVE entry.

# 21. ADVANCED EXECUTION INTELLIGENCE

Implement:

- Order Book Walking.
- Smart Limit Pricing.
- Maker/Taker Comparison.
- Slippage Prediction.
- Partial-Fill Management.
- Order Timeout.
- Cancellation Verification.
- Failed Order Recovery.
- Duplicate Order Prevention.
- Persistent Idempotency Keys.

Never submit a duplicate order after timeout without verifying the original.

Never chase beyond maximum slippage.

A failed cancellation must not be treated as a completed cancellation.

All position quantities must reflect confirmed fills.

# 22. EXIT FEASIBILITY — MANDATORY

Before implementing hybrid exits, investigate actual SELL order restrictions.

Verify through:

- Official Polymarket documentation.
- Current unified Python SDK.
- Market-specific metadata.
- CLOB validation rules.
- Supported order types.
- Share precision requirements.

Determine whether minimum order size applies to SELL orders and whether its units or limits differ from BUY orders.

Do not assume BUY and SELL have identical restrictions without verification.

### Before every BUY

Verify that the entire intended position can be exited with a valid SELL order.

Test feasibility for:

1. Full-position SELL.
2. Stop-loss SELL.
3. Take-profit SELL.
4. Partial-exit SELL.
5. Remaining-position size.
6. Residual dust.
7. Partial fills.

For partial exits:

- SELL quantity must satisfy the applicable minimum.
- Remaining quantity must not become untradeable dust unless explicitly accepted.
- Quantities must respect share precision.
- Exit orders must remain within available position balance.

If minimum-size restrictions make partial exits impossible:

Disable partial exits for that position and use a validated full-position exit strategy.

If full-position exit is impossible:

Do not enter, except when the user has explicitly approved a resolution-only strategy.

Implement:

SELL feasibility validation before BUY.

Add reason codes:

EXIT_NOT_FEASIBLE
SELL_BELOW_MINIMUM
PARTIAL_EXIT_UNAVAILABLE
RESIDUAL_DUST

Never report an unsuccessful SELL as completed.

Handle residual dust honestly and manage it through resolution when required.

Automated tests must cover minimum SELL rejection, partial fills, stop-loss feasibility, take-profit feasibility and residual balances.

# 23. HYBRID EXIT MANAGER

Initial research parameters:

- Take Profit: +20% ROI.
- Stop Loss: -25% ROI.

These are configurable starting values, not proven optimal settings.

Implement:

1. Dynamic Take Profit.
2. Stop Loss.
3. Trailing Stop.
4. Momentum Reversal Exit.
5. Probability Reversal Exit.
6. Liquidity Deterioration Exit.
7. Time-Based Exit.
8. Hold-to-Resolution.

Exit precedence:

1. Emergency safety or critical data failure.
2. Stop Loss.
3. Trailing Stop.
4. Take Profit.
5. Probability/Momentum Reversal.
6. Compare hold-to-resolution EV with immediate selling EV.
7. Otherwise hold.

Use executable prices and total transaction costs.

Verify every actual SELL fill.

Manage partial fills and remaining shares.

A stop-loss is not a maximum-loss guarantee.

Compare hybrid exits against hold-to-resolution using out-of-sample data.

# 24. PORTFOLIO EXPOSURE MONITOR

Maintain a persistent exposure ledger.

Track:

- Filled positions.
- Pending orders.
- Partially filled orders.
- Reserved capital.
- Entry fees.
- Available pUSD.
- Allowances.
- Daily committed risk.

Reserve risk before order submission.

Release reservations only after verified order status.

Ambiguous orders keep their reservation.

Before every order:

- Recalculate committed exposure.
- Recalculate remaining daily budget.
- Verify actual balances.
- Verify pending orders.
- Verify active positions.

On reconciliation mismatch:

NO NEW ENTRIES.

Wallet and CLOB state are the source of truth.

# 25. RISK AND PROFIT PROTECTION

Implement:

- 2% per-trade risk.
- 4% daily loss shutdown.
- Consecutive-loss cooldown.
- Daily profit lock.
- Maximum drawdown protection.
- Abnormal market shutdown.
- Extreme volatility protection.
- Feed failure protection.
- Oracle mismatch protection.
- Latency degradation protection.
- Liquidity protection.
- Emergency kill switch.

Daily profit target must be configurable.

When achieved, stop opening new trades.

Protection state must persist across restarts.

Open positions continue to be managed even when new entries are disabled.

# 26. EXECUTION LATENCY MONITOR

Record:

- Data timestamp.
- Signal timestamp.
- Order-built timestamp.
- Submission timestamp.
- Acknowledgement timestamp.
- First-fill timestamp.
- Last-fill timestamp.
- Cancellation timestamps.

Calculate:

- Signal-to-order latency.
- Order-to-acknowledgement latency.
- Order-to-fill latency.
- Cancellation latency.
- Rolling p50/p95/p99.

Reject new entries when latency or data age exceeds configured safety limits.

If expected acknowledgement falls outside the entry window:

NO TRADE.

Repeated latency breaches must pause new entries.

# 27. HEARTBEAT RUNTIME RULES

Investigate the current official Polymarket Heartbeat API.

Modern endpoint to verify:

POST /v1/heartbeats

Verify the current request/response structure and SDK support.

Requirements:

- First accepted heartbeat uses an empty heartbeat ID when required.
- Store the latest returned heartbeat ID.
- Send the latest ID with every subsequent request.
- Serialize heartbeat-ID updates.
- Update the stored ID after every successful response.
- Use the documented heartbeat interval; current guidance should be verified, with approximately 5-second scheduling as the initial target.
- Handle invalid or mismatched IDs.
- Handle network failure.
- Handle timeout.
- Handle worker restart.

### Isolated Runtime

Heartbeat execution must run independently of:

- Strategy analytics.
- Dashboard rendering.
- Database-heavy processing.
- Blocking market calculations.
- General trading-engine I/O.

Use a dedicated asynchronous event loop, isolated worker or supervised process with bounded timeouts.

Only one heartbeat sender may operate for a given API key.

### Main Engine Liveness

Independence must not mean heartbeats continue forever when the trading engine is unhealthy.

Implement a supervisor/liveness lease:

- The main trading engine periodically confirms it is healthy.
- Heartbeat execution continues only while the lease is valid.
- If the main process dies or becomes unhealthy, heartbeat sending stops.
- Heartbeat expiry can then trigger the platform's resting-order cancellation safeguard.

### Important API-Key Safety

Investigate whether the first accepted heartbeat activates API-key-wide timeout cancellation and whether the current API supports disarming.

Use a dedicated API key exclusively for this bot if cancellation affects all orders under that key.

Never share that key with unrelated trading processes.

### Heartbeat Expiry Recovery

Treat heartbeat-triggered cancellation as a normal recovery event.

Record:

CANCELLED_BY_HEARTBEAT

Then:

1. Query actual CLOB order states.
2. Verify cancellations.
3. Check partial fills.
4. Reconcile wallet balances.
5. Reconcile positions.
6. Restore exposure reservations.
7. Attach exit management.
8. Enter recovery mode as appropriate.

Heartbeat cancels resting orders, not filled positions.

Never treat it as a replacement for disaster recovery.

Test heartbeat failure, timeout, ID rotation, worker crash, reconnection and restart.

# 28. STRATEGY PERFORMANCE INTELLIGENCE

Implement testable strategy variants:

- Momentum.
- Reversal.
- Breakout.
- Trend Confirmation.

Every strategy must share:

- Probability model.
- Entry gates.
- EV calculation.
- Risk manager.
- Execution layer.
- Exit manager.

Only one initial strategy entry per market.

Other strategies may run in SHADOW.

Compare performance through:

- Walk-forward testing.
- Untouched holdout data.
- Out-of-sample validation.
- Bootstrap confidence intervals.
- Profit factor.
- Drawdown.
- Net expectancy.
- Regime-specific performance.

Automatically disable poorly performing strategies only under validated statistical rules.

Never increase hard risk limits.

# 29. LOSS ANALYSIS ENGINE

Classify losses:

- Wrong Direction.
- Late Entry.
- Poor Entry Price.
- Oracle Mismatch.
- Sudden Volatility.
- Liquidity Problem.
- Exit Timing Failure.
- Stale Feed.
- Divergent Feed.
- Latency Breach.
- Resolution Mismatch.
- Recovery Related.

Separate bad decisions from normal statistical losing outcomes.

Generate daily and weekly reports.

Recommendations only.

Do not automatically change strategy or risk settings based on one loss.

# 30. STATISTICAL VALIDATION

Minimum PAPER research target:

300 trades.

This is not automatic LIVE approval.

Require:

- Positive net expectancy after all costs.
- Out-of-sample validation.
- Block bootstrap confidence intervals.
- Minimum sample-size requirements.
- Market-regime coverage.
- Maximum drawdown limits.
- Probability calibration.
- Execution realism.

Restore and enforce:

### Multiple-Comparison Protection

Account for multiple strategies, parameter combinations, filters and regime comparisons.

Use an appropriate correction or statistical procedure.

Do not report the best result from hundreds of experiments without correcting for selection bias.

### Parameter Sensitivity

Test a predefined parameter grid.

Report whether profitability is stable across a region of parameters rather than depending on one narrowly optimized value.

### Ablation Testing

Compare:

- Baseline.
- Baseline + individual enhancements.
- Complete enhanced strategy.

Use identical data, costs and validation periods.

Features that do not add validated value remain OFF or SHADOW.

Never optimize using future information.

If no reliable edge exists, maintain NO TRADE.

# 31. PAPER TRADING REALISM

Simulate:

- Queue-position uncertainty.
- Order-book walking.
- Partial fills.
- Unfilled orders.
- Taker delay.
- Network latency.
- Slippage.
- Order cancellation.
- Real transaction costs.

Never assume every limit order fills.

Paper performance must be labeled as simulated.

Treat paper execution results as preliminary and potentially optimistic, not proof of live profitability.

# 32. DISASTER RECOVERY

Support:

- Application crash.
- Windows restart.
- Power failure.
- Internet disconnection.
- API outage.
- WebSocket outage.
- System sleep.
- Clock jump.
- Database corruption.

Implement a persistent write-ahead order journal.

Before every order submission, persist:

- Market.
- Asset identifier.
- Side.
- Price.
- Quantity.
- Strategy version.
- Decision timestamp.
- Idempotency key.

States:

INTENDED
SUBMITTING
ACKED
PARTIAL
FILLED
CANCEL_PENDING
CANCELLED
REJECTED
UNKNOWN

Never blindly retry UNKNOWN orders.

### Recovery Sequence

1. Database integrity check.
2. Load persisted state.
3. Restore protection limits.
4. Verify clock.
5. Reconnect feeds.
6. Reconnect CLOB.
7. Query wallet.
8. Query open orders.
9. Query recent trades.
10. Reconcile positions.
11. Resolve unknown intents.
12. Restore exposure.
13. Attach exit manager.
14. Enter READY only after all checks pass.

Every restart disables new entries until reconciliation succeeds.

Never reset daily loss, cooldown, profit lock or drawdown during restart.

Implement crash-loop protection.

Document Windows Task Scheduler or service supervision.

# 33. DATABASE AND DATA STORAGE

Use:

SQLite:
- Orders.
- Positions.
- Balances.
- Risk state.
- Exposure reservations.
- Order journal.
- Recovery state.

Use SQLite WAL mode and appropriate transactional controls.

Use Parquet + DuckDB for:

- BTC time-series data.
- Oracle observations.
- Order-book snapshots.
- Signal observations.
- Historical analytical datasets.

Implement:

- Batch writes.
- Retention policies.
- Compression.
- Integrity checks.
- Verified backups.
- Recovery procedures.

Use Alembic migrations for schema changes.

Never destroy existing user data.

# 34. DASHBOARD

Create a responsive local dashboard.

Account:

- Wallet balance.
- Available pUSD.
- Allocated capital.
- Exposure.
- Daily P&L.
- Remaining risk budget.

Market:

- BTC price.
- Chainlink TWAP.
- Opening reference.
- UP probability.
- DOWN probability.
- Share prices.
- Spread.
- Liquidity.
- Remaining time.

Strategy:

- Market regime.
- Multi-timeframe agreement.
- False-signal results.
- Signal.
- Confidence.
- Expected net value.
- Entry eligibility.
- NO TRADE reason.

Position:

- Direction.
- Entry price.
- Size.
- Unrealized P&L.
- TP.
- SL.
- Exit status.
- Residual shares.

System health:

- Primary feed.
- Secondary feed.
- Oracle status.
- CLOB connection.
- Heartbeat.
- Execution latency.
- Exposure.
- Reconciliation.
- Recovery state.

Analytics:

- Trades.
- Wins.
- Losses.
- Win rate.
- Net P&L.
- Expected value.
- Profit factor.
- Drawdown.
- Strategy comparisons.

Controls:

- Start/Stop.
- Emergency Stop.
- PAPER/LIVE status.
- Capital configuration.
- Risk configuration.
- Strategy configuration.
- Trade history.

Security:

- Bind to 127.0.0.1 by default.
- Authenticate control endpoints.
- Apply CSRF protection.
- Require typed LIVE confirmation.
- Never display wallet secrets.

Show actual backend state only.

# 35. GEOGRAPHIC AND WALLET SECURITY

Before LIVE activation:

- Call the official geographic restriction endpoint.
- Check eligibility.
- Check restricted and close-only status.
- Verify wallet authentication.
- Verify signature type.
- Verify funder address.
- Verify pUSD balance.
- Verify allowances.
- Verify reconciliation.

Never bypass restrictions.

Do not assume Bangladesh eligibility without live verification.

Use a dedicated wallet containing only the allocated trading capital where possible.

Never request a seed phrase through the application.

# 36. FEATURE MODES

Advanced features must support:

OFF
SHADOW
ACTIVE

OFF:
Feature disabled.

SHADOW:
Calculate and record decisions without affecting orders.

ACTIVE:
Feature may affect trading only after validation and authorization.

Protection features default to ACTIVE.

Experimental strategy enhancements default to SHADOW.

Correlation features default to SHADOW.

Feed failover defaults to BLOCK.

With all enhancements OFF, baseline behaviour must remain unchanged.

# 37. STRICT LIVE TRADING GATE

LIVE requires ALL conditions:

1. Explicit user activation.
2. Geographic eligibility passed.
3. Wallet authentication verified.
4. Correct signature type verified.
5. Funder address verified.
6. Sufficient pUSD balance.
7. Correct balance and allowance.
8. Clean order reconciliation.
9. Recovery state READY.
10. Healthy primary and required oracle feeds.
11. Verified market resolution rules.
12. User-approved rule fingerprints.
13. Resolution simulator ACTIVE.
14. Portfolio exposure monitor ACTIVE.
15. Disaster recovery ACTIVE.
16. Risk manager ACTIVE.
17. No unresolved protective shutdown.
18. Correct protocol and market version.
19. Minimum BUY feasibility verified.
20. Minimum SELL and exit feasibility verified.
21. Heartbeat safety configured and tested.
22. Statistical validation passed.
23. Explicit final user confirmation.

If ANY condition fails:

LIVE MUST REMAIN DISABLED.

Display the exact failed requirement.

Never silently enable LIVE.

After a restart, repeat recovery and all required safety checks.

# 38. REQUIRED AUTOMATED TESTS

Use mocked APIs only.

Never use real money for automated tests.

Test:

- Market discovery.
- Market lifecycle.
- 120-second cutoff.
- Market version detection.
- Correct asset IDs.
- pUSD.
- Balance and allowance.
- Minimum BUY.
- Minimum SELL.
- Partial SELL.
- Residual dust.
- Stop-loss feasibility.
- Take-profit feasibility.
- Dynamic position sizing.
- 2% risk.
- 4% daily loss.
- Fees.
- Taker delay.
- Oracle TWAP.
- Historical resolution versions.
- Resolution simulator.
- Probability.
- Expected value.
- Market regimes.
- False-signal filters.
- Multi-timeframe logic.
- No look-ahead bias.
- Correlation caps.
- Multiple-comparison correction.
- Parameter sensitivity.
- Order-book walking.
- Partial fills.
- Cancellation verification.
- Duplicate prevention.
- Exposure reservations.
- Heartbeat ID rotation.
- Heartbeat failure.
- Heartbeat expiry cancellation.
- Worker isolation.
- Recovery after heartbeat cancellation.
- Latency.
- Hybrid exits.
- Settlement.
- Redemption.
- Feed redundancy.
- Oracle mismatch.
- Application crashes.
- Restart recovery.
- Database corruption.
- Geographic restriction.
- LIVE activation gate.

Include fault injection at every critical order-journal transition.

# 39. DEVELOPMENT PHASES

Work strictly phase by phase.

After each phase, report actual results and STOP.

Do not proceed without explicit user approval.

## PHASE 0 — AUDIT AND PLAN

NO CODE CHANGES.

Inspect the actual repository and workspace.

If the repository is unavailable, state that clearly.

Verify official documentation for:

- Unified Python SDK and exact release.
- CLOB V2.
- pUSD.
- Market identifiers.
- Minimum BUY size.
- Minimum SELL size.
- Fee schedule.
- Taker delay.
- Heartbeat API.
- Heartbeat cancellation semantics.
- PolyBolt.
- TWAP.
- Geographic restrictions.
- Redemption.

Prepare:

- Architecture plan.
- Module mapping.
- Dependency list.
- Database plan.
- Testing plan.
- Capital compatibility report.
- Exit feasibility report.
- Heartbeat safety plan.
- Open technical decisions.
- Risk assessment.

Calculate whether configured capital can support a valid BUY and SELL under the 2% risk limit.

If the capital is too small, explain exactly why.

Do not change risk limits to make the trade possible.

STOP and wait for approval.

## PHASE 1 — DATA FOUNDATION

Implement:

- Market discovery.
- Market lifecycle.
- BTC feeds.
- Oracle feeds.
- PolyBolt integration.
- Data feed redundancy.
- Time synchronization.
- Historical data recording.
- Order-book snapshots.
- Database migrations.

Begin collecting data for ALL eligible markets, not only traded ones.

Run tests.

Report actual results.

STOP.

## PHASE 2 — ORACLE AND MARKET INTELLIGENCE

Implement:

- Resolution simulator.
- Historical rule-version separation.
- TWAP-aware probability foundations.
- Market regime detection.
- Multi-timeframe signals.
- False-signal detection.
- Correlation inputs.
- Validation infrastructure.

Run tests and report actual results.

STOP.

## PHASE 3 — PREDICTION AND STRATEGY

Implement:

- Probability model.
- Calibration.
- Dynamic entry.
- Entry timing.
- Expected value.
- Profit optimization.
- Capital compatibility.
- Dynamic sizing.
- Strategy variants.

Run historical and out-of-sample tests.

STOP.

## PHASE 4 — EXECUTION AND EXIT

Implement:

- Current official unified SDK integration.
- CLOB V2.
- pUSD.
- Fee calculation.
- Taker delay.
- Order execution.
- Smart limit pricing.
- Partial fills.
- SELL feasibility.
- Hybrid exits.
- Execution latency.
- Heartbeat API.

Use PAPER execution.

STOP.

## PHASE 5 — RISK AND CAPITAL PROTECTION

Implement:

- Risk manager.
- Exposure ledger.
- Daily loss shutdown.
- Profit protection.
- Drawdown.
- Cooldowns.
- Order reconciliation.
- Emergency stop.

Run tests.

STOP.

## PHASE 6 — DISASTER RECOVERY AND ANALYTICS

Implement:

- Order journal.
- Crash recovery.
- Restart recovery.
- Heartbeat-expiry recovery.
- Unknown-order resolution.
- Database recovery.
- Loss analysis.
- Strategy performance intelligence.
- Statistical validation.
- Automatic strategy disable.

Perform fault-injection tests.

STOP.

## PHASE 7 — DASHBOARD AND INTEGRATION

Implement the complete dashboard.

Integrate all backend systems.

Run the complete test suite.

Produce architecture and security reports.

STOP.

## PHASE 8 — PAPER VALIDATION AND LIVE GATE

Run realistic PAPER trading.

Collect the configured minimum sample, initially targeting 300 trades.

Evaluate:

- Net expectancy.
- Statistical confidence.
- Multiple-comparison correction.
- Parameter sensitivity.
- Fees.
- Slippage.
- Execution quality.
- SELL feasibility.
- Heartbeat reliability.
- Drawdown.
- Market regime coverage.
- Operational recovery.

Produce the complete validation report.

LIVE remains disabled unless every condition passes.

Require explicit user activation.

STOP and wait for approval.

# 40. FINAL DELIVERY

Provide:

1. Implemented features.
2. Repository structure.
3. Files changed.
4. Installation instructions.
5. Environment configuration.
6. Wallet setup.
7. pUSD requirements.
8. Dashboard instructions.
9. Windows startup instructions.
10. Actual automated test results.
11. Backtesting results.
12. Paper trading results.
13. Statistical validation.
14. Capital compatibility report.
15. BUY and SELL feasibility report.
16. Heartbeat reliability report.
17. Security report.
18. Known limitations.
19. LIVE activation checklist.

Never invent test results.

Never claim guaranteed profitability.

Never enable LIVE silently.

Never bypass any risk limit.

---

# FINAL OBJECTIVE

Build a complete autonomous Polymarket BTC 5-minute UP/DOWN trading system combining:

- Quantitative Prediction.
- Chainlink TWAP Intelligence.
- Adaptive Market Regimes.
- False Signal Detection.
- Multi-Timeframe Analysis.
- Dynamic Entry.
- Profit Optimization.
- Intelligent Hybrid Exit.
- BUY and SELL Feasibility.
- Advanced Execution.
- Portfolio Exposure.
- Capital Protection.
- Strategy Intelligence.
- Statistical Validation.
- Feed Redundancy.
- Heartbeat Protection.
- Disaster Recovery.

The system must identify genuine positive expected-value opportunities after all costs.

It must never force trades.

It must never increase risk to satisfy minimum order constraints.

It must never enter a position without a verified exit-feasibility plan.

When evidence is insufficient, the correct decision is NO TRADE.

PROTECT CAPITAL FIRST.

PURSUE SUSTAINABLE NET PROFIT SECOND.