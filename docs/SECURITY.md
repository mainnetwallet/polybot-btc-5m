# POLYBOT security

This document describes the security model actually implemented in this repository.
It is not an external audit or a guarantee against loss.

## Scope and assumptions

- Windows host used by a single operator.
- The dashboard is bound to `127.0.0.1`; anyone with local access to that account can
  reach it, so the Windows account itself must be protected.
- The exchange, its API, and the Polymarket protocol are trusted for the data they
  return, subject to the fail-closed checks described below.
- Polymarket's geographic eligibility must be verified live. VPNs and proxies must not
  be used to bypass restrictions.

## Secrets

| Secret | Storage | Notes |
|---|---|---|
| Private key | `.env` (`POLYBOT_PRIVATE_KEY`) | never logged, never returned by the API |
| CLOB API key/secret/passphrase | `.env` | used by the SDK and PolyBolt |
| Dashboard password hash | `.env` | PBKDF2-SHA256, 240k iterations, random salt |
| Session signing secret | `.env` | HMAC-SHA256; an ephemeral value is used if absent, invalidating sessions on restart |
| Runtime settings | `config\runtime.json` | no secrets |

Rules:

- `.env`, `config\runtime.json`, `config\live.flag`, databases, logs, and Parquet files
  are git-ignored.
- Never commit, paste, or screenshot secrets. Never send a seed phrase anywhere.
- Use a dedicated wallet and a dedicated API key that no other software uses.
- Restrict filesystem permissions to the operator account.
- Rotate credentials if exposure is suspected. Heartbeat cancellation applies to the
  entire API key, so a shared key can cancel another application's orders.

`security.redact()` masks secret-looking dictionary keys, `0x`-prefixed 64-hex private
keys, and is applied to JSON logs. API responses are built from explicit fields and do
not include settings/secrets. Tests assert that state/settings secrets do not appear in
responses.

## Wallet approvals

POLYBOT **never submits an allowance approval transaction** and never approves an
unlimited allowance. It reads the current collateral allowance from the exchange and the
LIVE gate requires:

```text
allowance >= configured capital_usd
```

If the allowance is insufficient, LIVE activation fails with the exact condition. Any
approval must be performed deliberately by the operator outside this application, with a
bounded amount no larger than what is required. Re-check the spender address and chain
before signing anything.

## Dashboard

- Loopback binding is enforced in the engine before uvicorn starts.
- Login: username + password; failed attempts are rate-limited per client (5 failures in
  5 minutes; 5-minute lockout).
- Sessions: HMAC-SHA256 signed, expiry-bound, HttpOnly, SameSite=Strict cookie.
- CSRF: double-submit token required in the `x-csrf-token` header for every mutating
  request.
- Response headers: strict CSP, `X-Frame-Options: DENY`, `X-Content-Type-Options:
  nosniff`, `Referrer-Policy: no-referrer`, `Cache-Control: no-store`.
- No FastAPI docs/OpenAPI endpoints are exposed.
- Mutating controls require typed confirmations:
  `KILL`, `CLEAR KILL`, `RESET DAY`, `APPROVE RULE`, `REVOKE LIVE`,
  `ACTIVATE LIVE TRADING`, and the one-way `ARM HEARTBEAT` test.
- The config endpoint writes only an allowlisted set of fields. Attempts to relax hard
  limits raise `HardLimitError` and are rejected.
- The dashboard never displays a private key or seed phrase.

## Trading safety

- 2% planned-loss cap per trade, computed as a full loss even when a stop is planned.
- 4% daily loss limit, profit lock option, two-loss cooldown, one position, one entry per
  market, hard 120-second entry window.
- Position size is never increased to satisfy exchange minimums; the order is skipped.
- Chainlink only for resolution. Pyth/spot text is rejected as a substitute. Unknown
  methodology, unknown source, unknown tie rule, or low rule confidence means NO TRADE.
- Rule fingerprints must be approved by the operator when `require_rules_approval` is true.
- Write-ahead order journal; `UNKNOWN` exchange outcomes are never blindly retried.
- Partial fills and DUST positions are held to resolution if selling is not feasible.
- Startup recovery uses the exchange as the source of truth and blocks on ambiguity.

## Heartbeat

The platform heartbeat is a key-wide dead-man switch: the first accepted heartbeat arms a
cancel-all-on-timeout that cannot be disarmed. Consequences:

- Heartbeat is disabled by default.
- It sends only when a persisted LIVE activation exists, the runtime mode is LIVE, the
  engine lease is fresh and healthy, and the heartbeat configuration is valid.
- It never sends in PAPER or when the lease is absent/stale.
- The gate requires a recorded explicit arm test.
- Use a key dedicated exclusively to POLYBOT.
- Do not enable or test it during development.

## Data and supply chain

- Dependencies are pinned at the top level in `pyproject.toml`
  (`polymarket-client==0.16.0`, SQLAlchemy, Alembic, DuckDB, PyArrow, FastAPI, uvicorn,
  httpx, websockets, pydantic, numpy, pandas).
- Tests are offline: the fake exchange adapter, synthetic feeds, and a temporary SQLite
  database. No funded wallet or production heartbeat is used.
- The exchange SDK is isolated behind `exchange/base.py` and `exchange/sdk_adapter.py`.
- Market IDs, slugs, token IDs, fees, tick size, and minimum size come from live metadata,
  never hardcoded.

## Logging and audit

- JSON-lines logs with rotation.
- Decisions, order/trade transitions, rule approvals, LIVE activation/revocation,
  heartbeat failures, recovery blocks, and protection events are persisted.
- Secrets are redacted, but treat logs as sensitive operational data anyway.

## Residual risks

- Software bugs, exchange API changes, network failures, and model error can cause loss.
- Local malware or an unlocked Windows session can control the bot.
- A compromised dependency or the exchange itself is outside this code's control.
- The statistical validation gate reduces, but cannot eliminate, the risk of deploying a
  strategy that does not generalize.

No security or profitability warranty is provided. Review the code and run PAPER first.
