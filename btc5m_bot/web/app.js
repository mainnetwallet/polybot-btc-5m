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
    const fix = risk.reason === "CAPITAL_UNSET" ? ["Set capital", "#settings"] : ["Open risk", "#risk"];
    items.push(["Trading is blocked: " + text(risk.detail || risk.reason), fix]);
  }
  if (market && market.rules_approved === false) items.push(["This market's resolution rules are not approved yet.", ["Approve rules", "#rules"]]);
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
    ["capital", risk.capital],
    ["daily PnL", risk.daily_pnl, Number(risk.daily_pnl) >= 0 ? "up" : "down"],
    ["daily loss limit", risk.daily_loss_limit],
    ["reserved", risk.reserved],
    ["open positions", risk.open_positions],
    ["consecutive losses", risk.consecutive_losses, Number(risk.consecutive_losses) > 0 ? "warn" : ""],
    ["cooldown until", risk.cooldown_until, risk.cooldown_until ? "warn" : ""],
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
      setSettingsMsg("", "muted");
      showSettingsStorage(r.storage);
    }
  } catch (err) {
    report("tab load failed: " + err.message);
  }
}

/* Fractions are shown as percentages. Exact, trimmed conversion: no silent rounding of a stored
   value (0.625 must show 62.5, not 63) and no float noise (0.07 must not show 7.000000000000001). */
function fractionToPct(v) { return String(+(Number(v) * 100).toFixed(4)); }
function pctToFraction(v) { return (Number(v) / 100).toFixed(6); }

/* Fill every Settings control from a config object. Used on page load and again after a save, so
   the form always shows what the server actually stored. */
function fillSettingsForm(c) {
  renderTradingSwitch(!!c.trading_enabled);
  $("cfg-capital").value = text(c.capital_usd, "");
  $("cfg-capfrombal").checked = !!c.capital_from_balance;
  $("cfg-tradesize").value = text(c.trade_size_usd, "");
  $("cfg-risk").value = fractionToPct(c.risk_per_trade_pct);
  $("cfg-daily").value = text(c.daily_loss_limit_usd, "");
  $("cfg-prob").value = c.min_probability;
  $("cfg-edge").value = c.min_net_edge;
  $("cfg-spread").value = c.max_spread;
  $("cfg-tp").value = c.take_profit_roi;
  $("cfg-dyntp").checked = !!c.dynamic_take_profit;
  $("cfg-dyntpmin").value = c.dynamic_tp_min_multiple;
  $("cfg-dyntpmax").value = c.dynamic_tp_max_multiple;
  $("cfg-sl").value = c.stop_loss_roi;
  $("cfg-momz").value = c.momentum_reversal_z;
  $("cfg-minhold").value = c.min_hold_seconds_before_reversal;
  $("cfg-liqdrop").value = fractionToPct(c.liquidity_exit_depth_drop_pct);
  $("cfg-liqspread").value = c.liquidity_exit_spread_multiple;
  $("cfg-strategy").value = c.strategy;
  $("cfg-rules").checked = !!c.require_rules_approval;
  loadAdvancedFields(c);
  loadFeatureFields(c);
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

/* Advanced settings: everything else that used to need .env / runtime.json edits. Built from this
   table so the form, the loader and the saver cannot drift apart. kind: num | bool | select.
   nullable: an empty box saves null (feature off). */
const ADV_FIELDS = [
  { key: "auto_approve_rules", label: "Auto-approve resolution rules (PAPER only)", kind: "bool" },
  { key: "strategy_mode", label: "Strategy mode", kind: "select", options: ["ACTIVE", "SHADOW", "OFF"] },
  { key: "daily_profit_lock_pct", label: "Daily profit lock (fraction of capital, empty = off)", kind: "num", step: "0.01", nullable: true },
  { key: "max_consecutive_losses", label: "Max consecutive losses (1-2)", kind: "num", step: "1" },
  { key: "cooldown_minutes", label: "Cooldown after losses (minutes)", kind: "num", step: "1" },
  { key: "entry_window_seconds", label: "Entry window (seconds, max 120)", kind: "num", step: "1" },
  { key: "max_slippage", label: "Max slippage", kind: "num", step: "0.001" },
  { key: "min_depth_multiple", label: "Min book depth (x trade size)", kind: "num", step: "0.1" },
  { key: "trailing_stop_pct", label: "Trailing stop (fraction)", kind: "num", step: "0.01" },
  { key: "time_exit_seconds_before_end", label: "Time exit (seconds before market end)", kind: "num", step: "1" },
  { key: "min_oracle_coverage_seconds", label: "Min oracle coverage (seconds)", kind: "num", step: "1" },
  { key: "max_data_age_ms", label: "Max BTC data age (ms)", kind: "num", step: "1" },
  { key: "max_book_age_ms", label: "Max order book age (ms)", kind: "num", step: "1" },
  { key: "max_ack_latency_ms", label: "Max order ack latency (ms, max 3000)", kind: "num", step: "1" },
  { key: "max_latency_p95_ms", label: "Max p95 latency (ms)", kind: "num", step: "1" },
  { key: "taker_delay_ms", label: "Paper taker delay (ms)", kind: "num", step: "1" },
  { key: "fee_buffer", label: "Fee buffer (USDC per share)", kind: "num", step: "0.001" },
  { key: "min_order_notional_usd", label: "Exchange minimum order (USDC, empty = share minimum)", kind: "num", step: "0.01", nullable: true },
];

function buildAdvancedFields() {
  const box = $("cfg-advanced");
  if (!box || box.dataset.built) return;
  box.dataset.built = "1";
  for (const f of ADV_FIELDS) {
    const isBool = f.kind === "bool";
    const label = document.createElement("label");
    label.className = isBool ? "toggle" : "field";
    const input = f.kind === "select" ? document.createElement("select") : document.createElement("input");
    input.id = "adv-" + f.key;
    if (isBool) input.type = "checkbox";
    else if (f.kind === "num") { input.type = "number"; input.step = f.step || "any"; }
    else for (const o of f.options) { const opt = document.createElement("option"); opt.value = o; opt.textContent = o; input.appendChild(opt); }
    if (isBool) {
      label.appendChild(input);
      label.appendChild(el("span", null, f.label));
    } else {
      // "Name (hint)" renders as a title with the hint as quieter small text.
      const m = /^(.*?)\s*\((.*)\)\s*$/.exec(f.label);
      const caption = el("span", "lbl", m ? m[1] : f.label);
      if (m) caption.appendChild(el("small", null, m[2]));
      label.appendChild(caption);
      label.appendChild(input);
    }
    box.appendChild(label);
  }
}

function loadAdvancedFields(c) {
  buildAdvancedFields();
  for (const f of ADV_FIELDS) {
    const input = $("adv-" + f.key);
    if (!input) continue;
    if (f.kind === "bool") input.checked = !!c[f.key];
    else input.value = c[f.key] === null || c[f.key] === undefined ? "" : String(c[f.key]);
  }
}

function collectAdvancedFields(patch, skipped) {
  for (const f of ADV_FIELDS) {
    const input = $("adv-" + f.key);
    if (!input) continue;
    if (f.kind === "bool") patch[f.key] = input.checked;
    else if (f.kind === "select") patch[f.key] = input.value;
    else {
      const v = input.value.trim();
      if (v === "") {
        if (f.nullable) patch[f.key] = null;
        else skipped.push(f.label.replace(/\s*\(.*\)\s*$/, ""));
      } else patch[f.key] = v;
    }
  }
}

/* Experimental feature switches (config.feature_modes). Built from the stored dict, so a flag that
   exists in the config is always editable here even if this list does not name it. The save sends
   the whole dict because the server replaces it, it does not merge it. */
const FEATURE_LABELS = {
  trailing_stop: "Trailing stop",
  partial_exits: "Partial exits",
  correlation_shadow: "Correlation shadow",
  ml_model: "ML model",
  feed_failover: "Feed failover",
};
let featureBase = {};

function loadFeatureFields(c) {
  const box = $("cfg-features");
  if (!box) return;
  featureBase = Object.assign({}, c.feature_modes || {});
  box.textContent = "";
  const keys = Object.keys(FEATURE_LABELS).concat(Object.keys(featureBase).filter((k) => !(k in FEATURE_LABELS)));
  for (const key of keys) {
    const label = document.createElement("label");
    label.className = "field";
    const select = document.createElement("select");
    select.id = "feat-" + key;
    select.dataset.key = key;
    for (const o of ["OFF", "SHADOW", "ACTIVE"]) {
      const opt = document.createElement("option");
      opt.value = o;
      opt.textContent = o;
      select.appendChild(opt);
    }
    select.value = featureBase[key] || "OFF";
    label.appendChild(el("span", "lbl", FEATURE_LABELS[key] || key));
    label.appendChild(select);
    box.appendChild(label);
  }
}

function collectFeatureFields(patch) {
  const box = $("cfg-features");
  if (!box || !box.children.length) return;
  const modes = Object.assign({}, featureBase);
  box.querySelectorAll("select").forEach((s) => { modes[s.dataset.key] = s.value; });
  patch.feature_modes = modes;
}

$("settings-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const patch = {};
  const skipped = [];   // blank required boxes: left unchanged instead of sending 0 and failing the whole save
  const bad = [];

  // [config key, input id, label, converter to the stored unit]
  const required = [
    ["risk_per_trade_pct", "cfg-risk", "Risk per trade", pctToFraction],
    ["daily_loss_limit_usd", "cfg-daily", "Daily loss limit", (v) => v],
    ["min_probability", "cfg-prob", "Minimum probability", (v) => v],
    ["min_net_edge", "cfg-edge", "Minimum net edge", (v) => v],
    ["max_spread", "cfg-spread", "Maximum spread", (v) => v],
    ["take_profit_roi", "cfg-tp", "Take profit", (v) => v],
    ["dynamic_tp_min_multiple", "cfg-dyntpmin", "Dynamic TP min multiple", (v) => v],
    ["dynamic_tp_max_multiple", "cfg-dyntpmax", "Dynamic TP max multiple", (v) => v],
    ["stop_loss_roi", "cfg-sl", "Stop loss", (v) => v],
    ["momentum_reversal_z", "cfg-momz", "Momentum reversal", (v) => v],
    ["min_hold_seconds_before_reversal", "cfg-minhold", "Minimum hold", (v) => v],
    ["liquidity_exit_depth_drop_pct", "cfg-liqdrop", "Liquidity depth drop", pctToFraction],
    ["liquidity_exit_spread_multiple", "cfg-liqspread", "Liquidity spread multiple", (v) => v],
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

  patch.dynamic_take_profit = $("cfg-dyntp").checked;
  patch.strategy = $("cfg-strategy").value;
  patch.require_rules_approval = $("cfg-rules").checked;
  patch.capital_from_balance = $("cfg-capfrombal").checked;
  const capital = $("cfg-capital").value.trim();
  patch.capital_usd = capital === "" ? null : capital;
  const tradeSize = $("cfg-tradesize").value.trim();
  patch.trade_size_usd = tradeSize === "" ? null : tradeSize;
  collectAdvancedFields(patch, skipped);
  collectFeatureFields(patch);

  try {
    const r = await post("/api/config", { patch });
    fillSettingsForm(r.config);   // show what the server really stored (normalised / clamped values)
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
