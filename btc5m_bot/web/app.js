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

function kv(container, rows) {
  clear(container);
  for (const [key, value, cls] of rows) {
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
  csrf = null;
  $("app").classList.add("hidden");
  $("login").classList.remove("hidden");
  $("login-error").textContent = "";
}

function showApp() {
  $("login").classList.add("hidden");
  $("app").classList.remove("hidden");
  route();
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
function startState() {
  if (stateTimer) clearInterval(stateTimer);
  refreshState();
  stateTimer = setInterval(refreshState, 2000);
}

async function refreshState() {
  try {
    const payload = await api("/api/state");
    lastState = payload.state;
    renderState(payload.state, payload.server_time);
  } catch (_e) {
    $("status-line").textContent = "disconnected — retrying";
  }
}

function badge(node, label, cls) {
  node.textContent = label;
  node.className = "badge" + (cls ? " " + cls : "");
}

function renderState(s, serverTime) {
  const mode = s.effective_mode || "PAPER";
  badge($("badge-mode"), mode, mode === "LIVE" ? "bad" : "warn");
  badge($("badge-health"), s.engine_healthy ? "ENGINE OK" : "ENGINE DOWN", s.engine_healthy ? "ok" : "bad");
  badge($("badge-live"), s.live_locked ? "LIVE LOCKED" : "LIVE ARMED", s.live_locked ? "ghost" : "bad");
  badge($("badge-uptime"), text(s.uptime_seconds, 0) + "s uptime", "ghost");
  $("server-time").textContent = serverTime ? "server " + serverTime.replace("T", " ").slice(0, 19) + "Z" : "";
  $("status-line").textContent = [
    s.detail || "running",
    s.recovery ? "recovery=" + s.recovery.state : "",
    s.counters ? "decisions=" + text(s.counters.decisions, 0) + " errors=" + text(s.counters.errors, 0) : "",
  ].filter(Boolean).join("  ·  ");

  renderMarket(s.market);
  renderSignal(s.probability, s.features, s.regime);
  renderFeeds(s.feeds);
  renderBooks(s.books);
  renderPosition(s.position);
  renderRisk(s.risk);
  renderDecision(s.last_decision);
  renderGate(s.live && s.live.gate);
  renderAnalytics(s.analytics);
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

function renderSignal(p, f, regime) {
  if (!p) {
    $("prob-outcome").textContent = "—";
    $("prob-value").textContent = "—";
    $("prob-bar").style.width = "0%";
    kv($("signal"), [["status", "no probability estimate"]]);
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

function renderFeeds(feeds) {
  const node = $("feeds");
  clear(node);
  if (!feeds || !feeds.length) { node.appendChild(el("div", "muted small", "no feed status")); return; }
  for (const feed of feeds) {
    const row = el("div", "feed");
    row.appendChild(el("span", null, feed.name));
    row.appendChild(el("span", "muted", text(feed.last_value, "") + " " + (feed.age_ms !== undefined ? feed.age_ms + "ms" : "")));
    const pill = el("span", "pill " + feed.health, feed.health);
    row.appendChild(pill);
    if (feed.detail) row.title = feed.detail;
    node.appendChild(row);
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
      ["age", book.age_ms + "ms", ""],
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
  kv($("position"), [
    ["outcome", p.outcome, p.outcome === "UP" ? "up" : "down"],
    ["shares", p.shares],
    ["avg price", p.avg_price],
    ["cost basis", p.cost_basis],
    ["mark", p.mark],
    ["ROI", p.roi !== null ? (Number(p.roi) * 100).toFixed(2) + "%" : "—", Number(p.roi) >= 0 ? "up" : "down"],
    ["status", p.status, p.status === "DUST" ? "warn" : ""],
    ["market", p.market_id],
  ]);
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

function renderDecision(d) {
  if (!d) { $("decision").textContent = "no decisions yet"; return; }
  const parts = [
    "[" + text(d.ts) + "] " + text(d.decision) + " · " + text(d.reason),
    text(d.detail),
  ];
  if (d.extra) parts.push(JSON.stringify(d.extra));
  $("decision").textContent = parts.join("\n");
}

function renderGate(gate) {
  const list = $("gate-list");
  clear(list);
  if (!gate) {
    $("gate-summary").textContent = "not evaluated";
    return;
  }
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

function renderAnalytics(a) {
  if (!a) { kv($("analytics"), [["status", "not loaded"]]); return; }
  const p = a.paper || {};
  kv($("analytics"), [
    ["paper trades", p.trades],
    ["net PnL", p.net_pnl, Number(p.net_pnl) >= 0 ? "up" : "down"],
    ["win rate", p.win_rate !== null && p.win_rate !== undefined ? (Number(p.win_rate) * 100).toFixed(1) + "%" : "—"],
    ["profit factor", p.profit_factor],
    ["expectancy", p.expectancy],
    ["max drawdown", p.max_drawdown],
    ["sharpe / trade", p.sharpe_per_trade],
    ["validation", a.validation ? (a.validation.passed ? "PASSED" : "NOT PASSED") : "—", a.validation && a.validation.passed ? "up" : "warn"],
    ["validation failures", a.validation && a.validation.failures && a.validation.failures.length ? a.validation.failures.join("; ") : "none"],
  ]);
}

/* --------------------------------------------------------------- controls */
async function post(path, body) {
  return api(path, { method: "POST", body });
}

function report(message) {
  $("status-line").textContent = message;
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
    await post("/api/control/reset-day", { phrase: $("reset-phrase").value, reason: "operator reset" });
    report("day lock reset");
    $("reset-phrase").value = "";
  } catch (err) { report("reset failed: " + err.message); }
});

$("gate-refresh").addEventListener("click", async () => {
  try {
    const result = await api("/api/gate");
    renderGate(result.gate);
    report("gate evaluated: " + result.gate.passed_count + "/" + result.gate.total);
  } catch (err) { report("gate evaluation failed: " + err.message); }
});

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
      td.textContent = text(c.value(row), "");
      tr.appendChild(td);
    }
    if (row.__action) tr.appendChild(row.__action);
    tbody.appendChild(tr);
  }
  node.appendChild(tbody);
}

async function loadTab(name) {
  try {
    if (name === "decisions") {
      const r = await api("/api/decisions?limit=200");
      table($("tbl-decisions"), [
        { label: "time", value: (x) => x.ts },
        { label: "decision", value: (x) => x.decision },
        { label: "reason", value: (x) => x.reason_code },
        { label: "p", value: (x) => x.probability },
        { label: "edge", value: (x) => x.edge },
        { label: "price", value: (x) => x.price },
        { label: "regime", value: (x) => x.regime },
        { label: "market", value: (x) => x.market_id },
        { label: "detail", value: (x) => x.reason_detail, wrap: true },
      ], r.decisions);
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
      const c = r.config;
      $("cfg-capital").value = text(c.capital_usd, "");
      $("cfg-risk").value = (Number(c.risk_per_trade_pct) * 100).toFixed(2);
      $("cfg-daily").value = (Number(c.daily_loss_limit_pct) * 100).toFixed(2);
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
      $("cfg-liqdrop").value = (Number(c.liquidity_exit_depth_drop_pct) * 100).toFixed(0);
      $("cfg-liqspread").value = c.liquidity_exit_spread_multiple;
      $("cfg-strategy").value = c.strategy;
      $("cfg-rules").checked = !!c.require_rules_approval;
    }
  } catch (err) {
    report("tab load failed: " + err.message);
  }
}

$("settings-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const patch = {
    risk_per_trade_pct: (Number($("cfg-risk").value) / 100).toFixed(4),
    daily_loss_limit_pct: (Number($("cfg-daily").value) / 100).toFixed(4),
    min_probability: $("cfg-prob").value,
    min_net_edge: $("cfg-edge").value,
    max_spread: $("cfg-spread").value,
    take_profit_roi: $("cfg-tp").value,
    dynamic_take_profit: $("cfg-dyntp").checked,
    dynamic_tp_min_multiple: $("cfg-dyntpmin").value,
    dynamic_tp_max_multiple: $("cfg-dyntpmax").value,
    stop_loss_roi: $("cfg-sl").value,
    momentum_reversal_z: $("cfg-momz").value,
    min_hold_seconds_before_reversal: $("cfg-minhold").value,
    liquidity_exit_depth_drop_pct: (Number($("cfg-liqdrop").value) / 100).toFixed(4),
    liquidity_exit_spread_multiple: $("cfg-liqspread").value,
    strategy: $("cfg-strategy").value,
    require_rules_approval: $("cfg-rules").checked,
  };
  const capital = $("cfg-capital").value.trim();
  patch.capital_usd = capital === "" ? null : capital;
  try {
    await post("/api/config", { patch });
    $("settings-msg").textContent = "saved";
    report("configuration saved");
  } catch (err) {
    $("settings-msg").textContent = err.message;
  }
});

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
  } else if (page === "rules" || page === "settings") {
    loadTab(page);
  }
  window.scrollTo(0, 0);
}
window.addEventListener("hashchange", route);
route();

boot();
