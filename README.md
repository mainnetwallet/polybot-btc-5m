# POLYBOT

Autonomous **Polymarket BTC 5-minute UP/DOWN** research and trading system for Windows.
PAPER is the default. The complete LIVE path exists but stays locked until the operator
passes a strict 23-condition gate and types an explicit confirmation phrase.

> **No profitability is promised or implied.** The system is designed to prefer
> **NO TRADE** over a bad trade. A passing test suite proves code behavior, not edge.

---

## Safety invariants (not configurable)

| Limit | Value |
|---|---|
| Planned loss per trade | max **2%** of allocated capital, assuming a full loss even with a stop |
| Daily loss limit | max **4%** |
| Consecutive losses before cooldown | max **2**, then a 15-minute cooldown |
| Concurrent positions | **1** |
| Entries per market | **1** |
| Entry window | **120 seconds** from market start |
| Martingale / averaging down / auto risk increase | **never implemented** |
| Raising size to meet exchange minimums | **never** — returns `BELOW_MIN_ORDER` |
| Resolution source | **Chainlink only**; Pyth/spot never substituted |
| Unknown resolution rules | **NO TRADE** |
| LIVE gate | **23 conditions**, all must pass, plus typed phrase |

The hard limits are enforced by `RuntimeConfig` validators, not just documentation.
Changing them raises `HardLimitError`. The dashboard cannot write them.

---

## Requirements

- Windows 10/11, Python **3.11+**
- A dedicated Polymarket wallet/API key for any authenticated use
- CLOB L2 credentials for PolyBolt Chainlink TWAP (required even in PAPER to verify the oracle)
- No VPN or proxy; geography is checked live by the exchange adapter and the gate

---

## Complete setup guide (step by step)

This walks you from a fresh Windows PC to a running PAPER bot. Nothing here enables LIVE trading.

### 0. Prerequisites

- Windows 10/11
- Python **3.11+** from [python.org](https://www.python.org/downloads/) (tick **Add python.exe to PATH** in the installer)
- Git

### 1. Clone the repository

```bat
git clone https://github.com/mainnetwallet/polybot-btc-5m.git
cd polybot-btc-5m
```

If the repository is private, authenticate with `gh auth login` or a Git credential manager.
Never paste a personal access token into a chat, an issue, or a commit.

### 2. Run setup

```bat
setup.bat
```

This creates `.venv`, installs dependencies, copies `.env.example` to `.env`, and initialises
the SQLite database and `config\runtime.json` (mode `PAPER`).

### 3. Create dashboard credentials

```bat
.venv\Scripts\python.exe -m btc5m_bot hash-password
```

Paste the two generated lines into `.env`:

```text
POLYBOT_DASHBOARD_PASSWORD_HASH=...
POLYBOT_SESSION_SECRET=...
```

You log in with username `admin` (`POLYBOT_DASHBOARD_USERNAME`) and the password you typed
when generating the hash.

### 4. Configure `.env`

| Variable | Needed in PAPER? | Notes |
|---|---|---|
| `POLYBOT_DASHBOARD_PASSWORD_HASH`, `POLYBOT_SESSION_SECRET` | Yes | From step 3 |
| `POLYBOT_PRIVATE_KEY` **or** the three `POLYBOT_CLOB_API_*` values | Only to verify the Chainlink oracle | Without them the oracle is `UNVERIFIED` and the bot records NO TRADE while still collecting data |
| `POLYBOT_SIGNATURE_TYPE`, `POLYBOT_FUNDER_ADDRESS` | Only for LIVE | Leave defaults for PAPER |
| `POLYBOT_HEARTBEAT_ENABLED` | No | Keep `false`. Never arm the heartbeat during development |
| `POLYBOT_HEARTBEAT_DEDICATED_KEY_CONFIRMED` | No | Keep `false` |
| Endpoint URLs (`POLYBOT_GAMMA_URL`, ...) | No | Leave as in `.env.example` |

If you supply a private key, use a **dedicated wallet created only for this bot**, holding no
funds while you are in PAPER. Never use your main wallet's key.

### 5. Set allocated capital

Edit `config\runtime.json` and replace `"capital_usd": null` with a number, for example:

```json
"capital_usd": 100
```

This is the capital the risk limits are computed from (2% planned loss per trade, 4% daily
loss), not your wallet balance. If it is unset the bot records `CAPITAL_UNSET` and does not trade.

### 6. Start the bot

```bat
start.bat
```

Open <http://127.0.0.1:8765> and log in. Leave the window open; press `Ctrl+C` or run
`stop.bat` from another terminal to shut down gracefully.

### 7. Approve the resolution rule

Open the **Rules** tab, compare each fingerprint's rule text with the live market metadata,
and approve only what you have verified:

```bat
.venv\Scripts\python.exe -m btc5m_bot approve-rule <fingerprint>
```

Until a rule is approved the bot records `RULES_UNAPPROVED` and does not trade.

### 8. Day-to-day commands

```bat
status.bat   :: mode, recovery, risk, open position, trade counts (JSON)
stop.bat     :: graceful shutdown
.venv\Scripts\python.exe -m btc5m_bot backup   :: on-demand database backup
```

Logs are in `logs\`, data in `data\`. If the bot is not trading, check the **Decisions** tab:
every NO TRADE has a reason code, listed in [docs/OPERATIONS.md](docs/OPERATIONS.md).

### 9. Before thinking about LIVE

Run in PAPER until you have enough recorded outcomes to judge the strategy. LIVE stays locked
until the 23-condition gate passes and you type the activation phrase. See
[Modes and LIVE activation](#modes-and-live-activation) and [docs/SECURITY.md](docs/SECURITY.md).

---

## Quick start (Windows)

```bat
setup.bat
```

`setup.bat` creates `.venv`, installs dependencies, copies `.env.example` to `.env`,
and initialises the SQLite database plus `config\runtime.json`.

Create dashboard credentials:

```bat
.venv\Scripts\python.exe -m btc5m_bot hash-password
```

Paste the two generated lines into `.env`:

```text
POLYBOT_DASHBOARD_PASSWORD_HASH=...
POLYBOT_SESSION_SECRET=...
```

Start the supervised engine and dashboard:

```bat
start.bat
```

Open <http://127.0.0.1:8765>, log in, and inspect **State**, **Markets**, **Rules**,
**Decisions**, **Paper trades**, and **Gate**.

Stop gracefully:

```bat
stop.bat
```

The supervisor writes `data\STOP`, shuts the heartbeat worker down first, then the engine.
`status.bat` prints mode, recovery, risk, open position/orders, and trade counts as JSON.

---

## First-run workflow

1. Run in PAPER and let it collect data. Without CLOB credentials the oracle is
   `UNVERIFIED`, feed health blocks entries, and the bot records NO TRADE — by design.
2. Open **Rules** and review each resolution-rule fingerprint. Approve only the rule
   text you verified against the live market metadata:
   ```bat
   .venv\Scripts\python.exe -m btc5m_bot approve-rule <fingerprint>
   ```
   Approval is recorded with an audit entry. Unknown or ambiguous rules stay blocked.
3. Configure allocated capital in the dashboard or `config\runtime.json`.
   Position size is derived from `capital × 2% / worst-case unit cost`; it is never
   increased to satisfy the minimum order size.
4. Leave the bot PAPER until you have enough real, recorded outcomes to judge it.
   No performance claim is made here.

---

## Modes and LIVE activation

`config\runtime.json` contains `"mode": "PAPER"` by default.

A requested LIVE mode is **not sufficient**. On every iteration the engine resolves the
effective mode to LIVE only if **all** of the following hold:

1. `mode` is `LIVE`
2. an active, persisted LIVE activation exists
3. the latest 23-condition gate report passed

Otherwise it silently runs PAPER and writes `live_locked=true` to state.

Activation command:

```bat
.venv\Scripts\python.exe -m btc5m_bot activate-live
```

The CLI prompts for `ACTIVATE LIVE TRADING`, evaluates the gate against live state, and
persists the activation only if every condition passes. The dashboard exposes the same
gate and phrase flow. Revoke with:

```bat
.venv\Scripts\python.exe -m btc5m_bot revoke-live
```

Revocation writes an audit entry, switches mode back to PAPER, and requests cancel-all.

### Heartbeat warning

Polymarket heartbeat expiry performs a **cancel-all for the entire API key**. The first
accepted heartbeat **arms this permanently**; there is no disarm endpoint. Therefore:

- heartbeat is disabled by default (`POLYBOT_HEARTBEAT_ENABLED=false`)
- it never sends unless LIVE is active, the engine lease is fresh, and the gate passed
- use a dedicated key used by **no other software**
- set `POLYBOT_HEARTBEAT_DEDICATED_KEY_CONFIRMED=true` only when that is true
- the gate requires a recorded heartbeat test; the operator must explicitly arm it

Never arm the heartbeat during development.

---

## Architecture

```text
btc5m_bot/
  cli.py, engine.py, supervisor.py   entry points, loop, crash supervision
  config.py                          immutable limits, runtime config, secrets
  gate.py                           23-condition LIVE gate
  recovery.py                        exchange-as-truth startup reconciliation
  heartbeat/                         lease, worker, one-way heartbeat safety
  markets/                           Gamma discovery, lifecycle, rule parsing
  exchange/                          SDK isolation, fees, fake adapter
  data/                              Binance/Coinbase feeds, health, Parquet, BTC series
  oracle/                            Chainlink 60s TWAP tracker + resolution simulator
  intelligence/                      features, regimes, filters, probability, strategies, EV
  sizing.py                          risk-based size and min-order compatibility
  execution/                         journal, FOK/FAK execution, paper broker, feasibility
  exits.py                            stop/take-profit/time/probability/momentum/liquidity/emergency exits
  risk/                              exposure ledger and protections
  analytics/                         performance, calibration, statistical validation
  api/, security.py, web/            authenticated loopback dashboard
  db/                                SQLAlchemy models, WAL engine, repository
```

### Trading loop

Each `_iteration()` refreshes mode, optionally refreshes the gate, discovers markets,
ticks lifecycle, refreshes balance/books/feed health/features/probability, records data,
manages exits, evaluates entry, settles expired windows, backs up, refreshes analytics,
and publishes state. Entry is fail-closed at every stage: recovery, feed health, rule
verification/approval, risk, data age, book age, probability, regime, MTF conflict,
spread, fee verification, depth, EV, SELL feasibility, filters, latency, and
acknowledgement window.

### Execution

- Write-ahead order journal with idempotency keys
- FOK/FAK book walking with slippage, fees, and partial-fill accounting
- Exchange `UNKNOWN` results are never blindly retried
- Partial fills and DUST positions are held to resolution when selling is infeasible
- Every entry and exit decision is logged with a reason code

### Data

SQLite (WAL, foreign keys, busy timeout) stores markets, windows, orders, positions,
reservations, risk days/state, protections, heartbeat/recovery state, rules, resolutions,
decisions, latency, trades, activations, audit, and equity snapshots.

Parquet datasets under `data\parquet\<dataset>\date=YYYY-MM-DD\`:

`btc_ticks`, `oracle_twap`, `book_snapshots`, `market_snapshots`, `signal_features`,
`paper_fills`

`ParquetRecorder.query()` exposes read-only DuckDB SQL over a dataset. Retention is
30 days by default. Backups are written to `data\backups\` every 6 hours and on demand:

```bat
.venv\Scripts\python.exe -m btc5m_bot backup
```

---

## Configuration

- **Secrets and infrastructure:** `.env` (never committed)
- **Runtime trading settings:** `config\runtime.json` via the dashboard/CLI/ConfigStore
- **Reference defaults:** `config.example.json`

Only allowlisted runtime fields are writable through the dashboard. Risk hard limits,
kill switches, exposure controls, daily-loss rules, and the LIVE gate are rejected by
the config validator.

---

## Dashboard security

- Binds **127.0.0.1** only; the engine refuses a non-loopback host
- PBKDF2 password authentication, HMAC-signed session cookie, CSRF double-submit
- Login rate limiting and lockout
- CSP, `X-Frame-Options: DENY`, `nosniff`, `no-referrer`, `no-store`
- Secrets are redacted from API responses and state
- Typed confirmations for kill, clear-kill, reset-day, rule approval, revoke, heartbeat
  arm test, and LIVE activation
- No private key or seed phrase is ever displayed

See [docs/SECURITY.md](docs/SECURITY.md) and [docs/OPERATIONS.md](docs/OPERATIONS.md).

---

## Tests

Offline only: no network, no funded keys, no production heartbeat.

```bat
.venv\Scripts\python.exe -m pytest tests
```

The suite covers config hard limits, discovery/lifecycle/rules, feeds and parsers,
health fail-closed logic, features/regimes/probability, sizing/fees/EV, execution and
journal, live order-result mapping and unresolved-order blocking, hybrid exits including
dynamic take-profit, momentum-reversal and liquidity-deterioration rules, risk protections,
heartbeat/recovery/gate, analytics, API auth/CSRF, Parquet/DuckDB, and an
offline engine iteration using the fake exchange adapter.

Actual last local result: **137 passed, 0 failed, 0 errors, 0 skipped** (Python 3.11,
Windows, 24.3s). This number is recorded from the real run; it is not a profitability
claim. See [docs/VALIDATION_REPORT.md](docs/VALIDATION_REPORT.md) for scope and limitations.

---

## Recovery

Startup recovery treats the exchange as the source of truth. If an order or position is
ambiguous, recovery blocks and the bot stays in NO TRADE until a human resolves it.
Backups are verified with SQLite integrity checks before use. Full procedures are in
[docs/OPERATIONS.md](docs/OPERATIONS.md).

---

## Non-goals / honest limitations

- No guarantee of profit, win rate, or edge.
- The strategy parameters are defaults, not validated alpha.
- LIVE has not been activated or funded in this build; the LIVE gate and statistical
  validation intentionally fail on an empty database.
- Exchange protocol details can change. Re-verify market metadata, fees, minimum order
  size, and resolution text against live endpoints before any LIVE attempt.
- Do not use VPNs/proxies to bypass geography, and do not share the API key with other
  software.
