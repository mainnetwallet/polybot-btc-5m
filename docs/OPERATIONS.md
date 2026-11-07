# POLYBOT operations

This is the operator runbook. It assumes Windows and the project layout created by
`setup.bat`. No command in this document enables LIVE by itself.

## Layout

| Path | Purpose |
|---|---|
| `.env` | secrets and infrastructure settings; never commit |
| `config\runtime.json` | persisted trading settings; starts in PAPER |
| `data\polybot.db` | SQLite state (WAL); not used when `POLYBOT_DATABASE_URL` points at Postgres |
| `data\polybot.lock` | single-instance lock |
| `data\STOP` | graceful shutdown request |
| `data\supervisor.json` | supervisor status snapshot |
| `data\backups\` | consistent SQLite backups |
| `data\parquet\` | BTC ticks, TWAP, books, features, fills |
| `logs\engine.jsonl` | engine log, JSON lines, rotated 20 MB × 10 |
| `logs\supervisor.jsonl` | supervisor log |
| `logs\heartbeat.jsonl` | heartbeat worker log (LIVE configuration only) |

## Daily operation

```bat
start.bat        :: supervised engine + dashboard
status.bat       :: JSON status (mode, recovery, risk, position, orders, trades)
stop.bat         :: write STOP; supervisor stops heartbeat then engine
```

The supervisor checks `data\STOP` every two seconds. If a child exits unexpectedly it is
restarted; more than `supervisor_max_restarts_per_hour` (default 5) in one hour trips
crash-loop protection and stops the supervisor. The engine lease becomes stale on restart,
so the heartbeat worker cannot send until the new engine renews it.

Dashboard: <http://127.0.0.1:8765>. The engine refuses to bind a non-loopback host.
`GET /api/health` is unauthenticated; it is reachable only from loopback unless the Render dashboard proxy below is on.

## Health endpoints (Render, UptimeRobot)

The dashboard itself always binds loopback. For hosting, the supervisor can run one extra, tiny
listener (`btc5m_bot/healthserver.py`, a daemon thread, stdlib only). It is off unless
`POLYBOT_HEALTH_PORT` is set, or the process runs on Render (`RENDER` is set and Render injects
`PORT`). Its own endpoints answer with fixed status codes and short reason codes: no trading
state, no secrets, no internal error text. If it cannot bind its port it logs an error and the
bot keeps running.

**Dashboard proxy.** With `POLYBOT_PUBLIC_DASHBOARD=true`, or automatically on Render when a
dashboard password hash is set (`false` disables it), the listener relays every path except
`/healthz` and `/health` (HTTP and the `/api/stream` WebSocket) to the loopback dashboard. The
proxy adds no authentication of its own: the dashboard's login, session cookie, CSRF check and
lockout apply unchanged, and it is never enabled without a password hash. In this mode `/`
belongs to the dashboard; use `/healthz` for liveness. If the engine's dashboard is not up yet
the proxy answers 502 `dashboard_unavailable`.

| Path | Meaning | Status |
|---|---|---|
| `/healthz` (also `/` unless the dashboard proxy is on) | liveness: the supervisor process is serving | always 200 |
| `/health` | readiness: supervisor `RUNNING`, engine process alive, engine lease fresh | 200, otherwise 503 with `reason` |

`reason` is one of `supervisor_not_running`, `supervisor_stalled`, `supervisor_unavailable`,
`engine_process_down`, `engine_lease_missing`, `engine_lease_stale`, `engine_lease_invalid`,
`engine_unhealthy`, `lease_unavailable`. The lease is considered stale after
`POLYBOT_HEALTH_MAX_LEASE_AGE_SECONDS` (default 60). Results are cached for 2 seconds.

Use `/healthz` for Render's `healthCheckPath` (Render may restart a service whose health check
keeps failing, and a trading bot should only restart on purpose) and `/health` for UptimeRobot.
`/health` reports **outages**. A bot that is up but deliberately not trading (`NO TRADE`: a too-small
trade size, unhealthy feeds) still returns 200; read those in the **Decisions** tab or
with `status`.

A leftover `data\STOP` file from a previous run is removed when the supervisor starts (after it
takes the single-instance lock), so a restart after `stop` does not exit immediately.

## Reading state

`status.bat` returns JSON. The dashboard exposes the same data plus decisions, orders,
positions, trades, rules, resolutions, analytics, and the 24-condition gate.

Key fields:

- `mode` — requested mode from `runtime.json`
- `live_activation` — whether a persisted LIVE activation exists
- `recovery.state` — `READY` or `BLOCKED`; a blocked recovery prevents entries
- `risk.kill_switch`, `risk.cooldown_until`, `risk.consecutive_losses`
- `open_position`, `open_orders`, `paper_trades`, `live_trades`
- `heartbeat.armed`, `heartbeat.consecutive_failures`

Decision reason codes explain every NO TRADE. This table lists every `ReasonCode` (a test fails
if one is missing):

| Code | Meaning |
|---|---|
| `OK` | the step succeeded (used on activity-log entries such as fills; not a block) |
| `TRADING_OFF` | the master trading switch is OFF (the default after a fresh start or restart); no new entries until you turn it ON in Settings. Open positions are still managed |
| `RECOVERY_NOT_READY` | startup reconciliation has not passed |
| `FEED_UNHEALTHY` | BTC feed or Chainlink TWAP health is not fully healthy |
| `STALE_DATA` / `STALE_BOOK` | data/book exceeds age limits |
| `RULES_UNVERIFIED`, `RULES_UNAPPROVED` | no longer emitted: resolution rules are approved automatically and never block a trade |
| `MARKET_NOT_ACTIVE` | the market has not started yet, so no entry is possible |
| `MARKET_NOT_VALIDATED` | market metadata is missing (for an open position: exit management is skipped until it is back) |
| `ENTRY_WINDOW_CLOSED` | past the 150-second entry cutoff |
| `ENTRY_ALREADY_USED` | the market's entry cap (3) is used, or a further entry was refused: it must be the same side as the held position and only while that position is in profit |
| `ACTIVE_POSITION` | the maximum number of open positions (2) is already reached |
| `ORACLE_DATA_INSUFFICIENT` | features or probability could not be computed (not enough oracle/BTC data) |
| `NO_SIGNAL` | the strategy produced no directional signal |
| `LOW_PROBABILITY` | model probability below threshold or not confident enough |
| `MTF_CONFLICT` | multi-timeframe signals disagree |
| `REGIME_BLOCKED` | volatility/trend regime disallows entries |
| `FILTER_FAILED` | an entry filter (for example spread or depth) rejected the trade |
| `FILTER_UNAVAILABLE` | a filter could not be evaluated, so the entry fails closed |
| `SPREAD_TOO_WIDE` | spread exceeds limit |
| `INSUFFICIENT_DEPTH` | the ask side does not have enough depth for the order (or no ask liquidity); says nothing about EV |
| `SLIPPAGE_TOO_HIGH` | expected fill price is worse than `max_slippage` allows, or the limit price would sit below the best ask; says nothing about EV |
| `NEGATIVE_EV` | the trade loses money in expectation after fees, slippage and the fee buffer |
| `INSUFFICIENT_EDGE` | expected value is positive but the net edge is below `min_net_edge` |
| `FEES_UNVERIFIED` | the market metadata has no fee schedule, so costs cannot be computed |
| `BELOW_MIN_ORDER` | the trade size is below the minimum order value (default $1); never upsized |
| `TRADE_SIZE_NOT_SET` | no USDC trade size is set, so the bot places no trades; set it in Settings |
| `RISK_LIMIT` | planned loss is not positive, or an unresolved working order exists |
| `INSUFFICIENT_BALANCE` | balance unavailable, or the planned cost exceeds the available balance |
| `LATENCY_BREACH` | signal-to-order or order-to-result latency p95 is above `max_latency_p95_ms` |
| `ACK_OUTSIDE_WINDOW` | too little time left to verify an order acknowledgement inside the entry window |
| `NO_FILL` | the entry order was submitted but did not fill |
| `SELL_NOT_FEASIBLE` | the position could not be exited with sufficient depth |
| `COOLDOWN`, `CONSECUTIVE_LOSS_LIMIT` | no longer emitted: the loss-streak cooldown was removed |
| `DAILY_LOSS_LIMIT`, `PROFIT_LOCK`, `KILL_SWITCH` | protective stops |
| `DUPLICATE_ORDER` | an order with the same idempotency key already exists. A failed exit is retried under a new attempt number; a dropped exit is logged as the `EXIT_DUPLICATE_ORDER` protection event |
| `SYSTEM_BLOCKED` | reserved: defined, not emitted by the current engine |
| `NOT_ARMED` | reserved: defined, not emitted by the current engine |
| `MARKET_VERSION_UNSUPPORTED` | reserved: defined, not emitted by the current engine |
| `ORACLE_UNHEALTHY` | reserved: oracle health is reported as `FEED_UNHEALTHY` |
| `ORACLE_UNVERIFIED` | reserved: defined, not emitted by the current engine |
| `GEO_BLOCKED` | reserved: defined, not emitted by the current engine (geoblock is checked by the LIVE gate) |
| `LIVE_GATE_FAILED` | reserved: defined, not emitted by the current engine (gate results are shown on the Go live page) |
| `UNKNOWN_ERROR` | reserved: defined, not emitted by the current engine |
| `CLOCK_UNSYNCED` | CLOB server time is unavailable or outside the allowed drift bound; LIVE entries are blocked |

## First PAPER run

1. Configure CLOB credentials. PolyBolt needs them; without them the oracle is
   `UNVERIFIED`, feed health blocks entries, and the bot records NO TRADE while still
   collecting public BTC/book data.
2. Let discovery run. The bot stores live Gamma/CLOB market metadata and a rule
   fingerprint. Rules are approved automatically; they never block a trade.
3. (Optional) Read the rule text in the dashboard **Rules** view to see what each market resolves on.
4. Set `trade_size_usd` (USDC per buy, minimum $1) in the dashboard or `config\runtime.json`. It has no
   default: until it is set the bot places no trades (`TRADE_SIZE_NOT_SET`). The daily loss cap is a fixed USDC amount (`daily_loss_limit_usd`).
5. Do not judge the strategy from a handful of trades. The statistical validation gate
   requires real recorded outcomes; it fails honestly on an empty database.

## Configuration changes

Only allowlisted fields are writable through `POST /api/config`. Hard limits, position
count above 2, entries per market above 3, and acknowledgement latency
above 3000 ms raise `HardLimitError`. Feature flags cannot disable risk protections.

Changing mode to LIVE in `runtime.json` is not enough to trade live. See
`docs/SECURITY.md` and `gate.py`.

## Kill switch and daily loss limit

- Dashboard **KILL** requires the typed phrase `KILL`; entries stop immediately.
- **CLEAR KILL** requires `CLEAR KILL`. Clearing a kill does not erase the consecutive-loss count.
- There is no loss-streak cooldown: losses in a row never pause the bot. The daily loss limit
  (`daily_loss_limit_usd`) is the loss guard and locks trading for the day once reached.
- **RESET DAY** requires `RESET DAY`; it resets the daily P&L window but preserves the
  kill switch.

## Backup and restore

With `POLYBOT_DATABASE_URL` set (hosted Postgres) the provider's snapshots / point-in-time restore are
the backup, and `backup` prints a note instead of writing a file. The rest of this section is for the
local SQLite file. Backups use SQLite `VACUUM INTO`, so they are consistent without stopping writes. They are
created every six hours while the engine runs, and on demand:

```bat
.venv\Scripts\python.exe -m btc5m_bot backup
```

Restore procedure (destructive — confirm you have the right file):

1. Stop the bot and wait for both processes to exit.
2. Preserve the current database:
   ```bat
   move data\polybot.db data\polybot.db.before-restore
   ```
3. Remove stale sidecar files `data\polybot.db-wal` and `data\polybot.db-shm` if present.
4. Copy the chosen backup to `data\polybot.db`.
5. Verify integrity before starting:
   ```bat
   .venv\Scripts\python.exe -c "import sqlite3; c=sqlite3.connect(r'data\polybot.db'); print(c.execute('PRAGMA integrity_check').fetchone()[0])"
   ```
6. Start the bot. Recovery will reconcile against the exchange. If anything is ambiguous,
   recovery stays BLOCKED and no entries occur.

Never delete the current database or backups until you have verified the restore.

## Recovery scenarios

### Database integrity failure

Startup runs `PRAGMA integrity_check`. Any failure blocks recovery. Restore from a verified
backup as above; do not bypass the check.

### Ambiguous order or unknown exchange result

The order journal records intent before submission. On restart:

- `INTENDED` (never submitted) is rejected locally and its reservation released.
- `SUBMITTING` without an exchange order id blocks until a human verifies the exchange.
- An untracked exchange working order blocks.
- A position that cannot be matched to local state blocks.

The engine never guesses an order into a terminal state. Resolve on the exchange, then
restart. Do not manually delete rows to clear a block unless you fully understand the
consequence; the journal is the audit trail.

An ambiguous order that occurs while the engine is running also fails closed immediately:
new entries for that market stop, no further SELL is submitted (to avoid overselling),
and settlement of the affected position is deferred until reconciliation. Restart the
engine to run recovery, which will reconcile against the exchange or stay BLOCKED.

### Feed or oracle failure

Any non-HEALTHY leg blocks new entries; failover is not silently enabled. If Binance and
Coinbase diverge by more than 25 bps, entries stop. If the TWAP is stale, unverified, or
from a non-Chainlink source, entries stop. Positions are still managed by the exit engine.

### Heartbeat failures

After three consecutive heartbeat failures the runtime state records the failure and the
gate/protective checks fail. The heartbeat is key-wide: a timeout cancels all orders for
the API key. Investigate the network and credentials before restarting. Never arm the
heartbeat for testing outside an authorized LIVE activation.

After a LIVE position settles, the engine scans for the matching venue position marked `redeemable`, submits one redemption, waits for the SDK transaction confirmation, and records the transaction hash. An ambiguous submission is quarantined as `UNKNOWN` and is not retried automatically; reconcile it on-chain before changing that state.

The validation report now separates the last 30% of recorded PAPER close trades as a chronological holdout (minimum 20 observations) and requires positive holdout PnL plus a positive moving-block-bootstrap lower bound. This is not a historical tick replay and does not prove the data was untouched by prior strategy tuning.

The engine synchronizes its market clock against the CLOB `/time` endpoint every 15 seconds while LIVE. A slow response or local clock drift beyond five seconds blocks LIVE entry and fails the activation gate. The engine also reads market `trading.secondsDelay` and requires enough entry-window time for that delay plus acknowledgement latency; malformed delay metadata blocks entry.

The worker re-checks the whole heartbeat safety configuration (enabled flag, credentials,
dedicated-key confirmation, interval and lease window, recorded arm test, LIVE activation)
before **every** send, not only at start-up. If any check fails it skips the send and records
the reason under `heartbeat_skipped`; the platform deadline then cancels resting orders, the
same fail-safe as a revoked LIVE session.

### Crash loop

More than five child restarts in an hour stops the supervisor. Inspect `logs\` and
`data\supervisor.json`, fix the cause, and start again. The lease mechanism prevents the
heartbeat from sending during a restart gap.

### Disk full / permission errors

Discovery failures, backup failures, and record failures are logged and raise protection
events. Trading remains subject to feed/risk gates. Free disk space and verify that the
service account can write `data\` and `logs\`.

## Logs

Logs are JSON lines with a timestamp, level, logger, message, optional exception, and
structured fields. Secrets are redacted by `security.redact`. Rotated files are kept as
`engine.jsonl.1` … `.10`.

Do not attach `.env`, private keys, seed phrases, API secrets, or full database dumps to
support tickets or chats.

## LIVE activation checklist (do not skip)

1. Read `docs/SECURITY.md`.
2. Dedicated wallet and dedicated API key only for POLYBOT.
3. CLOB credentials verified; no VPN/proxy; live geo check returns eligible.
4. Correct signature type and funder address.
5. Trade size set; balance and allowance cover at least one trade.
6. (Rule approval is automatic; nothing to do.)
7. Resolution simulator, exposure monitor, risk manager, and backups active.
8. Heartbeat configured with `POLYBOT_HEARTBEAT_DEDICATED_KEY_CONFIRMED=true` and the
   one-way arm test explicitly recorded.
9. Statistical validation passed on real recorded outcomes (the gate enforces this).
10. Run `activate-live` and type `ACTIVATE LIVE TRADING`.

Any missing condition keeps LIVE locked and the engine in PAPER. Revoke with
`revoke-live` (typed phrase `REVOKE LIVE`), which also requests cancel-all.
