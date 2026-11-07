# Remaining work (handoff prompt)

Paste the block below into a new Claude / Claude Code session opened on this repository.
It carries the context of the change this PR makes and what is still open.

---

## Prompt

You are working on POLYBOT (`btc5m_bot/`), a Polymarket BTC 5-minute UP/DOWN trading bot.
PAPER is the default mode and LIVE stays behind a 24-condition gate plus a typed phrase. Do not
weaken the LIVE gate. Run `python -m pytest -q` before every commit; the suite must stay green.

### What the previous PR changed (already done, do not redo)

- Up to **2 open positions** (`max_active_positions`, hard cap 2) and up to **3 entries per market**
  (`max_entries_per_market`, hard cap 3). A 2nd/3rd entry is a **top-up of the same side**, allowed only
  while the position is **in profit** (`bid * shares > cost_basis`), and never after an exit in that
  market. Top-up merges shares, avg price, entry fee and cost basis (`execution/executor.py`,
  `execution/paper.py`). Gating lives in `engine._evaluate_entry`; caps in `risk/manager.pre_trade_check`.
- Exit management now iterates **every** open position (`engine._manage_position` ->
  `_manage_one_position`). Probability/features are only passed for the currently active market.
- Entry window is **150 s** (`types.ENTRY_WINDOW_SECONDS`).
- The loss-streak **cooldown was removed** (`max_consecutive_losses`, `cooldown_minutes`). The streak is
  only a statistic. The daily loss limit is the only loss guard.
- **Rule approval and the Chainlink-only / unknown-rule gates were removed**: rules are auto-approved,
  `RULES_UNVERIFIED` / `RULES_UNAPPROVED` are no longer emitted, the LIVE gate's two rule conditions always
  pass, oracle health no longer checks the provider label (`TwapTracker.health(require_chainlink=False)`),
  and TWAP projection no longer filters on `is_chainlink`.
- Removed settings are listed in `RuntimeConfig.LEGACY_KEYS`, so old `runtime.json` files still load.

### Open tasks (in priority order)

1. **Daily loss limit vs trade size.** Default `daily_loss_limit_usd` is 1; with `trade_size_usd` >= 1
   a single full loss locks the day. Add a plain-words warning to the Settings advice
   (`api/app.py::_advice`, `sizing.py::trade_size_advice`) when the limit is below about 2-3x the trade
   size, and consider showing the worst-case exposure `trade_size_usd * max_entries_per_market *
   max_active_positions`. Do not silently change the user's values.
2. **Multi-position blind spots.**
   - `recovery.py` and `cli.py status` (`repo.open_position()` returns one row) were not reviewed for 2
     positions. Make startup recovery reconcile every open position and make `status` list all of them.
   - The dashboard "open position" card (`engine._publish`, `ledger.active_position`) shows only the first
     position. Publish a list and render all open positions in `web/app.js`.
3. **LIVE top-up path.** The top-up was tested with the fake adapter and the paper broker only. Review
   `exchange/sdk_adapter.py` and `recovery.py` for entries that add to an existing position, and add tests.
4. **Engine-level tests for the new entry gating.** Cover: top-up refused when not in profit, refused on the
   opposite side, refused after an exit, allowed when in profit; 2nd position allowed in a new market while
   the previous market's position is still open; 3rd position refused.
5. **Exit rules review (needs PAPER data, do not change on a hunch).** In `exits.py` the precedence is
   emergency -> stop loss (-25%) -> trailing stop (SHADOW by default) -> take profit (+20%, dynamic) ->
   probability reversal -> momentum reversal -> liquidity deterioration -> hold-vs-sell EV -> time exit
   (30 s before end) -> hold to resolution. Ask for / analyse recorded PAPER trades, then decide whether
   to drop a fixed stop loss, merge the overlapping reversal / EV rules, and turn trailing stop ON or
   delete it.
6. **Edge and strategy gates (needs PAPER data).** `min_net_edge` 0.05 is large for a market this
   efficient, and the active strategy (`momentum`) must agree with the probability model's side. Measure
   how many good trades that agreement filters out before keeping or removing it. Remove strategies that
   stay unused (`reversal`, `breakout`, `trend_confirmation`) only if the owner agrees.
7. **Outcome simulator.** `oracle/simulator.py` still refuses unknown / non-Chainlink rules when
   recording actual outcomes, so analytics skip those markets. Decide whether to record them with a
   lower confidence flag instead.
8. **Docs.** `docs/SPEC.md`, `docs/PHASE0_REPORT.md` and `docs/VALIDATION_REPORT.md` still describe the
   120 s window, one position, one entry, the 2-loss cooldown and rule approval. Mark them as historical
   or update them.
9. **Repo hygiene.** `polybot.egg-info/` is tracked in git and changes on every `pip install -e .`.
   Remove it from the index and add it to `.gitignore`.

### Safety notes to keep in mind

- Every entry spends `trade_size_usd`, so worst-case exposure is `trade_size_usd * 3 * 2`.
- Removing the rule / Chainlink gates adds basis risk: the market may resolve on a different source than
  the Chainlink TWAP the model uses.
- Run a few hundred PAPER trades (see `paper_validation_min_trades`) before any LIVE activation.
- Never paste tokens or keys into chat, commits or issues.
