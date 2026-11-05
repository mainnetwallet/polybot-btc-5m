# POLYBOT operations

This is the operator runbook. It assumes Windows and the project layout created by
`setup.bat`. No command in this document enables LIVE by itself.

## Layout

| Path | Purpose |
|---|---|
| `.env` | secrets and infrastructure settings; never commit |
| `config\runtime.json` | persisted trading settings; starts in PAPER |
| `data\polybot.db` | SQLite state (WAL) |
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
`GET /api/health` is unauthenticated but reachable only from loopback.

## Health endpoints (Render, UptimeRobot)

The dashboard is never exposed. For hosting, the supervisor can run one extra, tiny listener
(`btc5m_bot/healthserver.py`, a daemon thread, stdlib only). It is off unless `POLYBOT_HEALTH_PORT`
is set, or the process runs on Render (`RENDER` is set and Render injects `PORT`). It answers
`GET` and `HEAD` only and returns fixed status codes and short reason codes: no trading state, no
secrets, no internal error text. If it cannot bind its port it logs an error and the bot keeps
running.

| Path | Meaning | Status |
|---|---|---|
| `/healthz` (also `/`) | liveness: the supervisor process is serving | always 200 |
| `/health` | readiness: supervisor `RUNNING`, engine process alive, engine lease fresh | 200, otherwise 503 with `reason` |

`reason` is one of `supervisor_not_running`, `supervisor_stalled`, `supervisor_unavailable`,
`engine_process_down`, `engine_lease_missing`, `engine_lease_stale`, `engine_lease_invalid`,
`engine_unhealthy`, `lease_unavailable`. The lease is considered stale after
`POLYBOT_HEALTH_MAX_LEASE_AGE_SECONDS` (default 60). Results are cached for 2 seconds.

Use `/healthz` for Render's `healthCheckPath` (Render may restart a service whose health check
keeps failing, and a trading bot should only restart on purpose) and `/health` for UptimeRobot.
`/health` reports **outages**. A bot that is up but deliberately not trading (`NO TRADE`: unapproved
rule, unset capital, unhealthy feeds) still returns 200; read those in the **Decisions** tab or
with `status`.

A leftover `data\STOP` file from a previous run is removed when the supervisor starts (after it
takes the single-instance lock), so a restart after `stop` does not exit immediately.

## Reading state

`status.bat` returns JSON. The dashboard exposes the same data plus decisions, orders,
positions, trades, rules, resolutions, analytics, and the 23-condition gate.

Key fields:

- `mode` — requested mode from `runtime.json`
- `live_activation` — whether a persisted LIVE activation exists
- `recovery.state` — `READY` or `BLOCKED`; a blocked recovery prevents entries
- `risk.kill_switch`, `risk.cooldown_until`, `risk.consecutive_losses`
- `open_position`, `open_orders`, `paper_trades`, `live_trades`
- `heartbeat.armed`, `heartbeat.consecutive_failures`

Decision reason codes explain every NO TRADE. Common ones:

| Code | Meaning |
|---|---|
| `RECOVERY_NOT_READY` | startup reconciliation has not passed |
| `FEED_UNHEALTHY` | BTC feed or Chainlink TWAP health is not fully healthy |
| `RULES_UNVERIFIED` | rule not registered, methodology unknown, or confidence too low |
| `RULES_UNAPPROVED` | rule fingerprint awaits operator approval |
| `CAPITAL_UNSET` | allocated capital is not configured |
| `STALE_DATA` / `STALE_BOOK` | data/book exceeds age limits |
| `LOW_PROBABILITY` | model probability below threshold or not confident enough |
| `REGIME_BLOCKED` | volatility/trend regime disallows entries |
| `SPREAD_TOO_WIDE` | spread exceeds limit |
| `INSUFFICIENT_EDGE` / `NEGATIVE_EV` | fees/slippage/fee buffer leave no positive EV |
| `SELL_NOT_FEASIBLE` | the position could not be exited with sufficient depth |
| `BELOW_MIN_ORDER` | risk-based size is below the exchange minimum; never upsized |
| `ENTRY_WINDOW_CLOSED` | past the hard 120-second entry cutoff |
| `COOLDOWN` | after two consecutive losses |
| `DAILY_LOSS_LIMIT`, `PROFIT_LOCK`, `KILL_SWITCH` | protective stops |

## First PAPER run

1. Configure CLOB credentials. PolyBolt needs them; without them the oracle is
   `UNVERIFIED`, feed health blocks entries, and the bot records NO TRADE while still
   collecting public BTC/book data.
2. Let discovery run. The bot stores live Gamma/CLOB market metadata and a rule
   fingerprint. Unknown or ambiguous methodologies stay blocked.
3. Review the rule text in the dashboard **Rules** view. Approve only what you verified
   against the live market metadata:
   ```bat
   .venv\Scripts\python.exe -m btc5m_bot approve-rule <fingerprint>
   ```
4. Set `capital_usd` in the dashboard or `config\runtime.json`. This is the allocated
   capital, not the wallet balance. It is the basis for the 2% planned-loss cap and the
   4% daily-loss cap.
5. Do not judge the strategy from a handful of trades. The statistical validation gate
   requires real recorded outcomes; it fails honestly on an empty database.

## Configuration changes

Only allowlisted fields are writable through `POST /api/config`. Hard limits, position
count, entries per market, entry window above 120 seconds, and acknowledgement latency
above 3000 ms raise `HardLimitError`. Feature flags cannot disable risk protections.

Changing mode to LIVE in `runtime.json` is not enough to trade live. See
`docs/SECURITY.md` and `gate.py`.

## Kill switch and cooldown

- Dashboard **KILL** requires the typed phrase `KILL`; entries stop immediately.
- **CLEAR KILL** requires `CLEAR KILL`. Clearing a kill does not erase a cooldown or the
  consecutive-loss count.
- Two consecutive losses trigger a 15-minute cooldown. Entry resumes only after the
  cooldown and when all other gates pass.
- **RESET DAY** requires `RESET DAY`; it resets the daily P&L window but preserves the
  kill switch and cooldown.

## Backup and restore

Backups use SQLite `VACUUM INTO`, so they are consistent without stopping writes. They are
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
5. Capital configured; balance and allowance meet the capital requirement.
6. At least one verified rule fingerprint approved.
7. Resolution simulator, exposure monitor, risk manager, and backups active.
8. Heartbeat configured with `POLYBOT_HEARTBEAT_DEDICATED_KEY_CONFIRMED=true` and the
   one-way arm test explicitly recorded.
9. Statistical validation passed on real recorded outcomes (the gate enforces this).
10. Run `activate-live` and type `ACTIVATE LIVE TRADING`.

Any missing condition keeps LIVE locked and the engine in PAPER. Revoke with
`revoke-live` (typed phrase `REVOKE LIVE`), which also requests cancel-all.
