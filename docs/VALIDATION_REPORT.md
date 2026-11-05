# VALIDATION REPORT

- Date: 2026-11-05
- Build: local Windows development build
- Python: 3.11.9 (`.venv`), `polymarket-client==0.16.0`
- Mode: PAPER only. LIVE is implemented but **not activated**.

---

## 1. What was validated

Validation was performed offline with mocked exchange APIs and a fixed clock. No live
connectivity, funded wallet, on-chain approval, production heartbeat, or real order was
used at any point.

Commands actually executed:

```bat
.venv\Scripts\python.exe -m pytest tests
.venv\Scripts\python.exe -m compileall -q btc5m_bot
.venv\Scripts\python.exe -m pip check
.venv\Scripts\python.exe -m btc5m_bot --help
.venv\Scripts\python.exe -m btc5m_bot status
```

Actual result of the final full test run:

```
137 passed, 0 failed, 0 errors, 0 skipped in 24.32s
```

The single warning is a `StarletteDeprecationWarning` emitted by FastAPI's own
`testclient` import (`httpx` compatibility), not by project code. `compileall` reported
no syntax errors and `pip check` reported no broken requirements.

The test count is a real local result, not a profitability or reliability claim. The
suite is deterministic (fixed clock, seeded RNG, fake adapter) and does not depend on
wall time or network access.

## 2. Coverage summary

| Area | Validated behaviour |
|---|---|
| Configuration | Hard limits (2% per trade, 4% daily), immutable risk settings, capital validation |
| Markets | Dynamic Gamma discovery, slug/epoch validation, V1/V2 id routing, rule fingerprinting and approval |
| Lifecycle | DISCOVERED → ACTIVE → ENTRY_CLOSED → RESOLVING → SETTLED, 120-second entry window |
| Data | Binance/Coinbase parsers, reconnect logic, composite feed health, staleness blocks |
| Oracle | 60-second Chainlink TWAP tracking, source canonicalisation, UNVERIFIED fail-closed state |
| Intelligence | Features, regimes, filters, multi-timeframe conflict, probability and EV |
| Sizing | Risk-first sizing under five limits; never raises risk to satisfy an exchange minimum |
| Execution | Paper fills, duplicate prevention, journal states, latency, reservations |
| Live result mapping | Matched BUY/SELL amounts, partial detection, delayed/live/unknown states never assumed filled |
| Exits | SELL feasibility, dust handling, hybrid exit precedence, dynamic TP, momentum reversal, liquidity deterioration |
| Risk | Daily loss, consecutive losses, cooldown, profit lock, kill switch, exposure ledger |
| Recovery | Startup reconciliation, ambiguous orders/positions block trading, reservation release |
| Heartbeat | Configuration validation, lease gating, failure accounting; never armed in tests |
| Gate | 23-condition LIVE gate cannot be bypassed by a passing report alone |
| Analytics | Parquet writers, DuckDB queries, retention, performance and loss reports |
| Dashboard | Loopback binding, PBKDF2 login, session/CSRF, typed confirmations, no secret exposure |
| Entry points | Engine/supervisor/CLI logging wiring, STOP file, Windows scripts present |

## 3. Defects found and fixed during validation

These were found by executing the code and reading the SDK contract, not by assumption.

1. **Lifecycle crash on transition.** The engine read non-existent
   `LifecycleTransition.to_state`/`from_state` attributes; the first DISCOVERED → ACTIVE
   transition would have crashed the loop. Fixed to use `old`/`new`.
2. **Logging signature mismatch.** Engine, supervisor and CLI called
   `setup_logging(component, path)` while the function takes `(logs_dir, *, component=...)`.
   Startup would have raised `TypeError`. All call sites corrected.
3. **Live fills discarded.** The adapter returned `filled_shares=0` for every accepted
   order, so a LIVE fill would never become a position. Added `map_accepted_order`, which
   converts `matched` maker/taker amounts into shares, average price and fee (BUY and SELL
   asset sides handled separately), while `delayed`/`live`/missing amounts remain
   unresolved.
4. **BUY market order used the wrong unit.** The SDK requires `amount` (collateral) for
   BUY and rejects `shares`; the adapter passed `shares`, so every LIVE BUY would have
   failed before reaching the exchange. BUY now sends `amount` plus `max_spend` capped at
   notional + modelled fee, and `max_price`; SELL sends `shares` and `min_price`.
5. **SDK validation errors were treated as unknown.** `UserInputError` raised before
   signing/posting is now `REJECTED` (`INVALID_ORDER`); only genuinely ambiguous transport
   failures become `UNKNOWN`.
6. **Reservation leak on FOK/FAK partials.** A partial market order left its exposure
   reservation active forever, double-counting exposure against the new position. Terminal
   market-order partials now release it; resting-order partials keep it.
7. **Terminal partials looked like working orders.** `open_orders` included FOK/FAK
   partials, which would block PAPER recovery after a legitimate partial fill. They are now
   excluded; GTC partials remain working orders.
8. **Runtime ambiguity was not fail-closed.** UNKNOWN/PENDING/ACKED orders were only
   reconciled at restart. The engine now blocks new entries for the market, blocks further
   SELLs for an unresolved exit (prevents overselling), defers settlement of the affected
   position, and records a protection event until reconciliation.
9. **Resolution mapping assumed index 0 = UP.** Payouts are now mapped through the market's
   own `outcomes` tuple, and unresolved or tied payouts return `None` instead of guessing.
10. **Book-snapshot test expectation** was corrected to account for the two-second snapshot
    rate limit (test-only fix).
11. **Three required hybrid-exit rules were missing or static.** A final audit against
    spec §23 found no momentum-reversal or liquidity-deterioration exit, and take-profit
    was a fixed threshold rather than dynamic. Take-profit now scales with probability and
    time remaining (`dynamic_take_profit`, `dynamic_tp_min_multiple`,
    `dynamic_tp_max_multiple`); momentum reversal and liquidity deterioration use
    conservative, configurable thresholds fed by the same feature snapshot as entries.
    All sit in the documented precedence after emergency/stop/trailing/take-profit.

## 4. What was NOT validated

These are explicit limitations of this build and must be handled before any LIVE attempt:

- No live connection to Gamma, CLOB, Data API, PolyBolt, the geo endpoint, or the
  heartbeat endpoint was made in this validation. Protocol behavior is implemented from
  the verified Phase 0 facts and the pinned SDK, but real responses can differ.
- No wallet, private key, allowance, deposit wallet, or real funds were used.
- The production heartbeat was never armed. The first accepted heartbeat irreversibly
  enables key-wide cancel-all-on-timeout.
- No profitability, win rate, or edge is claimed. The strategy defaults are unvalidated
  parameters; the statistical validation gate intentionally fails on an empty database.
- The exact minimum-order unit (shares vs notional) and live tick size remain resolved
  from live metadata at runtime, as recorded in `docs/PHASE0_REPORT.md`; re-verify them
  before LIVE.
- Live fee amounts may differ slightly from the schedule-derived fee recorded by the bot.
  Accounting is conservative and uses the published formula, but the exchange is the final
  authority.
- Runtime recovery for an ambiguous order requires a restart; there is no in-process
  operator console to resolve it while running.
- Partial exits are disabled for every position: the exit manager requires a feasible
  full-position SELL and otherwise holds to resolution (the spec-permitted fallback).
  The `partial_exits` feature flag therefore has no effect in this build.
- Correlation intelligence (ETH, broader crypto market) is not computed. The
  `correlation_shadow` flag records the spec-mandated SHADOW default and has no trading
  influence; enabling it requires additional feeds, trailing-window computation and
  out-of-sample validation first.
- The trailing stop is implemented but SHADOW by default; it only acts when the
  `trailing_stop` feature is explicitly set to ACTIVE.

## 5. Safety state of this build

- Dashboard binds `127.0.0.1` only and the engine refuses a non-loopback host.
- LIVE remains locked: the requested mode may be LIVE, but the effective mode stays PAPER
  until the 23-condition gate passes **and** a stored activation exists.
- All hard risk limits are enforced independently of strategy/learning code.
- The app never submits allowance approvals and never requests unlimited allowances.
- Private keys are read from environment variables only; logs and API responses redact
  secrets.
- `status` on the local database currently reports `mode: PAPER`, `live_activation: false`,
  `heartbeat.armed: false`, zero trades.

**No live trading was performed. LIVE is not activated.**
