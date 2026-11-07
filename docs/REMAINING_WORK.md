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

### Done in the follow-up commits of this PR

- **Loss limit vs trade size:** Settings advice warns when `daily_loss_limit_usd` is at most one trade or
  under 2x the trade size, and reports the worst-case exposure. It only warns; values are never changed.
- **Multi-position:** `repo.open_positions()` replaces the single-row `open_position()`, `status` lists every
  open position, and the dashboard publishes and renders all of them (`positions`).
- **Recovery of a LIVE top-up:** a top-up fill is merged into the open position (or recognised as already
  merged) instead of blocking startup or being dropped. Sizes that cannot be explained still block.
- **Entry gating tests:** `tests/test_entry_gating.py`.
- **Docs:** SPEC, PHASE0_REPORT and VALIDATION_REPORT carry a "historical" banner listing what changed.
- **Repo hygiene:** `polybot.egg-info/` is untracked and ignored.

### Still open (in priority order)

1. **Review `exchange/sdk_adapter.py::place_order` for top-ups.** Confirm the fill size and average price
   come from the order's own fills, never from the exchange's total position, which includes earlier
   entries. Not reviewed yet. Do this before any LIVE activation.
2. **Exit rules review (needs PAPER data, do not change on a hunch).** In `exits.py` the precedence is
   emergency -> stop loss (-25%) -> trailing stop (SHADOW by default) -> take profit (+20%, dynamic) ->
   probability reversal -> momentum reversal -> liquidity deterioration -> hold-vs-sell EV -> time exit
   (30 s before end) -> hold to resolution. Analyse recorded PAPER trades, then decide whether to drop a
   fixed stop loss, merge the overlapping reversal / EV rules, and turn trailing stop ON or delete it.
3. **Edge and strategy gates (needs PAPER data).** `min_net_edge` 0.05 is large for a market this
   efficient, and the active strategy (`momentum`) must agree with the probability model's side. Measure
   how many good trades that agreement filters out before keeping or removing it. Remove strategies that
   stay unused (`reversal`, `breakout`, `trend_confirmation`) only if the owner agrees.
4. **Outcome simulator (owner decision).** `oracle/simulator.py` still refuses unknown / non-Chainlink rules
   when recording actual outcomes, so analytics skip those markets. Decide whether to record them with a
   lower confidence flag instead.

### Safety notes to keep in mind

- Every entry spends `trade_size_usd`, so worst-case exposure is `trade_size_usd * 3 * 2`.
- Removing the rule / Chainlink gates adds basis risk: the market may resolve on a different source than
  the Chainlink TWAP the model uses.
- Run a few hundred PAPER trades (see `paper_validation_min_trades`) before any LIVE activation.
- Never paste tokens or keys into chat, commits or issues.
