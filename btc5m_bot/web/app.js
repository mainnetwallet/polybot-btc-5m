"use strict";

const $ = (id) => document.getElementById(id);
let csrf = null;
let stateTimer = null;
let lastState = null;

function text(value, fallback = "—") {
  if (value === null || value === undefined || value === "") return fallback;
  return String(value);
}

function el(tag, cls, content) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (content !== undefined) node.textContent = content;
  return node;
}

function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

function isEmpty(v) { return v === null || v === undefined || v === "" || v === "—"; }

function kv(container, rows) {
  clear(container);
  const shown = rows.filter((r) => !isEmpty(r[1]));
  if (!shown.length) { container.appendChild(el("p", "idle-note", "No data yet.")); return; }
  for (const [key, value, cls] of shown) {
    container.appendChild(el("div", "k", key));
    container.appendChild(el("div", "v" + (cls ? " " + cls : ""), text(value)));
  }
}

async function api(path, options = {}) {
  const opts = Object.assign({ credentials: "same-origin", headers: {} }, options);
  opts.headers = Object.assign({}, options.headers);
  if (opts.body && typeof opts.body !== "string") {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(opts.body);
  }
  if (opts.method && opts.method !== "GET" && csrf) {
    opts.headers["X-CSRF-Token"] = csrf;
  }
  const res = await fetch(path, opts);
  if (res.status === 401) {
    showLogin();
    throw new Error("authentication required");
  }
  let payload = null;
  try {
    payload = await res.json();
  } catch (_e) {
    payload = null;
  }
  if (!res.ok) {
    const detail = payload && payload.detail ? payload.detail : "request failed";
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return payload;
}

/* ---------------------------------------------------------------- session */
function showLogin() {
  if (stateTimer) clearInterval(stateTimer);
  stateTimer = null;
  stopStream();
  csrf = null;
  $("app").classList.add("hidden");
  $("login").classList.remove("hidden");
  $("login-error").textContent = "";
}

function showApp() {
  $("login").classList.add("hidden");
  $("app").classList.remove("hidden");
  route();
  if (window.Hero) window.Hero.start(api);
}

async function boot() {
  try {
    const session = await api("/api/session");
    csrf = session.csrf;
    showApp();
    startState();
  } catch (_e) {
    showLogin();
  }
}

$("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const username = $("login-user").value.trim();
  const password = $("login-pass").value;
  try {
    const result = await api("/api/login", { method: "POST", body: { username, password } });
    csrf = result.csrf;
    showApp();
    startState();
  } catch (err) {
    $("login-error").textContent = err.message;
  }
});

$("logout").addEventListener("click", async () => {
  try { await api("/api/logout", { method: "POST" }); } catch (_e) { /* ignore */ }
  showLogin();
});

/* ------------------------------------------------------------------ state */
const POLL_MS = 500;
let inflight = false;
let ws = null;
let wsRetry = 0;
let wsTimer = null;
let lastPushAt = 0;
const clockState = { offsetMs: 0 };

function startState() {
  if (stateTimer) clearInterval(stateTimer);
  refreshState();
  stateTimer = setInterval(refreshState, POLL_MS);
  connectStream();
}

function stopStream() {
  if (wsTimer) clearTimeout(wsTimer);
  wsTimer = null;
  if (ws) { ws.onclose = null; try { ws.close(); } catch (_e) { /* ignore */ } }
  ws = null;
}

/* Live push from the server; polling above is only a fallback when the socket is down. */
function connectStream() {
  if (ws || document.hidden) return;
  try {
    ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/api/stream");
  } catch (_e) { ws = null; return; }
  ws.onopen = () => { wsRetry = 0; };
  ws.onmessage = (event) => {
    try { handlePayload(JSON.parse(event.data)); lastPushAt = Date.now(); } catch (_e) { /* ignore bad frame */ }
  };
  ws.onclose = () => {
    ws = null;
    if ($("app").classList.contains("hidden") || document.hidden) return;
    wsTimer = setTimeout(connectStream, Math.min(5000, 300 * 2 ** wsRetry++));
  };
  ws.onerror = () => { try { ws.close(); } catch (_e) { /* ignore */ } };
}

function handlePayload(payload) {
  const serverMs = Date.parse(payload.server_time);
  clockState.offsetMs = Number.isFinite(serverMs) ? serverMs - Date.now() : 0;
  lastState = payload.state;
  renderState(payload.state, payload.server_time);
  if (window.Hero) window.Hero.update(payload.state, payload.server_time);
}

async function refreshState() {
  if (inflight) return;
  if (ws && ws.readyState === WebSocket.OPEN && Date.now() - lastPushAt < 3000) return; // push is healthy
  inflight = true;
  try {
    handlePayload(await api("/api/state"));
  } catch (_e) {
    $("status-line").textContent = "disconnected — retrying";
  } finally {
    inflight = false;
  }
}

/* Between pushes, keep clocks and countdowns moving from absolute server timestamps. */
function serverNowMs() { return Date.now() + clockState.offsetMs; }

function secondsUntil(iso, fallback) {
  const ms = Date.parse(iso);
  return Number.isFinite(ms) ? (ms - serverNowMs()) / 1000 : Number(fallback || 0);
}

function liveMarket(m) {
  if (!m) return m;
  return Object.assign({}, m, {
    seconds_remaining: Math.max(0, secondsUntil(m.end_date, m.seconds_remaining)),
    entry_seconds_remaining: Math.max(0, secondsUntil(m.entry_deadline, m.entry_seconds_remaining)),
  });
}

/* The uptime badge has exactly ONE writer. It used to be written from two places with different
   time bases (a 100 ms local tick from started_at, and every push from the engine's rounded
   uptime_seconds), so it flipped between two values and widths on every update. Now it counts
   from started_at, never steps backwards when the clock offset jitters between pushes, and
   only touches the DOM when the visible text changes. */
let uptimeStartedAt = null;
let uptimeShown = 0;

function renderUptime() {
  if (!lastState) return;
  const started = Date.parse(lastState.started_at);
  let secs;
  if (Number.isFinite(started)) {
    secs = Math.max(0, Math.floor((serverNowMs() - started) / 1000));
    if (started !== uptimeStartedAt) { uptimeStartedAt = started; uptimeShown = secs; } // first sample or engine restart
    else uptimeShown = Math.max(uptimeShown, secs);
  } else {
    // started_at not known yet: fall back to the engine's own figure
    uptimeShown = Math.max(0, Math.floor(Number(lastState.uptime_seconds) || 0));
  }
  const label = uptimeShown + "s uptime";
  const node = $("badge-uptime");
  if (node.textContent !== label) node.textContent = label;
}

function tickClock() {
  if (!lastState || $("app").classList.contains("hidden")) return;
  $("server-time").textContent = "server " + new Date(serverNowMs()).toISOString().replace("T", " ").slice(0, 19) + "Z";
  renderUptime();
  if (lastState.market) renderMarket(liveMarket(lastState.market));
}
setInterval(tickClock, 100);

function badge(node, label, cls) {
  node.textContent = label;
  node.className = "badge" + (cls ? " " + cls : "");
}

function renderState(s, serverTime) {
  const mode = s.effective_mode || "PAPER";
  badge($("badge-mode"), mode, mode === "LIVE" ? "bad" : "warn");
  badge($("badge-health"), s.engine_healthy ? "ENGINE OK" : "ENGINE DOWN", s.engine_healthy ? "ok" : "bad");
  badge($("badge-live"), s.live_locked ? "LIVE LOCKED" : "LIVE ARMED", s.live_locked ? "ghost" : "bad");
  renderUptime();
  $("server-time").textContent = serverTime ? "server " + serverTime.replace("T", " ").slice(0, 19) + "Z" : "";
  $("status-line").textContent = [
    s.detail || "running",
    s.recovery ? "recovery=" + s.recovery.state : "",
    s.counters ? "decisions=" + text(s.counters.decisions, 0) + " errors=" + text(s.counters.errors, 0) : "",
  ].filter(Boolean).join("  ·  ");

  renderMarket(liveMarket(s.market));
  renderSignal(s.probability, s.features, s.regime, s.market);
  renderFeeds(s.feeds);
  renderBooks(s.books);
  renderPosition(s.position);
  renderRisk(s.risk);
  renderDecision(s.last_decision, s.position);
  renderAlert(s.risk, s.market);
  renderGate(s.live && s.live.gate);
  renderModeSwitch(s);
  renderAnalytics(s.analytics);
  renderPnl(s);
}

function renderMarket(m) {
  const node = $("market");
  if (!m) { kv(node, [["status", "waiting for market discovery"]]); return; }
  const rem = Number(m.seconds_remaining || 0);
  const entry = Number(m.entry_seconds_remaining || 0);
  kv(node, [
    ["slug", m.slug],
    ["state", m.state, m.state === "ACTIVE" ? "up" : ""],
    ["version / negRisk", text(m.version) + " / " + text(m.neg_risk)],
    ["event start", m.event_start],
    ["window end", m.end_date],
    ["seconds remaining", rem.toFixed(1), rem < 30 ? "warn" : ""],
    ["entry window remaining", entry.toFixed(1), entry <= 0 ? "down" : "up"],
    ["tick / min order", text(m.tick_size) + " / " + text(m.min_order_size)],
    ["fees", m.fees_enabled ? m.fee_rate : "disabled"],
    ["methodology", m.methodology],
    ["rules approved", m.rules_approved ? "yes" : "NO", m.rules_approved ? "up" : "down"],
    ["accepting orders", m.accepting_orders ? "yes" : "no"],
  ]);
}

function renderSignal(p, f, regime, market) {
  const card = $("prob-outcome").closest(".signal");
  if (card) card.classList.toggle("idle", !p);
  if (!p) {
    $("prob-outcome").textContent = "No signal";
    $("prob-outcome").className = "prob-outcome idle";
    $("prob-value").textContent = "";
    $("prob-bar").style.width = "0%";
    let note = "No estimate yet. It appears once the engine has enough BTC price data.";
    if (!market) note = "Waiting for a market to trade.";
    else if (Number(market.entry_seconds_remaining) <= 0) note = "The entry window for this market has closed. Estimates only run while it is open, so the next one comes with the next 5-minute market.";
    const box = $("signal");
    clear(box);
    box.appendChild(el("p", "idle-note", note));
    return;
  }
  const up = Number(p.p_up);
  const outcome = p.expected_outcome || (up >= 0.5 ? "UP" : "DOWN");
  $("prob-outcome").textContent = outcome;
  $("prob-outcome").className = "prob-outcome " + (outcome === "UP" ? "up" : "down");
  $("prob-value").textContent = (up * 100).toFixed(1) + "% UP";
  $("prob-bar").style.width = Math.max(0, Math.min(100, up * 100)) + "%";
  kv($("signal"), [
    ["confidence", p.confidence],
    ["uncertainty", text(p.uncertainty ? p.uncertainty.map((x) => Number(x).toFixed(3)).join(" – ") : null)],
    ["current TWAP", p.current_twap],
    ["opening reference", p.opening_reference],
    ["expected final TWAP", p.expected_final_twap],
    ["sigma / z", text(p.sigma) + " / " + text(p.z)],
    ["coverage", text(p.coverage_seconds) + "s"],
    ["signal quality", p.signal_quality],
    ["method", p.method],
    ["regime", regime ? regime.regime + " (" + Number(regime.confidence).toFixed(2) + ")" : "—"],
    ["BTC price", f ? f.btc_price : "—"],
    ["TWAP distance bps", f ? Number(f.twap_distance_bps || 0).toFixed(1) : "—"],
    ["MTF agreement", f ? Number(f.mtf_agreement || 0).toFixed(2) : "—"],
  ]);
}

/* Feed rows are built once per feed and then updated in place: only the price text changes on
   each push (no age/ms), and the DOM is touched only when a value actually changed. */
const feedRows = new Map();

function setNodeText(node, value) {
  if (node.textContent !== value) node.textContent = value;
}

function feedPrice(feed) {
  const raw = feed.last_value;
  if (raw === null || raw === undefined || raw === "") return "no value";
  const n = Number(raw);
  return Number.isFinite(n) ? n.toLocaleString(undefined, { maximumFractionDigits: 2 }) : String(raw);
}

/* Polymarket resolves BTC 5m markets on Chainlink, so Chainlink is the only feed shown here.
   The spot feeds (binance/coinbase) still run inside the engine; they are just not displayed. */
function isChainlinkFeed(feed) {
  return /chainlink/i.test(String(feed && feed.name));
}

function renderFeeds(allFeeds) {
  const node = $("feeds");
  const feeds = (allFeeds || []).filter(isChainlinkFeed);
  if (!feeds.length) {
    feedRows.clear();
    clear(node);
    node.appendChild(el("div", "muted small", "no Chainlink feed status"));
    return;
  }
  const names = new Set(feeds.map((f) => f.name));
  for (const [name, r] of feedRows) {
    if (!names.has(name)) { r.row.remove(); feedRows.delete(name); }
  }
  if (!feedRows.size) clear(node); // drop the "no feed status" placeholder
  for (const feed of feeds) {
    let r = feedRows.get(feed.name);
    if (!r) {
      const row = el("div", "feed");
      row.appendChild(el("span", null, feed.name));
      const price = el("span", "muted", "");
      const pill = el("span", "pill", "");
      row.appendChild(price);
      row.appendChild(pill);
      node.appendChild(row);
      r = { row, price, pill };
      feedRows.set(feed.name, r);
    }
    setNodeText(r.price, feedPrice(feed));
    const cls = "pill " + feed.health;
    if (r.pill.className !== cls) r.pill.className = cls;
    setNodeText(r.pill, String(feed.health));
    const tip = feed.detail || "";
    if (r.row.title !== tip) r.row.title = tip;
  }
}

function renderBooks(books) {
  const node = $("books");
  clear(node);
  if (!books || !Object.keys(books).length) { node.appendChild(el("div", "muted small", "no books")); return; }
  for (const [outcome, book] of Object.entries(books)) {
    const box = el("div", "book");
    box.appendChild(el("h4", outcome === "UP" ? "up" : "down", outcome));
    const rows = [
      ["bid", book.best_bid, "bid"],
      ["ask", book.best_ask, "ask"],
      ["spread", book.spread, ""],
      ["bid depth", book.bid_depth, ""],
      ["ask depth", book.ask_depth, ""],
      ["age", Math.max(0, Math.round(Number(book.age_ms) || 0)) + " ms", ""],
    ];
    for (const [k, v, cls] of rows) {
      const r = el("div", "row");
      r.appendChild(el("span", null, k));
      r.appendChild(el("span", cls, text(v)));
      box.appendChild(r);
    }
    node.appendChild(box);
  }
}

function renderPosition(p) {
  if (!p) { kv($("position"), [["status", "flat"]]); return; }
  const n = (v) => { const x = Number(v); return Number.isFinite(x) ? x : null; };
  const usdc = (v) => (n(v) === null ? "—" : n(v).toFixed(2) + " USDC");
  const pnl = n(p.unrealized_pnl);
  const roi = n(p.roi);
  kv($("position"), [
    ["side", p.outcome, p.outcome === "UP" ? "up" : "down"],
    ["invested", usdc(p.cost_basis)],
    ["entry price", p.avg_price],
    ["price now", p.mark],
    ["shares", p.shares],
    ["value now", usdc(p.value_now)],
    ["P&L", pnl === null ? "—" : (pnl >= 0 ? "+" : "") + pnl.toFixed(2) + " USDC" + (roi === null ? "" : " (" + (roi >= 0 ? "+" : "") + (roi * 100).toFixed(1) + "%)"), pnl !== null && pnl >= 0 ? "up" : "down"],
    ["status", p.status],
    ["market", p.market_id],
  ]);
}

function renderAlert(risk, market) {
  const box = $("alert");
  clear(box);
  const items = [];
  if (risk && risk.allowed === false) {
    const fix = ["Open risk", "#risk"];
    items.push(["Trading is blocked: " + text(risk.detail || risk.reason), fix]);
  }
  for (const [msg, [label, href]] of items) {
    const row = el("div", "alert-row");
    row.appendChild(el("span", null, msg));
    const a = el("a", null, label); a.href = href;
    row.appendChild(a);
    box.appendChild(row);
  }
  box.classList.toggle("hidden", items.length === 0);
}

function renderRisk(risk) {
  if (!risk) { kv($("risk"), [["status", "risk unavailable"]]); return; }
  kv($("risk"), [
    ["allowed", risk.allowed ? "YES" : "NO", risk.allowed ? "up" : "down"],
    ["reason", risk.reason],
    ["detail", risk.detail],
    ["daily PnL", risk.daily_pnl, Number(risk.daily_pnl) >= 0 ? "up" : "down"],
    ["daily loss limit", risk.daily_loss_limit],
    ["reserved", risk.reserved],
    ["open positions", risk.open_positions],
    ["consecutive losses", risk.consecutive_losses, Number(risk.consecutive_losses) > 0 ? "warn" : ""],
    ["day locked", risk.locked ? "YES" : "no", risk.locked ? "down" : ""],
    ["profit locked", risk.profit_locked ? "YES" : "no"],
    ["kill switch", risk.kill_switch ? "ENGAGED" : "off", risk.kill_switch ? "down" : "up"],
    ["kill reason", risk.kill_reason],
  ]);
}


/* Live profit/loss: closed trades (analytics) + today (risk) + the open position's mark-to-market. */
function renderPnl(s) {
  const box = $("pnl");
  if (!box) return;
  clear(box);
  const mode = String(s.effective_mode || "PAPER").toLowerCase();
  const perf = (s.analytics && s.analytics[mode]) || null;
  const num = (v) => { const n = Number(v); return Number.isFinite(n) ? n : 0; };
  const signed = (n) => (n >= 0 ? "+" : "") + n.toFixed(2);
  const cls = (n) => (n > 0 ? "up" : n < 0 ? "down" : "");

  const realized = perf ? num(perf.net_pnl) : 0;
  const unreal = s.position && s.position.unrealized_pnl !== null && s.position.unrealized_pnl !== undefined
    ? num(s.position.unrealized_pnl) : 0;
  const total = realized + unreal;
  const today = s.risk ? num(s.risk.daily_pnl) : 0;
  const wins = perf ? perf.wins : 0;
  const losses = perf ? perf.losses : 0;
  const closed = wins + losses + (perf ? perf.flats : 0);

  const top = el("div", "pnl-total");
  top.appendChild(el("span", "pnl-big " + cls(total), signed(total) + " USDC"));
  top.appendChild(el("span", "muted small", "total now (" + mode.toUpperCase() + ")"));
  box.appendChild(top);

  const grid = el("div", "pnl-grid");
  const cell = (label, value, tone) => {
    const c = el("div", "pnl-cell");
    c.appendChild(el("span", "muted small", label));
    c.appendChild(el("span", "pnl-val " + (tone || ""), value));
    grid.appendChild(c);
  };
  cell("Wins", String(wins), wins ? "up" : "");
  cell("Losses", String(losses), losses ? "down" : "");
  cell("Win rate", closed && perf && perf.win_rate !== null ? (perf.win_rate * 100).toFixed(0) + "%" : "—");
  cell("Today", signed(today), cls(today));
  cell("Closed P&L", signed(realized), cls(realized));
  cell("Open P&L", s.position ? signed(unreal) : "—", s.position ? cls(unreal) : "");
  box.appendChild(grid);
}

function fmtUsdt(v) {
  const n = Number(v);
  return Number.isFinite(n) ? n.toFixed(2) + " USDC" : "—";
}

function sideTag(parent, outcome) {
  const o = String(outcome || "").toUpperCase();
  parent.appendChild(el("span", "side " + (o === "UP" ? "up" : o === "DOWN" ? "down" : ""), o || "—"));
}

function renderDecision(d, position) {
  const box = $("decision");
  clear(box);
  box.classList.remove("decision");
  box.classList.add("dcard");
  if (!d) { box.textContent = "no decisions yet"; return; }
  const x = d.extra || {};
  const kind = String(d.decision || "").toUpperCase();

  // Headline: one plain sentence with side + USDC.
  const head = el("div", "dc-head");
  const badge = el("span", "dbadge tone-" + decisionTone(kind), kind.replace("_", " "));
  head.appendChild(badge);
  const line = el("span", "dc-line");
  if (kind === "TRADE") {
    line.appendChild(document.createTextNode("Bought "));
    sideTag(line, x.outcome);
    line.appendChild(document.createTextNode(" for " + fmtUsdt(x.usdt) + " · " + text(x.shares) + " shares @ " + text(x.price)));
  } else if (kind === "EXIT") {
    line.appendChild(document.createTextNode("Sold "));
    sideTag(line, x.outcome);
    line.appendChild(document.createTextNode(" for " + fmtUsdt(x.usdt) + " · " + text(x.shares) + " shares @ " + text(x.price)));
  } else if (kind === "SETTLE") {
    line.appendChild(document.createTextNode("Window ended: "));
    sideTag(line, x.outcome);
    line.appendChild(document.createTextNode(x.won ? " WON" : " LOST"));
  } else if (kind === "HOLD") {
    line.appendChild(document.createTextNode("Holding position, not selling yet"));
  } else {
    line.appendChild(document.createTextNode("No new trade"));
  }
  head.appendChild(line);
  box.appendChild(head);

  // Result numbers for exits and settlements.
  if ((kind === "EXIT" || kind === "SETTLE") && !isEmpty(x.pnl)) {
    const pnl = Number(x.pnl);
    const r = el("div", "dc-row");
    r.appendChild(el("span", "muted", "Result"));
    r.appendChild(el("span", pnl >= 0 ? "up" : "down", (pnl >= 0 ? "+" : "") + pnl.toFixed(2) + " USDC"));
    box.appendChild(r);
  }

  // Why, in plain words.
  const why = kind === "TRADE" || kind === "EXIT" || kind === "SETTLE" ? null : text(d.detail, "");
  if (why) {
    const r = el("div", "dc-why");
    r.appendChild(el("span", "muted", "Why "));
    r.appendChild(document.createTextNode(kind === "NO_TRADE" ? why : why));
    box.appendChild(r);
  }
  if (kind === "EXIT" && x.rule) {
    const r = el("div", "dc-why");
    r.appendChild(el("span", "muted", "Rule "));
    r.appendChild(document.createTextNode(String(x.rule).replace(/_/g, " ").toLowerCase()));
    box.appendChild(r);
  }

  // What is held right now (always shown while a position is open).
  const pos = el("div", "dc-pos");
  if (position && position.shares) {
    pos.appendChild(el("span", "muted", "Holding now "));
    sideTag(pos, position.outcome);
    const roi = position.roi !== null && position.roi !== undefined ? Number(position.roi) : null;
    pos.appendChild(document.createTextNode(
      " · " + text(position.shares) + " shares · cost " + fmtUsdt(position.cost_basis)
    ));
    if (roi !== null && Number.isFinite(roi)) {
      pos.appendChild(document.createTextNode(" · ROI "));
      pos.appendChild(el("span", roi >= 0 ? "up" : "down", (roi >= 0 ? "+" : "") + (roi * 100).toFixed(2) + "%"));
    }
  } else {
    pos.appendChild(el("span", "muted", "Holding now "));
    pos.appendChild(document.createTextNode("nothing (flat)"));
  }
  box.appendChild(pos);

  box.appendChild(el("div", "dc-time muted", text(d.ts, "").replace("T", " ").slice(0, 19)));
}

function renderGate(gate) {
  const list = $("gate-list");
  const meter = $("gate-meter");
  clear(list);
  if (!gate) {
    $("gate-summary").textContent = "not evaluated";
    if (meter) meter.style.width = "0%";
    return;
  }
  if (meter) meter.style.width = (gate.total ? Math.round((100 * gate.passed_count) / gate.total) : 0) + "%";
  $("gate-summary").textContent = gate.passed_count + "/" + gate.total + " passed" + (gate.passed ? " · READY" : "");
  $("gate-summary").className = gate.passed ? "muted up" : "muted";
  for (const c of gate.conditions) {
    const item = el("div", "gate-item " + (c.passed ? "pass" : "fail"));
    item.appendChild(el("span", "num", String(c.number)));
    item.appendChild(el("span", null, (c.passed ? "✓ " : "✗ ") + c.name));
    if (c.detail) item.appendChild(el("span", "detail", c.detail));
    list.appendChild(item);
  }
}

function stats(container, rows) {
  clear(container);
  const shown = rows.filter((r) => !isEmpty(r[1]));
  if (!shown.length) { container.appendChild(el("p", "idle-note", "No data yet.")); return; }
  for (const [label, value, cls, wide] of shown) {
    const tile = el("div", "stat" + (wide ? " wide" : ""));
    tile.appendChild(el("div", "stat-label", label));
    tile.appendChild(el("div", "stat-value" + (cls ? " " + cls : ""), text(value)));
    container.appendChild(tile);
  }
}

function renderAnalytics(a) {
  if (!a) { stats($("analytics"), [["status", "not loaded"]]); return; }
  const p = a.paper || {};
  const ok = a.validation && a.validation.passed;
  const fails = a.validation && a.validation.failures && a.validation.failures.length ? a.validation.failures.join("; ") : "none";
  stats($("analytics"), [
    ["Paper trades", p.trades],
    ["Net PnL", p.net_pnl, Number(p.net_pnl) >= 0 ? "up" : "down"],
    ["Win rate", p.win_rate !== null && p.win_rate !== undefined ? (Number(p.win_rate) * 100).toFixed(1) + "%" : "—"],
    ["Profit factor", p.profit_factor],
    ["Expectancy", p.expectancy],
    ["Max drawdown", p.max_drawdown],
    ["Sharpe per trade", p.sharpe_per_trade],
    ["Validation", a.validation ? (ok ? "Passed" : "Not passed") : "—", ok ? "up" : "warn"],
    ["Validation failures", fails, "", true],
  ]);
}

/* --------------------------------------------------------------- controls */
async function post(path, body) {
  return api(path, { method: "POST", body });
}

/* Action feedback. The status line is rewritten on every state push (every 500 ms), so a message
   written only there disappears almost at once; a short toast stays long enough to read. */
function toast(message) {
  const box = $("toasts");
  if (!box) return;
  const bad = /fail|refus|error|denied|disconnect/i.test(String(message));
  const node = el("div", "toast" + (bad ? " err" : ""), String(message));
  box.appendChild(node);
  while (box.children.length > 4) box.removeChild(box.firstChild);
  setTimeout(() => node.classList.add("leaving"), 3800);
  setTimeout(() => node.remove(), 4100);
}

function report(message) {
  $("status-line").textContent = message;
  toast(message);
}

$("kill-engage").addEventListener("click", async () => {
  try {
    await post("/api/control/kill", { phrase: $("kill-phrase").value, reason: $("kill-reason").value, action: "engage" });
    report("kill switch engaged");
    $("kill-phrase").value = "";
  } catch (err) { report("kill switch failed: " + err.message); }
});

$("kill-clear").addEventListener("click", async () => {
  try {
    await post("/api/control/kill", { phrase: $("clear-phrase").value, reason: "operator clear", action: "clear" });
    report("kill switch cleared");
    $("clear-phrase").value = "";
  } catch (err) { report("clear failed: " + err.message); }
});

$("reset-day").addEventListener("click", async () => {
  try {
    const res = await post("/api/control/reset-day", { phrase: $("reset-phrase").value, reason: "operator reset" });
    report(res && res.will_relock
      ? "day lock cleared, but daily P&L (" + res.daily_pnl + ") is still at the loss limit (-" + res.daily_loss_limit + ") so it will re-lock on the next entry check"
      : "day lock reset");
    $("reset-phrase").value = "";
  } catch (err) { report("reset failed: " + err.message); }
});

/* Opening the Go live page re-evaluates the gate silently; only the Evaluate button announces it. */
async function evaluateGate(announce) {
  try {
    const result = await api("/api/gate");
    renderGate(result.gate);
    if (announce) report("gate evaluated: " + result.gate.passed_count + "/" + result.gate.total);
  } catch (err) { report("gate evaluation failed: " + err.message); }
}

$("gate-refresh").addEventListener("click", () => evaluateGate(true));

$("activate").addEventListener("click", async () => {
  if (!confirm("Activate LIVE trading? Real funds may be used. All 23 gate conditions must pass.")) return;
  try {
    const result = await post("/api/live/activate", { phrase: $("activate-phrase").value });
    renderGate(result.gate);
    report(result.ok ? "LIVE activated" : "LIVE activation refused — see gate");
    $("activate-phrase").value = "";
  } catch (err) { report("activation failed: " + err.message); }
});

$("revoke").addEventListener("click", async () => {
  if (!confirm("Revoke LIVE and cancel all open orders?")) return;
  try {
    await post("/api/live/revoke", { phrase: $("revoke-phrase").value, reason: $("revoke-reason").value });
    report("LIVE revoked; cancel-all requested");
    $("revoke-phrase").value = "";
  } catch (err) { report("revoke failed: " + err.message); }
});

/* ---------------------------------------------------------------- mode switch (Settings)
   PAPER <-> LIVE from the Settings page. This is only a second front door to the Go live page's two
   actions: LIVE still needs every gate condition and the typed phrase, and the server re-checks both.
   `mode` is deliberately not writable through /api/config. */
const GATE_PENDING_KEYS = new Set(["explicit_activation", "final_confirmation"]); // satisfied by this very click
let lastModeState = null;
let modeBusy = false;

function renderModeSwitch(s) {
  lastModeState = s;
  const requested = s.mode || "PAPER";
  const effective = s.effective_mode || "PAPER";
  const pending = requested === "LIVE" && effective !== "LIVE";
  const state = $("mode-state");
  badge(state, pending ? "LIVE LOCKED → PAPER" : effective, effective === "LIVE" ? "bad" : pending ? "warn" : "ok");
  const paperBtn = $("mode-paper");
  const liveBtn = $("mode-live");
  paperBtn.setAttribute("aria-pressed", effective === "PAPER" ? "true" : "false");
  liveBtn.setAttribute("aria-pressed", effective === "LIVE" ? "true" : "false");
  paperBtn.disabled = modeBusy || requested === "PAPER";
  liveBtn.disabled = modeBusy || effective === "LIVE";
  const gate = s.live && s.live.gate;
  const gateText = gate ? "Live gate: " + gate.passed_count + "/" + gate.total + " conditions passed." : "Live gate not evaluated yet.";
  $("mode-hint").textContent = effective === "LIVE"
    ? "Trading real funds. " + gateText
    : pending ? "LIVE was requested but is locked, so the bot is running PAPER. " + gateText : gateText;
}

function failingGateNames(gate) {
  const failing = gate.conditions.filter((c) => !c.passed && !GATE_PENDING_KEYS.has(c.key));
  const shown = failing.slice(0, 3).map((c) => c.name + (c.detail ? " (" + c.detail + ")" : ""));
  const more = failing.length - shown.length;
  return { count: failing.length, text: shown.join("; ") + (more > 0 ? "; +" + more + " more (see Go live)" : "") };
}

async function switchModeBusy(fn) {
  if (modeBusy) return;
  modeBusy = true;
  if (lastModeState) renderModeSwitch(lastModeState);
  try { await fn(); } finally {
    modeBusy = false;
    if (lastModeState) renderModeSwitch(lastModeState);
  }
}

$("mode-live").addEventListener("click", () => switchModeBusy(async () => {
  try {
    const checked = await api("/api/gate");
    renderGate(checked.gate);
    const blocked = failingGateNames(checked.gate);
    if (blocked.count) {
      report("LIVE blocked: " + blocked.count + " gate condition(s) failing: " + blocked.text);
      return;
    }
    const phrase = window.prompt("Switching to LIVE trades REAL funds.\n\nType ACTIVATE LIVE TRADING to confirm.");
    if (phrase === null) return;
    const result = await post("/api/live/activate", { phrase });
    renderGate(result.gate);
    if (result.ok) { report("switched to LIVE"); return; }
    const refused = failingGateNames(result.gate);
    report("LIVE refused" + (refused.count ? ": " + refused.text : ": confirmation text did not match"));
  } catch (err) { report("switch to LIVE failed: " + err.message); }
}));

$("mode-paper").addEventListener("click", () => switchModeBusy(async () => {
  const openPosition = !!(lastModeState && lastModeState.position);
  const warning = "Switch to PAPER?\n\nLIVE is revoked and every open exchange order is cancelled."
    + (openPosition ? "\n\nThere is an OPEN position. It will NOT be managed or closed by the bot in PAPER mode: close it first, or manage it on Polymarket yourself." : "");
  if (!window.confirm(warning)) return;
  try {
    await post("/api/live/revoke", { phrase: "REVOKE LIVE", reason: "switched to PAPER from Settings" });
    report("switched to PAPER; cancel-all requested");
  } catch (err) { report("switch to PAPER failed: " + err.message); }
}));

$("hb-confirm").addEventListener("change", (e) => {
  $("hb-test").disabled = !e.target.checked;
});

$("hb-test").addEventListener("click", async () => {
  if (!confirm("This permanently arms a key-wide heartbeat cancel-all. Continue?")) return;
  try {
    const result = await post("/api/live/heartbeat-test", { phrase: $("hb-phrase").value });
    report(result.ok ? "heartbeat armed: " + JSON.stringify(result) : "heartbeat test refused: " + text(result.error));
    $("hb-phrase").value = "";
  } catch (err) { report("heartbeat test failed: " + err.message); }
});

/* ------------------------------------------------------------------- tabs */
document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
    document.querySelectorAll(".tab-panel").forEach((p) => p.classList.add("hidden"));
    tab.classList.add("active");
    $("tab-" + tab.dataset.tab).classList.remove("hidden");
    loadTab(tab.dataset.tab);
  });
});

function table(node, columns, rows) {
  clear(node);
  if (!rows.length) {
    const tb = el("tbody"), tr = el("tr");
    tr.appendChild(el("td", "empty", "Nothing here yet."));
    tb.appendChild(tr);
    node.appendChild(tb);
    return;
  }
  columns = columns.filter((c) => rows.some((r) => !isEmpty(c.value(r))));
  const thead = el("thead");
  const htr = el("tr");
  for (const c of columns) htr.appendChild(el("th", null, c.label));
  thead.appendChild(htr);
  node.appendChild(thead);
  const tbody = el("tbody");
  for (const row of rows) {
    const tr = el("tr");
    for (const c of columns) {
      const td = el("td", c.wrap ? "wrap" : null);
      let cell = text(c.value(row), "");
      if (/^\d{4}-\d\d-\d\dT/.test(cell)) cell = cell.replace("T", " ").slice(0, 19);
      td.textContent = cell;
      tr.appendChild(td);
    }
    if (row.__action) tr.appendChild(row.__action);
    tbody.appendChild(tr);
  }
  node.appendChild(tbody);
}

const CLEAR_NOTES = {
  decisions: "all decision logs",
  orders: "finished PAPER orders (open orders and LIVE orders are kept)",
  positions: "closed PAPER positions (open positions and LIVE positions are kept)",
  trades: "PAPER trades (LIVE trades are kept; this also resets paper-validation progress)",
};

const clearBtn = $("activity-clear");
if (clearBtn) {
  clearBtn.addEventListener("click", async () => {
    const active = document.querySelector(".tab.active");
    const tab = active ? active.dataset.tab : "decisions";
    if (!CLEAR_NOTES[tab]) return;
    try {
      const r = await post("/api/activity/clear", { action: tab });
      report("cleared " + r.removed + " " + tab + " row(s)");
      loadTab(tab);
    } catch (err) { report("clear failed: " + err.message); }
  });
}


/* Decision log: one detailed entry per decision (badge, key numbers, reason, full detail, raw features). */
function decisionTone(d) {
  const v = String(d || "").toUpperCase();
  if (v === "NO_TRADE" || v === "HOLD" || v === "SKIP") return "muted";
  if (v.includes("SELL") || v.includes("EXIT") || v.includes("CLOSE")) return "warn";
  return "up";
}

function decisionLog(node, rows) {
  clear(node);
  if (!rows.length) { node.appendChild(el("div", "empty", "Nothing here yet.")); return; }
  const fmtNum = (v, dp) => {
    if (isEmpty(v)) return null;
    const n = Number(v);
    return Number.isFinite(n) ? n.toFixed(dp) : String(v);
  };
  for (const x of rows) {
    const entry = el("div", "dentry tone-" + decisionTone(x.decision));
    const head = el("div", "dhead");
    head.appendChild(el("span", "dbadge", text(x.decision)));
    head.appendChild(el("span", "dreason", text(x.reason_code)));
    head.appendChild(el("span", "dtime muted", text(x.ts, "").replace("T", " ").slice(0, 19)));
    if (x.mode) head.appendChild(el("span", "dmode", x.mode));
    entry.appendChild(head);

    const chips = el("div", "dchips");
    const add = (label, value) => {
      if (isEmpty(value)) return;
      const c = el("span", "dchip");
      c.appendChild(el("span", "muted", label + " "));
      c.appendChild(document.createTextNode(value));
      chips.appendChild(c);
    };
    add("p", fmtNum(x.probability, 4));
    add("edge", fmtNum(x.edge, 4));
    add("price", fmtNum(x.price, 4));
    add("regime", x.regime);
    add("market", x.market_id);
    if (chips.childNodes.length) entry.appendChild(chips);

    if (!isEmpty(x.reason_detail)) entry.appendChild(el("div", "ddetail", x.reason_detail));

    const feats = x.features && typeof x.features === "object" ? x.features : {};
    const keys = Object.keys(feats);
    if (keys.length) {
      const box = el("details", "dfeat");
      box.appendChild(el("summary", null, "features (" + keys.length + ")"));
      const grid = el("div", "dfeat-grid");
      for (const k of keys) {
        const v = feats[k];
        grid.appendChild(el("span", "muted", k));
        grid.appendChild(el("span", null, v !== null && typeof v === "object" ? JSON.stringify(v) : text(v)));
      }
      box.appendChild(grid);
      entry.appendChild(box);
    }
    node.appendChild(entry);
  }
}

async function loadTab(name) {
  try {
    if (name === "decisions") {
      const r = await api("/api/decisions?limit=200");
      decisionLog($("tbl-decisions"), r.decisions);
    } else if (name === "orders") {
      const r = await api("/api/orders?limit=200");
      table($("tbl-orders"), [
        { label: "created", value: (x) => x.created_at },
        { label: "market", value: (x) => x.market_id },
        { label: "side", value: (x) => x.side },
        { label: "outcome", value: (x) => x.outcome },
        { label: "type", value: (x) => x.order_type },
        { label: "shares", value: (x) => x.shares },
        { label: "limit", value: (x) => x.limit_price },
        { label: "filled", value: (x) => x.filled_shares },
        { label: "avg", value: (x) => x.avg_price },
        { label: "fee", value: (x) => x.fee },
        { label: "status", value: (x) => x.status },
        { label: "error", value: (x) => x.error_code },
        { label: "mode", value: (x) => x.mode },
      ], r.orders);
    } else if (name === "positions") {
      const r = await api("/api/positions?limit=200");
      table($("tbl-positions"), [
        { label: "market", value: (x) => x.market_id },
        { label: "outcome", value: (x) => x.outcome },
        { label: "shares", value: (x) => x.shares },
        { label: "avg", value: (x) => x.avg_price },
        { label: "cost", value: (x) => x.cost_basis },
        { label: "realized", value: (x) => x.realized_pnl },
        { label: "status", value: (x) => x.status },
        { label: "opened", value: (x) => x.opened_at },
        { label: "closed", value: (x) => x.closed_at },
        { label: "mode", value: (x) => x.mode },
      ], r.positions);
    } else if (name === "trades") {
      const r = await api("/api/trades?limit=500");
      table($("tbl-trades"), [
        { label: "time", value: (x) => x.created_at },
        { label: "market", value: (x) => x.market_id },
        { label: "kind", value: (x) => x.kind },
        { label: "side", value: (x) => x.side },
        { label: "outcome", value: (x) => x.outcome },
        { label: "price", value: (x) => x.price },
        { label: "shares", value: (x) => x.shares },
        { label: "fee", value: (x) => x.fee },
        { label: "pnl", value: (x) => x.realized_pnl },
        { label: "mode", value: (x) => x.mode },
      ], r.trades);
    } else if (name === "rules") {
      const r = await api("/api/rules");
      const rows = (r.rules || []).map((rule) => {
        if (!rule.approved) {
          const td = el("td");
          const btn = el("button", "btn small", "Approve");
          btn.addEventListener("click", async () => {
            const phrase = prompt("Type APPROVE RULE to approve this resolution rule");
            if (phrase === null) return;
            try {
              await post("/api/rules/approve", { fingerprint: rule.fingerprint, phrase });
              report("rule approved");
              loadTab("rules");
            } catch (err) { report("approval failed: " + err.message); }
          });
          td.appendChild(btn);
          rule.__action = td;
        }
        return rule;
      });
      table($("tbl-rules"), [
        { label: "fingerprint", value: (x) => x.fingerprint },
        { label: "method", value: (x) => x.methodology },
        { label: "source", value: (x) => x.source },
        { label: "tie", value: (x) => x.tie_rule },
        { label: "precision", value: (x) => x.precision },
        { label: "rounding", value: (x) => x.rounding },
        { label: "window", value: (x) => x.window_seconds },
        { label: "approved", value: (x) => (x.approved ? "yes" : "NO") },
        { label: "approved by", value: (x) => x.approved_by },
      ], rows);
    } else if (name === "settings") {
      const r = await api("/api/config");
      fillSettingsForm(r.config);
      showAdvice(r.advice);
      setSettingsMsg("", "muted");
      showSettingsStorage(r.storage);
    }
  } catch (err) {
    report("tab load failed: " + err.message);
  }
}

/* Fill every Settings control from a config object. Used on page load and again after a save, so
   the form always shows what the server really stored. Settings that are not on this page (exit
   fine-tuning, latency limits, feed ages ...) keep their stored/default values; saving never touches them. */
function fillSettingsForm(c) {
  renderTradingSwitch(!!c.trading_enabled);
  $("cfg-tradesize").value = text(c.trade_size_usd, "");
  $("cfg-daily").value = text(c.daily_loss_limit_usd, "");
  $("cfg-strategy").value = c.strategy;
  $("cfg-prob").value = c.min_probability;
  $("cfg-tp").value = c.take_profit_roi;
  $("cfg-sl").value = c.stop_loss_roi;
  $("cfg-edge").value = c.min_net_edge;
  $("cfg-spread").value = c.max_spread;
  $("cfg-maxpos").value = c.max_active_positions;
  $("cfg-maxentries").value = c.max_entries_per_market;
}

/* Warn in plain words when the Trade size can never produce an order (it buys fewer shares than the
   exchange minimum). Without this the bot just shows endless NO_TRADE rows. */
function showAdvice(advice) {
  const node = $("cfg-advice");
  if (!node) return;
  const message = advice && !advice.ok ? advice.message : "";
  node.textContent = message || "";
  node.classList.toggle("hidden", !message);
}

function renderTradingSwitch(on) {
  const badge = $("trading-state");
  const btn = $("trading-toggle");
  badge.textContent = on ? "ON" : "OFF";
  badge.className = "badge " + (on ? "ok" : "bad");
  btn.textContent = on ? "Turn OFF" : "Turn ON";
  btn.className = "btn " + (on ? "danger" : "primary");
  btn.dataset.on = on ? "1" : "0";
}

$("trading-toggle").addEventListener("click", async () => {
  const turnOn = $("trading-toggle").dataset.on !== "1";
  if (turnOn && !window.confirm("Turn trading ON? The bot will be allowed to open new positions.")) return;
  try {
    const r = await post("/api/config", { patch: { trading_enabled: turnOn } });
    renderTradingSwitch(!!r.config.trading_enabled);
    report("trading " + (turnOn ? "ON" : "OFF"));
  } catch (err) {
    report("trading switch failed: " + err.message);
  }
});

$("settings-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const patch = {};
  const skipped = [];   // blank required boxes: left unchanged instead of sending 0 and failing the whole save
  const bad = [];

  // [config key, input id, label, converter to the stored unit]
  const required = [
    ["trade_size_usd", "cfg-tradesize", "Trade size", (v) => v],
    ["daily_loss_limit_usd", "cfg-daily", "Daily loss limit", (v) => v],
    ["min_probability", "cfg-prob", "Minimum probability", (v) => v],
    ["take_profit_roi", "cfg-tp", "Take profit", (v) => v],
    ["stop_loss_roi", "cfg-sl", "Stop loss", (v) => v],
    ["min_net_edge", "cfg-edge", "Minimum net edge", (v) => v],
    ["max_spread", "cfg-spread", "Maximum spread", (v) => v],
    ["max_active_positions", "cfg-maxpos", "Max open positions", (v) => v],
    ["max_entries_per_market", "cfg-maxentries", "Max entries per market", (v) => v],
  ];
  for (const [key, id, label, conv] of required) {
    const raw = $(id).value.trim();
    if (raw === "") { skipped.push(label); continue; }
    if (!Number.isFinite(Number(raw))) { bad.push(label); continue; }
    patch[key] = conv(raw);
  }
  if (bad.length) {
    const msg = "Not a number: " + bad.join(", ");
    setSettingsMsg(msg, "err");
    report("save failed: " + msg);
    return;
  }

  patch.strategy = $("cfg-strategy").value;

  try {
    const r = await post("/api/config", { patch });
    fillSettingsForm(r.config);   // show what the server really stored (normalised / clamped values)
    showAdvice(r.advice);
    showSettingsStorage(r.storage);
    if (skipped.length) {
      setSettingsMsg("Saved. Left unchanged (empty): " + skipped.join(", "), "saved");
    } else {
      setSettingsMsg("Saved", "saved");
    }
    report("configuration saved");
  } catch (err) {
    setSettingsMsg(err.message, "err");
    report("save failed: " + err.message);
  }
});

/* Say where saves go. Without a settings database the host may wipe runtime.json on restart. */
function showSettingsStorage(storage) {
  const node = $("settings-storage");
  if (!node || !storage) return;
  if (storage.durable) {
    node.textContent = "Saved to the database: settings survive restarts and redeploys.";
    node.className = "small saved";
  } else {
    node.textContent = "Saved to a local file only: a host without a disk loses them on restart. Set POLYBOT_DATABASE_URL to keep them.";
    node.className = "small err";
  }
}

/* Tell the operator when the form differs from what is stored. Values loaded by loadTab() are
   set from code, which fires no input event, so only real edits mark the form as changed. */
function setSettingsMsg(message, tone) {
  const node = $("settings-msg");
  node.textContent = message;
  node.className = "small " + (tone || "muted");
}
["input", "change"].forEach((evt) => $("settings-form").addEventListener(evt, () => setSettingsMsg("Unsaved changes", "dirty")));

$("analytics-refresh").addEventListener("click", async () => {
  try {
    const r = await api("/api/analytics");
    renderAnalytics(r.analytics);
    report("analytics refreshed");
  } catch (err) {
    report("analytics failed: " + err.message);
  }
});

document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    if (stateTimer) clearInterval(stateTimer);
    stateTimer = null;
    stopStream();
  } else if (!$("app").classList.contains("hidden")) {
    startState();
  }
});

/* ------------------------------------------------------------------ pages */
const PAGES = {
  overview: "Overview", risk: "Risk", golive: "Go live", activity: "Activity",
  analytics: "Analytics", rules: "Rules", settings: "Settings",
};

function route() {
  let page = location.hash.slice(1);
  if (!(page in PAGES)) page = "overview";
  document.querySelectorAll(".page").forEach((p) => p.classList.toggle("hidden", p.id !== "page-" + page));
  document.querySelectorAll(".nav-link").forEach((a) => {
    const on = a.dataset.page === page;
    a.classList.toggle("active", on);
    if (on) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  });
  document.title = "Polybot — " + PAGES[page].toLowerCase();
  if ($("app").classList.contains("hidden")) return;
  if (page === "activity") {
    const active = document.querySelector(".tab.active");
    loadTab(active ? active.dataset.tab : "decisions");
  } else if (page === "golive") {
    evaluateGate(false);
  } else if (page === "rules" || page === "settings") {
    loadTab(page);
  }
  window.scrollTo(0, 0);
}
window.addEventListener("hashchange", route);
route();

boot();
