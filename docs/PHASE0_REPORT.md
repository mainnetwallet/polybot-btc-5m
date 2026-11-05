# PHASE 0 — AUDIT AND PLAN REPORT

- Date: 2026-11-05
- Status: FINAL (awaiting approval) — all official-documentation verification completed 2026-11-05
- Master spec: `docs/SPEC.md`

---

## 1. Repository / Workspace Audit

- `C:\Users\mainw\New folder` is **empty** — fresh project, no existing code, data, or trading records to preserve.
- Not a git repository. (Proposal: `git init` in Phase 1 so every phase is reviewable.)
- Environment: Windows 10 (build 26300), Python 3.11.9 (`C:\Users\mainw\AppData\Local\Programs\Python\Python311\python.exe`), pip 26.2.1. Python 3.10 also installed (unused). No `uv`. Git Bash available.
- The master spec has been preserved at `docs/SPEC.md` (the attachment's temp location is not permanent).

Conclusion: no repository-level constraints exist. Clean-slate implementation per the spec.

## 2. Verified Protocol Facts

All items verified against official sources on 2026-11-05 (docs.polymarket.com, PyPI, GitHub py-sdk, live Gamma API, OpenAPI/AsyncAPI contracts). Two live-observation discrepancies are flagged in §2.7 — they will be resolved empirically in Phase 1 before any sizing code is trusted.

### 2.1 Unified Python SDK — PIN: `polymarket-client==0.16.0`

- PyPI package `polymarket-client`, import `polymarket`; latest stable **0.16.0** (2026-11-02), `requires_python >= 3.11` (we have 3.11.9). Confirmed directly via PyPI JSON.
- Clients: `PublicClient`, `SecureClient`, and async twins `AsyncPublicClient`, `AsyncSecureClient` (native async — no thread-pool wrappers needed). Legacy `CLOBClient`/`MarketDataClient`/`PolyBoltClient` no longer exist.
- No automatic `.env` loading; pass `private_key` to `SecureClient.create()`; optional relayer/builder API keys.

### 2.2 CLOB V2 protocol

- REST `https://clob.polymarket.com`; WS `wss://ws-subscriptions-clob.polymarket.com/ws/market` (public market stream) and `/ws/user` (authenticated).
- Auth: L1 = EIP-712 `ClobAuth` message → `POST /auth/api-key` (or derive) → apiKey/secret/passphrase; L2 = HMAC-SHA256 over `timestamp+method+path+body`, headers `POLY_ADDRESS`, `POLY_SIGNATURE`, `POLY_TIMESTAMP`, `POLY_API_KEY`, `POLY_PASSPHRASE`.
- Order signing: EIP-712, Exchange domain version **"2"**; order struct carries `timestamp`(ms)/`metadata`/`builder`.
- Wallet `signature_type`: 0 EOA, 1 POLY_PROXY, 2 POLY_GNOSIS_SAFE, 3 POLY_1271.
- Collateral: **pUSD** `0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB` (6 decimals). Exchanges: CTF Exchange V2 `0xE111180000d2663C0091e4f400237545B87B996B`, NegRisk CTF Exchange `0xe2222d279d744050d28e00520010520000310F59`, ExchangeV3 Poly v2 `0xe3333700cA9d93003F00f0F71f8515005F6c00Aa`. No funder address in V2.
- Balance check: `GET /balance-allowance?asset_type=COLLATERAL` (L2). Docs show max-approval examples; spec §6 forbids unlimited allowances → exact-amount approvals (SDK amount parameter verified in Phase 4).
- pUSD replaced USDC.e as CLOB collateral on 2026-04-28 with CLOB V2 go-live.

### 2.3 Market identifiers, discovery, and market version

- Market metadata: Gamma API `gamma-api.polymarket.com` (public, no auth): `GET /markets/slug/{slug}`, `/markets/{id}`, `/markets` (params `id`, `slug`, `condition_ids`, `clob_token_ids`, `closed`, `tag_id` — crypto tag 21, "Crypto Prices" 1312), `/markets/keyset` (cursor pagination). CLOB supplement: `GET /clob-markets/{condition_id}`.
- V2 markets: per-outcome `positionIds`; V1 (CTF): `clobTokenIds`; authoritative field: `version` (`"v1"`/`"v2"`). `negRisk` boolean.
- **Live observation 2026-11-05: current BTC 5-minute markets are `version: "v1"`, `negRisk: false`, `positionIds: null`** → execution targets V1 CTF token IDs today; V2 position-ID routing retained (spec §6).
- Market Data API: `data-api.polymarket.com` — Data API v1 is **retired**; `/v2` endpoints (positions, trades, activity, prices-history, resolutions, tokens...), no auth. No markets-list endpoint in v2 (Gamma covers discovery; adapter isolates this).

### 2.4 BTC 5-minute market structure and resolution rules

- Series slug `btc-up-or-down-5m`; event slug `btc-updown-5m-<epoch-seconds>`; market slug = event slug. Continuous back-to-back 5-minute windows (verified 6 consecutive events 300 s apart).
- Official rule text (market description): resolves **Up** if the Chainlink BTC/USD price at the END of the time range is **greater than or equal to** the price at the BEGINNING; otherwise **Down**. Tie → Up. Source: Chainlink BTC/USD data stream (`data.chain.link/streams/btc-usd`).
- Current methodology: **60-second TWAP** (all 5-min crypto markets, effective 2026-08-14 00:00 UTC). Historical: before 2026-08-07 single snapshot; 2026-08-07 → 08-13: 30-second TWAP. Matches spec §9 exactly.
- UNVERIFIED (undocumented): exact TWAP window anchoring vs the 5-min boundary (start- vs end-anchored) and value precision/rounding → the resolution simulator captures per-market rule text and validates empirically (Phase 2); exchange spot prices never substitute oracle values.

### 2.5 PolyBolt (oracle feed)

- URL `wss://ws-live-v2.polymarket.com/ws`; **authentication required** (CLOB API credentials: `{"op":"auth","auth":{apiKey, secret, passphrase}}`).
- Subscribe: `{"op":"subscribe","subscriptions":[{"channel":"price.crypto.twap","filter":{"symbol":"btcusd","window_seconds":60}}]}`.
- `price.crypto.twap`: symbol lowercase slash-stripped (`btcusd`); **only `window_seconds: 60` carries data**; messages carry `value` (float approx), `full_accuracy_value` (exact decimal — use this), `timestamp` (ms), `source`; on subscribe a snapshot of the preceding ~2 minutes is delivered; Chainlink-produced.
- UNVERIFIED: per-observation cadence (event-driven). Server pings every 25 s.
- Also available: `price.crypto`, `price.polymarket`, `price.equity`, `price.equity.twap`, `esports.series`.

### 2.6 CLOB market WebSocket (order books/trades)

- `wss://ws-subscriptions-clob.polymarket.com/ws/market` is **public** (no auth headers for the market stream); app-level `PING` every 10 s (`PONG` reply).
- Subscribe: `{"assets_ids": ["<token_id>"], "type": "market"}`. Events by `event_type`: `book`, `price_change`, `last_trade_price`, `tick_size_change` (+ `best_bid_ask`, `new_market`, `market_resolved` when enabled).

### 2.7 Order constraints and fees (crypto)

- Live BTC 5-min metadata: `orderMinSize: 5`. **Tick-size discrepancy between two independent live observations: `orderPriceMinTickSize` 0.01 vs 0.001** → resolve empirically in Phase 1 (read per-market metadata; never hardcode). Docs show crypto tick examples of 0.01.
- **Unit conflict (open):** docs call `minimumOrderSize` "minimum USDC notional per order" in one page and `min_order_size` "minimum number of shares" in another → §8 plans both interpretations; resolved empirically in Phase 1.
- BUY vs SELL: docs show identical size/price constraints; no side-specific minimums documented (validated against actual CLOB behavior in Phase 4, per spec §22).
- Fees (crypto): taker rate `0.07`, maker `0`, maker rebate 20%; fee = `C × rate × p × (1-p)`, rounded to 5 decimals. Market metadata carries `feesEnabled` + `feeSchedule {rate, exponent, takerOnly, rebateRate}` — always read live values, never hardcode.

### 2.8 Taker delay

- **150 ms on crypto markets**, effective 2026-09-04 14:00 UTC (50 ms since 2026-08-17; 250 ms before). Included in execution modelling, slippage, EV, and paper simulation; re-verified from metadata per market.

### 2.9 Geographic restrictions

- Current official endpoint: `GET https://polymarket.com/api/geoblock` (IP-based) → `{blocked, ip, country, region}`; close-only jurisdictions (US, GB, FR, DE, ...); OFAC countries fully blocked.
- The address-based `geo-v2.polymarket.com/eligible` endpoint is no longer documented (legacy) → the LIVE gate uses whatever endpoint is officially documented at activation time; eligibility is verified live, never assumed.

### 2.10 Redemption

- `SecureClient.redeem_positions(condition_id | market_id | position_id)` handles CTF and V2 paths; no redemption deadline; redeems full balance.

### 2.11 Heartbeat API (safety-critical, fully verified)

- `POST https://clob.polymarket.com/v1/heartbeats`. First request `{"heartbeat_id": ""}`; response 200 returns the new UUID to store and echo back. Mismatch → HTTP 400 `Invalid Heartbeat ID`.
- Interval: every **5 s**. After **10 s** without an accepted send, a 5-second scan removes registration and queues a **best-effort cancel-all** (timing/success NOT guaranteed). WebSocket keepalives do NOT refresh this deadline.
- **The first accepted send enables timeout cancellation for ALL open orders owned by that API key, across markets and processes. There is NO disarm operation.** Stopping sends leaves the deadline active. Legacy `/heartbeats` shares the registration.
- SDK: `send_order_heartbeat()` on `SecureClient` / `AsyncSecureClient`.
- Consequences for this bot: dedicated API key used exclusively by the bot (never shared); heartbeat worker sends reliably while the engine is healthy and stops when the engine is unhealthy (lease design §3.1); every recovery path treats `CANCELLED_BY_HEARTBEAT` as a normal event (spec §27).

### 2.12 Sources

docs.polymarket.com (getting-started, api-spec/clob-openapi.yaml, changelog, contracts, fees, geoblock, market-data, place-orders, api-reference/wss/polybolt.md, migrate/rtds-to-polybolt.md, realtime-data, concepts/pusd.md, resolution.md); pypi.org/pypi/polymarket-client/json; github.com/Polymarket/py-sdk; live gamma-api.polymarket.com observations; ws-live-v2.polymarket.com/asyncapi.json; data-api.polymarket.com/v2/openapi.json.

## 3. Architecture Plan

### 3.1 Process topology (Windows-friendly, asyncio)

Three supervised processes, all state persisted in SQLite so any process can crash independently:

1. **Supervisor** (`python -m btc5m_bot.supervisor`)
   - Spawns and monitors the engine and heartbeat worker (multiprocessing, spawn context).
   - Writes a **liveness lease** (SQLite row, engine-healthy timestamp) renewed by the engine.
   - Crash-loop protection (bounded restarts per window), clean shutdown, restart sequence per spec §32.
   - Runs under Windows Task Scheduler (documented in Phase 6) and manually.

2. **Engine** (main process; includes FastAPI dashboard on 127.0.0.1)
   - Market discovery/lifecycle, BTC feeds, PolyBolt oracle feed, signal pipeline, probability engine, entry engine, risk manager, execution, exit manager, recovery, analytics.
   - Renews the liveness lease every cycle; if the engine is unhealthy, the lease expires → heartbeat stops → platform resting-order safeguard can trigger (spec §27).

3. **Heartbeat worker** (dedicated process, bounded timeouts)
   - Sends `POST /v1/heartbeats` on its own loop (~5 s target) **only while the engine lease is valid**.
   - Owns `heartbeat_id` persistence/serialization; never shares its API key with anything else.

### 3.2 Internal layering (BOT_BASELINE first, BOT_FINAL later)

```
feeds ──► recorders ──► signal pipeline ──► probability engine ──► entry engine
                                                                    │
                                   RISK MANAGER (absolute authority)│
                                                                    ▼
                                    execution (adapter ► CLOB V2) ► order journal
                                                                    │
                                        exit manager ◄── positions ◄┘
                                                                    │
              analytics ◄── decisions/trades/signals ◄──────────────┘
```

- **ExchangeAdapter** — the only place that touches the Polymarket SDK. All order, book, balance, market-metadata, geo, and heartbeat operations go through it. SDK upgrades never require engine rewrites (spec §3).
- **Decision audit trail** — every evaluation (including every NO TRADE) is logged with its reason code and feature snapshot (spec §7/§12).
- **Feature modes** — every experimental feature carries OFF / SHADOW / ACTIVE state persisted in config/DB; protections default ACTIVE, experimental enhancements default SHADOW (spec §36).
- Internal version names only: `BOT_BASELINE` (initial) and `BOT_FINAL` (enhanced). `POLYMARKET_CLOB_V2` refers only to the external exchange protocol.

### 3.3 Data flow for one 5-minute window

1. Discovery: dynamic market scan finds the next BTC 5-min window (never hardcoded IDs/slugs).
2. Lifecycle: DISCOVERED → ACTIVE_ENTRY_WINDOW (first 120 s, only period new entries allowed) → ENTRY_CLOSED → RESOLVING → SETTLED; persisted across restarts (spec §7).
3. During the window: oracle opening reference captured; TWAP series recorded; features computed (5 s/15 s/30 s/1 m/3 m returns, volatility, momentum, regime, filters).
4. Entry evaluation every 1–2 s during the 120 s window: P(UP), net EV after fees/slippage/spread, mandatory gates, SELL-feasibility pre-check, sizing under all five limits.
5. If entered: position attached to exit manager; orders journaled before submission (idempotency keys); fills verified; latency recorded.
6. Post-window: resolution simulator vs actual resolution; performance/loss analytics update.

## 4. Module Mapping

| Module | Responsibility | Spec § |
|---|---|---|
| `config` | Pydantic Settings; user config (capital, risk %, TP/SL, feature modes); validation | 4, 16, 36 |
| `db` | SQLAlchemy engine, WAL, models, Alembic, integrity/backup | 33 |
| `exchange` | SDK adapter: CLOB V2, pUSD, data API, geo, redemption | 3, 6, 35 |
| `markets` | Discovery + lifecycle state machine; rule capture/fingerprints | 7, 9 |
| `data` | Primary/secondary BTC feeds, health states, BLOCK failover | 8, 10 |
| `oracle` | PolyBolt TWAP feed, resolution simulator, methodology versions | 9 |
| `signals` | Regime detection, multi-timeframe, false-signal filters, correlation | 11–13, 15 |
| `probability` | TWAP-aware probability model, calibration, Brier/log-loss | 14 |
| `entry` | Dynamic entry engine, EV, timing, opportunity rejection | 16, 17 |
| `sizing` | Dynamic position sizing vs 5 independent limits, capital compatibility | 18, 19 |
| `execution` | Book walking, smart limits, partial fills, cancellation verification, idempotency, latency monitor | 20, 21, 26 |
| `exits` | SELL feasibility (pre-BUY), hybrid exit manager with precedence | 22, 23 |
| `risk` | Risk manager (absolute authority), exposure ledger, protections, kill switch | 5, 24, 25 |
| `heartbeat` | Isolated heartbeat worker, lease, expiry recovery | 27 |
| `recovery` | Order journal (WAL), crash/restart recovery, reconciliation | 32 |
| `paper` | Paper broker: queue uncertainty, slippage, taker delay, partial fills | 31 |
| `analytics` | Performance intelligence, loss analysis, statistical validation (multiple-comparison, sensitivity, ablation) | 28–30 |
| `gate` | 23-condition LIVE gate checker; typed confirmation | 37 |
| `api` | FastAPI backend: dashboard state + authenticated controls | 34 |
| `web` | Static responsive dashboard (served by FastAPI) | 34 |
| `supervisor` | Process supervision, liveness lease, crash-loop protection | 27, 32 |

Package name proposal: `btc5m_bot` (changeable). All internal "bot version" concepts stay as BOT_BASELINE / BOT_FINAL.

## 5. Dependency List

Core (Python 3.11):

- `polymarket-client` — **pinned `==0.16.0`** (verified on PyPI 2026-11-05; native async clients available).
- `sqlalchemy>=2.0`, `alembic` — transactional state + migrations.
- `duckdb`, `pyarrow` — high-frequency analytics (Parquet + DuckDB).
- `fastapi`, `uvicorn[standard]` — backend/dashboard.
- `httpx`, `websockets` — feed clients (also SDK transport).
- `pydantic>=2`, `pydantic-settings` — config and models.
- `numpy`, `pandas` — feature computation (justified: multi-timeframe/regime math).
- Dev: `pytest`, `pytest-asyncio`, `respx` (httpx mocking) or `pytest-httpx`.

No external DB server, no cloud services — everything runs locally.

## 6. Database Plan

### 6.1 SQLite (transactional state, WAL mode, `busy_timeout`, `synchronous=NORMAL`)

Tables: `markets`, `market_windows`, `orders` (write-ahead order journal, states INTENDED→SUBMITTING→ACKED→PARTIAL→FILLED→CANCEL_PENDING→CANCELLED→REJECTED→UNKNOWN), `positions`, `balance_snapshots`, `exposure_reservations`, `risk_state` (daily loss, consecutive losses, cooldown, profit lock, drawdown — persisted across restarts per spec §25), `protection_events`, `heartbeat_state`, `recovery_state`, `resolution_rules` (fingerprint + user approval), `resolutions`, `decision_log` (every signal/NO TRADE with reason), `latency_samples`.

- Alembic migrations from day one (spec §33).
- All order submissions journaled **before** network calls, with idempotency keys (spec §32).
- Backup: `VACUUM INTO` snapshots + `PRAGMA integrity_check`; verified-restore procedure in Phase 6.

### 6.2 Parquet + DuckDB (analytics/time-series)

Datasets: `btc_ticks` (per feed), `oracle_twap_observations`, `book_snapshots`, `market_events`, `signal_features`, `paper_trades`.
- Batch writers (engine flushes every N seconds), per-dataset retention policies, compression (zstd).
- Read-only DuckDB views for analytics/validation; raw data never mutated in place.

## 7. Testing Plan

- `pytest` + `pytest-asyncio`; **mocked APIs only** — never real money (spec §38).
- All exchange I/O behind fakes seeded from captured-but-sanitized market metadata; SDK adapter contract-tested.
- **Fault injection** wrapper around every order-journal state transition (crash between INTENDED/SUBMITTING/ACKED/…).
- Coverage mapped 1:1 to spec §38: lifecycle/120 s cutoff, min BUY/SELL + partial SELL + residual dust, sizing vs all 5 limits, 2 %/4 %/cooldown/profit-lock, fee + taker-delay math, TWAP + resolution-simulator versions, probability calibration, EV, regime/filter/multi-timeframe logic, no-look-ahead checks, correlation caps, multiple-comparison correction, sensitivity, book walking, partial fills, cancellation verification, duplicate prevention, exposure reservations, heartbeat ID rotation/failure/expiry/isolation, latency, hybrid exits, settlement/redemption, feed redundancy + oracle mismatch, crash/restart/DB-corruption recovery, geo restriction, LIVE gate.
- Deterministic seeded RNG and fixed clock injection (`freezegun` or clock abstraction) so tests never depend on wall time.

## 8. Capital Compatibility Report

Verified: crypto order minimum `orderMinSize: 5`; its unit is officially conflicting (USDC notional vs shares — §2.7), and the tick size has a live-observation discrepancy (0.01 vs 0.001 — §2.7). Both minimum-size interpretations are tabulated (entry taker fee = `C × 0.07 × p(1-p)`; a separate exit fee applies on early exit, zero if held to resolution):

| Share price p | Min valid order | Entry fee | Worst-case loss | Min capital (worst / 0.02) |
|---|---|---|---|---|
| **If unit = 5 shares** | | | | |
| $0.40 | 5 sh = $2.00 | $0.084 | $2.084 | **$104.20** |
| $0.45 | 5 sh = $2.25 | $0.087 | $2.337 | **$116.83** |
| $0.50 | 5 sh = $2.50 | $0.088 | $2.588 | **$129.38** |
| $0.55 | 5 sh = $2.75 | $0.087 | $2.837 | **$141.83** |
| $0.60 | 5 sh = $3.00 | $0.084 | $3.084 | **$154.20** |
| $0.65 | 5 sh = $3.25 | $0.080 | $3.330 | **$166.48** |
| $0.70 | 5 sh = $3.50 | $0.074 | $3.574 | **$178.68** |
| **If unit = 5 USDC notional** | | | | |
| $0.40 | $5.00 | $0.084 | $5.084 | **$254.20** |
| $0.45 | $5.00 | $0.087 | $5.087 | **$254.33** |
| $0.50 | $5.00 | $0.088 | $5.088 | **$254.38** |
| $0.55 | $5.00 | $0.087 | $5.087 | **$254.33** |
| $0.60 | $5.00 | $0.084 | $5.084 | **$254.20** |
| $0.65 | $5.00 | $0.080 | $5.080 | **$253.98** |
| $0.70 | $5.00 | $0.074 | $5.074 | **$253.68** |

(Share counts pre-rounding; final sizing rounds down to the verified tick, which slightly raises required capital.)

**Verdict:** the 2 % risk limit is only compatible if allocated capital is ≥ ~$105 (shares interpretation) / ≥ ~$255 (notional interpretation) per single minimum order — allowing exactly one minimum-sized position at a time under the cap. Phase 1 resolves the unit and tick empirically (live metadata + CLOB validation behavior); Phase 4 verifies SELL-side minimums the same way. If the minimum valid order cost exceeds the 2 % budget → `NO TRADE — BELOW_MIN_ORDER`; risk limits are never raised to fit (spec §5/§18).

*User's configured capital is still needed for the final verdict.*

## 9. Exit Feasibility Report

Verified: crypto BUY and SELL share the same documented constraints (`orderMinSize: 5`; tick per §2.7 discrepancy) — the unit conflict applies to both sides; no side-specific minimums documented. Spec §22 still requires validating actual SELL behavior in Phase 4 (including partial-exit dust at tick precision). Deliverables:

- Pre-BUY SELL feasibility check (full, stop-loss, take-profit, partial exits, residual dust, partial fills).
- Reason codes: `EXIT_NOT_FEASIBLE`, `SELL_BELOW_MINIMUM`, `PARTIAL_EXIT_UNAVAILABLE`, `RESIDUAL_DUST`.
- If partial exits are impossible at minimum size → disable partial exits, use full-position exits.
- If full exit impossible → no entry (unless user explicitly approves resolution-only).
- Tests: min-SELL rejection, partial fills, SL/TP feasibility, residual balances.

## 10. Heartbeat Safety Plan

Heartbeat semantics fully verified (§2.11): the first accepted heartbeat arms API-key-wide timeout cancellation of all resting orders; **no disarm exists**; deadline = 10 s without an accepted send + 5 s scan; the cancel-all is best-effort. Plan per spec §27:

- Dedicated API key used exclusively by the bot; never shared with other trading processes.
- Isolated worker process + bounded timeouts; heartbeat-id rotation and serialized updates.
- Liveness lease: heartbeats stop when the engine is unhealthy, so the platform safeguard can cancel resting orders.
- Expiry recovery: `CANCELLED_BY_HEARTBEAT` handling → query CLOB order states → verify cancellations/partial fills → reconcile balances/positions → restore exposure reservations → attach exit management → recovery mode if needed.
- Tests: ID rotation, failure, timeout, worker crash, reconnection, restart, expiry-cancellation recovery.

## 11. Open Technical Decisions

1. **Min-order unit conflict** (shares vs USDC notional in official docs) and **tick-size discrepancy** (0.01 vs 0.001 live observations) — resolved empirically in Phase 1 before any sizing code ships.
2. **Current BTC 5-min markets are `version: "v1"`** (CTF token IDs) — execution targets V1 primarily; V2 position-ID routing retained; watch for v1→v2 migration announcements.
3. **Geo eligibility**: address-based geo-v2 endpoint is legacy; current official is IP-based `polymarket.com/api/geoblock` — LIVE gate re-verifies at activation time.
4. **Heartbeat**: no disarm exists; key-wide cancellation is armed by the first accepted send → dedicated API key is mandatory (planned, §2.11).
5. **TWAP details still undocumented**: window anchoring vs the 5-min boundary and comparison precision/rounding — resolve empirically via the resolution simulator (Phase 2).
6. **User's allocated capital** — REQUIRED INPUT (affects §8 verdict).
7. **Wallet type** (EOA vs Gnosis-safe, funder address) — needed only for LIVE gate; not blocking Phases 1–3.
8. **Frontend approach** — proposal: static HTML/JS + fetch served by FastAPI (no build toolchain; responsive, localhost-only).
9. **BTC feed providers** — proposal: Binance + Coinbase public websockets (primary/secondary) with PolyBolt as oracle feed; final choice in Phase 1.
10. **`git init`** in Phase 1 — propose yes (history per phase); user's call.
11. **Paper-trade minimum sample** — spec target 300 trades (configurable); collection starts in Phase 4.

## 12. Risk Assessment

- **Market risk**: BTC 5-minute UP/DOWN outcomes are near coin-flip; edges (if any) are thin and can vanish. Fees + spread + adverse selection make naive strategies net-negative. The spec's own answer — rigorous statistical validation, NO TRADE by default — is the right one.
- **Protocol risk**: resolution methodology has already changed three times (snapshot → 30 s TWAP → 60 s TWAP); a rules change mid-flight is a real risk → rule-fingerprint verification + resolution simulator + mismatch auto-pause. Market-version risk: live BTC 5-min markets are v1 today; a platform migration to v2 position IDs would change asset routing → identifiers re-read on every discovery via the adapter, never hardcoded.
- **Execution risk**: partial fills, rejected SELLs, taker delay, latency → mitigated by feasibility pre-checks, journaling, conservative slippage assumptions.
- **Operational risk**: Windows sleep/restart/power loss → WAL journal + recovery sequence + supervision; heartbeat safeguard covers engine death. Heartbeat risk: no disarm exists — if the worker stalls while the engine is down, the key-wide cancel-all safeguard fires (by design); recovery treats `CANCELLED_BY_HEARTBEAT` as a normal event and reconciles.
- **Security**: private key handling — key stays in env/local secret store, never displayed/logged; dashboard binds 127.0.0.1 with authenticated controls + CSRF + typed LIVE confirmation; dedicated low-balance wallet recommended; geo-eligibility verified before LIVE.
- **Capital risk**: worst case is full loss of allocated capital — it must be money the user can afford to lose; risk caps (2 %/4 %/cooldown/kill switch) limit drawdown rate, not loss existence.
- **No guaranteed profit.** The deliverable is a disciplined system with honest validation; whether it trades profitably is decided by data, not promises.

## 13. Phase 1 Proposal (after approval)

- `git init` (if approved), project scaffolding (`pyproject.toml`, package `btc5m_bot`, venv).
- Pin verified `polymarket-client` version; install dependencies.
- Implement: market discovery + lifecycle, BTC primary/secondary feeds, PolyBolt oracle feed, feed-health/blocking failover, time sync, historical recording (Parquet), order-book snapshots, Alembic migrations.
- Collect data for ALL eligible markets (not just traded ones).
- Run tests; report actual results; STOP.

### Required inputs from the user (to finalize §8 and proceed)

1. **Approval of this plan** (and any changes you want).
2. **Allocated capital amount** (e.g., pUSD figure) for the capital-compatibility verdict.
3. Confirmation of package-name/frontend/feed-provider/git proposals (or your preferences).
