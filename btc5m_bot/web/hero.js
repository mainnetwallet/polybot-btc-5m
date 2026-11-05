/* Market hero: Polymarket-style header (price to beat, current price, countdown) with a live
   Chainlink chart. Display only: it reads the same state the rest of the dashboard gets. */
(function () {
  "use strict";

  const NS = "http://www.w3.org/2000/svg";
  const WINDOW_MS = 60 * 1000;        // the chart shows the last minute
  const RIGHT_PAD_MS = 3 * 1000;      // breathing room after "now"
  const KEEP_MS = 10 * 60 * 1000;     // history kept in memory
  const X_TICK_MS = 10 * 1000;
  const PAD = { t: 52, r: 66, b: 30 }; // top reserves room for the hover label

  const $ = (id) => document.getElementById(id);
  const usd = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const reduceMotion = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  /* ------------------------------------------------------------ formatting */
  function partsOf(fmt, ms) {
    const out = {};
    for (const p of fmt.formatToParts(new Date(ms))) out[p.type] = p.value;
    return out;
  }
  const etFmt = new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", month: "long", day: "numeric", hour: "numeric", minute: "2-digit", hour12: true });
  const dtFmt = new Intl.DateTimeFormat("en-US", { month: "short", day: "2-digit", year: "numeric", hour: "numeric", minute: "2-digit", second: "2-digit", hour12: true });
  const clockFmt = new Intl.DateTimeFormat("en-US", { hour: "numeric", minute: "2-digit", second: "2-digit", hour12: true });
  const localHm = new Intl.DateTimeFormat([], { hour: "2-digit", minute: "2-digit" });

  function windowLabel(startMs, endMs) {
    const a = partsOf(etFmt, startMs);
    const b = partsOf(etFmt, endMs);
    const start = a.hour + ":" + a.minute + (a.dayPeriod !== b.dayPeriod ? a.dayPeriod : "");
    return a.month + " " + a.day + ", " + start + "-" + b.hour + ":" + b.minute + b.dayPeriod + " ET";
  }
  function etClock(ms) {
    const p = partsOf(etFmt, ms);
    return p.hour + ":" + p.minute + " " + p.dayPeriod;
  }
  function dateTimeLabel(ms) {
    const p = partsOf(dtFmt, ms);
    return p.month + " " + p.day + ", " + p.year + " " + p.hour + ":" + p.minute + ":" + p.second + " " + p.dayPeriod;
  }
  function clockLabel(ms) {
    const p = partsOf(clockFmt, ms);
    return p.hour + ":" + p.minute + ":" + p.second + " " + p.dayPeriod;
  }
  const pad2 = (n) => String(n).padStart(2, "0");

  function niceStep(raw) {
    const pow = Math.pow(10, Math.floor(Math.log10(raw)));
    const f = raw / pow;
    return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10) * pow;
  }
  function axisLabel(v, step) {
    const d = step >= 1 ? 0 : step >= 0.1 ? 1 : 2;
    return "$" + v.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
  }
  const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

  /* ----------------------------------------------------------------- state */
  const S = {
    api: null, built: false, raf: 0,
    pts: [],            // [epoch ms, price], ascending
    off: null,          // server clock minus local clock
    market: null, beat: NaN, health: "",
    marketKey: "", past: [], pastTimer: 0,
    W: 0, H: 0, yLo: null, yHi: null,
    view: null, hoverT: null, lastDraw: 0,
  };
  let els = null;
  // Separate caches: a node can get both text and attributes (axis labels, hover readout). With one
  // shared map, setText stored a string where setAttr expected an object, and setAttr threw a
  // TypeError in strict mode, which aborted every draw() after the first y-axis label.
  const textCache = new WeakMap(); // element -> last text written, so unchanged nodes are not touched
  const attrCache = new WeakMap(); // element -> { attribute name -> last "name=value" written }

  function now() { return Date.now() + (S.off || 0); }

  function setText(node, value) {
    if (textCache.get(node) !== value) { textCache.set(node, value); node.textContent = value; }
  }
  function setAttr(node, name, value) {
    const key = name + "=" + value;
    const seen = attrCache.get(node) || {};
    if (seen[name] !== key) { seen[name] = key; attrCache.set(node, seen); node.setAttribute(name, value); }
  }

  /* ------------------------------------------------------------------ data */
  function addPoint(t, v) {
    const last = S.pts[S.pts.length - 1];
    if (last && t <= last[0]) return;
    S.pts.push([t, v]);
    const cutoff = t - KEEP_MS;
    if (S.pts[0][0] < cutoff) {
      let i = 0;
      while (i < S.pts.length && S.pts[i][0] < cutoff) i++;
      S.pts.splice(0, i);
    }
  }

  function mergePoints(incoming) {
    const seen = new Set(S.pts.map((p) => p[0]));
    let changed = false;
    for (const p of incoming) {
      if (Array.isArray(p) && Number.isFinite(p[0]) && Number.isFinite(p[1]) && !seen.has(p[0])) {
        S.pts.push([p[0], p[1]]);
        seen.add(p[0]);
        changed = true;
      }
    }
    if (changed) S.pts.sort((a, b) => a[0] - b[0]);
  }

  async function loadChart() {
    try {
      const r = await S.api("/api/chart?seconds=180");
      mergePoints((r && r.points) || []);
    } catch (_e) { /* the live feed fills it in anyway */ }
  }

  async function loadPast() {
    try {
      const r = await S.api("/api/resolutions?limit=10");
      S.past = (r && r.resolutions) || [];
      renderPast();
    } catch (_e) { /* keep what is shown */ }
  }

  function update(state, serverTime) {
    const srv = Date.parse(serverTime);
    if (Number.isFinite(srv)) {
      const off = srv - Date.now();
      S.off = S.off === null ? off : S.off + (off - S.off) * 0.2; // smooth network jitter
    }
    const feed = (state.feeds || []).find((f) => /chainlink/i.test(String(f && f.name)));
    if (feed) {
      const t = Date.parse(feed.last_tick_at);
      const v = Number(feed.last_value);
      if (Number.isFinite(t) && Number.isFinite(v) && feed.last_value !== null && feed.last_value !== "") addPoint(t, v);
      S.health = String(feed.health || "");
    }
    S.market = state.market || null;
    const beat = S.market ? S.market.price_to_beat : null;
    S.beat = beat === null || beat === undefined || beat === "" ? NaN : Number(beat);
    if (S.built) renderStatic();
  }

  /* ---------------------------------------------------------- static parts */
  function renderWindow(m) {
    els.wins.replaceChildren();
    if (!m) { setText(els.window, "Waiting for a market"); els.info.removeAttribute("title"); return; }
    const start = Date.parse(m.event_start);
    const end = Date.parse(m.end_date);
    if (!Number.isFinite(start) || !Number.isFinite(end)) return;
    setText(els.window, windowLabel(start, end));
    els.info.setAttribute("title", m.question || m.slug || "");
    for (let i = 0; i < 4; i++) {
      const pill = document.createElement("span");
      pill.className = "wpill" + (i === 0 ? " on" : "");
      if (i === 0) {
        const dot = document.createElement("i");
        dot.className = "live-dot";
        pill.appendChild(dot);
        pill.setAttribute("aria-current", "true");
      } else {
        pill.title = "Upcoming window";
      }
      pill.appendChild(document.createTextNode(etClock(end + i * 5 * 60 * 1000)));
      els.wins.appendChild(pill);
    }
  }

  function renderStatic() {
    const m = S.market;
    const key = m ? m.market_id + "|" + m.end_date : "";
    if (key !== S.marketKey) {
      S.marketKey = key;
      renderWindow(m);
      if (m) {
        loadPast();
        clearTimeout(S.pastTimer);
        S.pastTimer = setTimeout(loadPast, 10000); // the previous window settles just after the switch
      }
    }
    const last = S.pts.length ? S.pts[S.pts.length - 1][1] : NaN;
    setText(els.beat, Number.isFinite(S.beat) ? usd.format(S.beat) : "—");
    setText(els.now, Number.isFinite(last) ? usd.format(last) : "—");
    if (Number.isFinite(last) && Number.isFinite(S.beat)) {
      const d = last - S.beat;
      const a = Math.abs(d);
      setText(els.delta, (d >= 0 ? "▲ $" : "▼ $") + (a >= 10 ? Math.round(a).toLocaleString("en-US") : a.toFixed(2)));
      els.delta.classList.toggle("up", d >= 0);
      els.delta.classList.toggle("down", d < 0);
    } else {
      setText(els.delta, "");
    }
    const bad = S.health && S.health !== "HEALTHY";
    setText(els.src, "Source: Chainlink TWAP" + (bad ? " · " + S.health : ""));
    els.src.classList.toggle("bad", !!bad);
  }

  function renderCountdown() {
    let mins = "--";
    let secs = "--";
    let hot = false;
    const m = S.market;
    const end = m ? Date.parse(m.end_date) : NaN;
    if (Number.isFinite(end)) {
      const rem = Math.max(0, Math.ceil((end - now()) / 1000));
      mins = pad2(Math.floor(rem / 60));
      secs = pad2(rem % 60);
      hot = rem <= 60;
    }
    setText(els.min, mins);
    setText(els.sec, secs);
    els.countdown.classList.toggle("hot", hot);
  }

  function arrowDot(outcome) {
    const dot = document.createElement("span");
    const up = outcome === "UP";
    dot.className = "pdot " + (up ? "up" : "down");
    dot.innerHTML = up
      ? '<svg viewBox="0 0 12 12" aria-hidden="true"><path d="M6 2.6 10 8.8H2z"/></svg>'
      : '<svg viewBox="0 0 12 12" aria-hidden="true"><path d="M6 9.4 2 3.2h8z"/></svg>';
    dot.title = up ? "Up" : "Down";
    return dot;
  }

  function renderPast() {
    const rows = S.past
      .map((r) => ({ r, out: String(r.actual_outcome || r.simulated_outcome || "").toUpperCase() }))
      .filter((x) => x.out === "UP" || x.out === "DOWN");
    els.dots.replaceChildren();
    rows.slice(0, 4).reverse().forEach((x) => els.dots.appendChild(arrowDot(x.out))); // oldest on the left
    els.pop.replaceChildren();
    if (!rows.length) {
      const empty = document.createElement("div");
      empty.className = "past-empty";
      empty.textContent = "No settled windows yet.";
      els.pop.appendChild(empty);
      return;
    }
    for (const { r, out } of rows) {
      const row = document.createElement("div");
      row.className = "past-row";
      const when = document.createElement("span");
      when.className = "past-when";
      const created = Date.parse(r.created_at);
      when.textContent = Number.isFinite(created) ? localHm.format(new Date(created)) : "";
      const prices = document.createElement("span");
      prices.className = "past-px";
      const o = Number(r.opening_ref);
      const c = Number(r.closing_ref);
      prices.textContent = Number.isFinite(o) && Number.isFinite(c) ? usd.format(o) + " → " + usd.format(c) : "";
      row.appendChild(when);
      row.appendChild(arrowDot(out));
      row.appendChild(prices);
      els.pop.appendChild(row);
    }
  }

  /* ----------------------------------------------------------------- chart */
  function svgEl(tag, cls, attrs) {
    const n = document.createElementNS(NS, tag);
    if (cls) n.setAttribute("class", cls);
    if (attrs) for (const k in attrs) n.setAttribute(k, attrs[k]);
    return n;
  }

  function buildChart() {
    const svg = els.svg;
    const defs = svgEl("defs");
    const grad = svgEl("linearGradient", null, { id: "hc-fill", x1: "0", y1: "0", x2: "0", y2: "1" });
    grad.appendChild(svgEl("stop", "hc-stop-top", { offset: "0" }));
    grad.appendChild(svgEl("stop", "hc-stop-bot", { offset: "1" }));
    const clip = svgEl("clipPath", null, { id: "hc-clip" });
    const clipRect = svgEl("rect");
    clip.appendChild(clipRect);
    defs.appendChild(grad);
    defs.appendChild(clip);
    svg.appendChild(defs);

    const c = {
      clipRect,
      grid: svgEl("g"),
      xlabels: svgEl("g"),
      targetLine: svgEl("line", "hc-target-line"),
      nowLine: svgEl("line", "hc-now-line"),
      body: svgEl("g", null, { "clip-path": "url(#hc-clip)" }),
      area: svgEl("path", "hc-area", { fill: "url(#hc-fill)" }),
      line: svgEl("path", "hc-line"),
      end: svgEl("g", "hc-end"),
      ring: svgEl("circle", "hc-ring", { r: "9" }),
      dot: svgEl("circle", "hc-dot", { r: "3.4" }),
      pill: svgEl("g", "hc-pill"),
      pillBg: svgEl("rect", "hc-pill-bg", { height: "22", rx: "4" }),
      pillTx: svgEl("text", "hc-pill-tx"),
      chev: svgEl("path", "hc-chev"),
      hover: svgEl("g", "hc-hover hidden"),
      hLine: svgEl("line", "hc-h-line"),
      hDot: svgEl("circle", "hc-h-dot", { r: "4.5" }),
      hIcon: svgEl("g", "hc-h-icon"),
      hPrice: svgEl("text", "hc-h-price"),
      hTime: svgEl("text", "hc-h-time"),
      hit: svgEl("rect", "hc-hit"),
      yLabels: [], yLines: [], xTexts: new Map(),
    };
    c.pillTx.textContent = "Target";
    c.body.appendChild(c.area);
    c.body.appendChild(c.line);
    c.end.appendChild(c.ring);
    c.end.appendChild(c.dot);
    c.pill.appendChild(c.pillBg);
    c.pill.appendChild(c.pillTx);
    c.pill.appendChild(c.chev);
    const iconBg = svgEl("circle", "hc-h-icon-bg", { cx: "8", cy: "8", r: "8" });
    const iconUse = svgEl("use", null, { href: "#btc-glyph", x: "0", y: "0", width: "16", height: "16" });
    c.hIcon.appendChild(iconBg);
    c.hIcon.appendChild(iconUse);
    c.hover.appendChild(c.hLine);
    c.hover.appendChild(c.hDot);
    c.hover.appendChild(c.hIcon);
    c.hover.appendChild(c.hPrice);
    c.hover.appendChild(c.hTime);
    [c.grid, c.targetLine, c.nowLine, c.body, c.end, c.xlabels, c.pill, c.hover, c.hit].forEach((n) => svg.appendChild(n));

    c.hit.addEventListener("pointermove", onPointer);
    c.hit.addEventListener("pointerdown", onPointer);
    c.hit.addEventListener("pointerleave", () => { S.hoverT = null; });
    c.hit.addEventListener("pointercancel", () => { S.hoverT = null; });
    els.c = c;
  }

  function onPointer(e) {
    const v = S.view;
    if (!v) return;
    const x = e.clientX - els.svg.getBoundingClientRect().left;
    const t = v.tMin + ((x - v.plotL) / (v.plotR - v.plotL)) * (v.tMax - v.tMin);
    let best = null;
    let bd = Infinity;
    for (const p of S.pts) {
      if (p[0] < v.tMin) continue;
      const d = Math.abs(p[0] - t);
      if (d < bd) { bd = d; best = p; } else if (p[0] > t) break;
    }
    S.hoverT = best ? best[0] : null;
  }

  function sizeChart() {
    const r = els.svg.getBoundingClientRect();
    S.W = Math.round(r.width);
    S.H = Math.round(r.height);
    if (!S.W || !S.H) return;
    els.svg.setAttribute("viewBox", "0 0 " + S.W + " " + S.H);
    els.c.hit.setAttribute("x", "0");
    els.c.hit.setAttribute("y", String(PAD.t));
    els.c.hit.setAttribute("width", String(S.W - PAD.r));
    els.c.hit.setAttribute("height", String(S.H - PAD.t - PAD.b));
  }

  function ensure(list, parent, tag, cls, n) {
    while (list.length < n) { const node = svgEl(tag, cls); parent.appendChild(node); list.push(node); }
    for (let i = 0; i < list.length; i++) list[i].classList.toggle("hidden", i >= n);
  }

  function draw() {
    renderCountdown();
    const c = els.c;
    const W = S.W;
    const H = S.H;
    if (!W || !H) return;
    const plotL = 0;
    const plotR = W - PAD.r;
    const plotT = PAD.t;
    const plotB = H - PAD.b;
    const have = S.pts.length > 0;
    els.empty.classList.toggle("hidden", have);
    els.svg.classList.toggle("hidden", !have);
    if (!have) return;

    const tMax = now() + RIGHT_PAD_MS;
    const tMin = tMax - WINDOW_MS;
    S.view = { tMin, tMax, plotL, plotR };

    // visible points, plus the one just before the left edge so the line enters smoothly
    let first = 0;
    while (first < S.pts.length && S.pts[first][0] < tMin) first++;
    if (first > 0) first--;
    const vis = S.pts.slice(first);
    const lastPt = S.pts[S.pts.length - 1];

    // y range from what is on screen; the target joins the range only when it is close
    let lo = Infinity;
    let hi = -Infinity;
    for (const p of vis) {
      if (p[0] >= tMin) { if (p[1] < lo) lo = p[1]; if (p[1] > hi) hi = p[1]; }
    }
    if (!Number.isFinite(lo)) { lo = hi = lastPt[1]; }
    lo = Math.min(lo, lastPt[1]);
    hi = Math.max(hi, lastPt[1]);
    if (hi - lo < 4) { const mid = (hi + lo) / 2; lo = mid - 2; hi = mid + 2; }
    const span = hi - lo;
    let tLo = lo - span * 0.18;
    let tHi = hi + span * 0.18;
    if (Number.isFinite(S.beat) && S.beat >= lo - span && S.beat <= hi + span) {
      tLo = Math.min(tLo, S.beat - span * 0.1);
      tHi = Math.max(tHi, S.beat + span * 0.1);
    }
    if (S.yLo === null) { S.yLo = tLo; S.yHi = tHi; }
    else { S.yLo += (tLo - S.yLo) * 0.14; S.yHi += (tHi - S.yHi) * 0.14; }
    const yLo = S.yLo;
    const yHi = S.yHi;

    const X = (t) => plotL + ((t - tMin) / (tMax - tMin)) * (plotR - plotL);
    const Y = (v) => plotB - ((v - yLo) / (yHi - yLo)) * (plotB - plotT);

    setAttr(c.clipRect, "x", plotL);
    setAttr(c.clipRect, "y", plotT - 4);
    setAttr(c.clipRect, "width", plotR - plotL);
    setAttr(c.clipRect, "height", plotB - plotT + 8);

    // horizontal grid and price labels
    const step = niceStep((yHi - yLo) / 3);
    const ticks = [];
    for (let v = Math.ceil(yLo / step) * step; v <= yHi + 1e-9 && ticks.length < 8; v += step) ticks.push(Math.round(v / step) * step);
    ensure(c.yLines, c.grid, "line", "hc-grid", ticks.length);
    ensure(c.yLabels, c.grid, "text", "hc-ylabel", ticks.length);
    ticks.forEach((v, i) => {
      const y = Y(v);
      setAttr(c.yLines[i], "x1", plotL);
      setAttr(c.yLines[i], "x2", plotR);
      setAttr(c.yLines[i], "y1", y);
      setAttr(c.yLines[i], "y2", y);
      setAttr(c.yLabels[i], "x", W - 4);
      setAttr(c.yLabels[i], "y", y + 4);
      setText(c.yLabels[i], axisLabel(v, step));
    });

    // line and area
    let d = "";
    vis.forEach((p, i) => { d += (i ? "L" : "M") + X(p[0]).toFixed(1) + " " + Y(p[1]).toFixed(1); });
    const x0 = X(vis[0][0]).toFixed(1);
    const xN = X(lastPt[0]).toFixed(1);
    setAttr(c.line, "d", d);
    setAttr(c.area, "d", d + "L" + xN + " " + plotB + "L" + x0 + " " + plotB + "Z");

    // current-price guide and the end dot
    const yNow = Y(lastPt[1]);
    setAttr(c.nowLine, "x1", plotL);
    setAttr(c.nowLine, "x2", plotR);
    setAttr(c.nowLine, "y1", yNow);
    setAttr(c.nowLine, "y2", yNow);
    setAttr(c.end, "transform", "translate(" + xN + " " + yNow.toFixed(1) + ")");

    // target: a dashed guide plus a pill; pinned to an edge with chevrons when it is off the chart
    const hasBeat = Number.isFinite(S.beat);
    c.targetLine.classList.toggle("hidden", !hasBeat);
    c.pill.classList.toggle("hidden", !hasBeat);
    if (hasBeat) {
      const yT = Y(S.beat);
      const inside = yT >= plotT + 2 && yT <= plotB - 2;
      const yc = clamp(yT, plotT + 24, plotB - 24);
      const pw = inside ? 62 : 80;
      setAttr(c.targetLine, "x1", plotL);
      setAttr(c.targetLine, "x2", plotR);
      setAttr(c.targetLine, "y1", yc);
      setAttr(c.targetLine, "y2", yc);
      setAttr(c.pillBg, "width", pw);
      setAttr(c.pillBg, "y", -11);
      setAttr(c.pillTx, "x", 12);
      setAttr(c.pillTx, "y", 4);
      setAttr(c.pill, "transform", "translate(" + (W - pw - 2) + " " + yc.toFixed(1) + ")");
      if (inside) {
        setAttr(c.chev, "d", "");
      } else {
        const down = yT > plotB;
        const cy = down ? -2.5 : 2.5;
        const dy = down ? 3.5 : -3.5;
        const cx = pw - 22;
        setAttr(c.chev, "d", "M" + cx + " " + cy + "l4 " + dy + "l4 " + -dy + "M" + cx + " " + (cy + (down ? 4 : -4)) + "l4 " + dy + "l4 " + -dy);
      }
    }

    // time labels every 10 s, aligned to the clock
    const wanted = new Set();
    const tickMs = W < 520 ? X_TICK_MS * 2 : X_TICK_MS;
    for (let t = Math.ceil(tMin / tickMs) * tickMs; t <= tMax; t += tickMs) {
      const x = X(t);
      if (x < 30 || x > plotR - 24) continue;
      wanted.add(t);
      let node = c.xTexts.get(t);
      if (!node) {
        node = svgEl("text", "hc-xlabel");
        node.textContent = clockLabel(t);
        c.xlabels.appendChild(node);
        c.xTexts.set(t, node);
      }
      setAttr(node, "x", x.toFixed(1));
      setAttr(node, "y", H - 8);
    }
    for (const [t, node] of c.xTexts) {
      if (!wanted.has(t)) { node.remove(); c.xTexts.delete(t); }
    }

    // hover readout, pinned to the data point under the pointer
    let hp = null;
    if (S.hoverT !== null) hp = vis.find((p) => p[0] === S.hoverT) || null;
    const hx = hp ? X(hp[0]) : NaN;
    const show = !!hp && hx >= plotL && hx <= plotR;
    c.hover.classList.toggle("hidden", !show);
    if (show) {
      const hy = Y(hp[1]);
      const price = usd.format(hp[1]);
      const groupW = 16 + 6 + price.length * 7.6;
      const cx = clamp(hx, groupW / 2 + 4, plotR - 76);
      setAttr(c.hLine, "x1", hx);
      setAttr(c.hLine, "x2", hx);
      setAttr(c.hLine, "y1", plotT - 4);
      setAttr(c.hLine, "y2", plotB);
      setAttr(c.hDot, "cx", hx);
      setAttr(c.hDot, "cy", hy);
      setAttr(c.hIcon, "transform", "translate(" + (cx - groupW / 2).toFixed(1) + " 6)");
      setAttr(c.hPrice, "x", (cx - groupW / 2 + 22).toFixed(1));
      setAttr(c.hPrice, "y", 19);
      setText(c.hPrice, price);
      setAttr(c.hTime, "x", cx.toFixed(1));
      setAttr(c.hTime, "y", 38);
      setText(c.hTime, dateTimeLabel(hp[0]));
    }
  }

  /* ------------------------------------------------------------------ boot */
  function build() {
    els = {
      hero: $("hero"), svg: $("hero-svg"), empty: $("hero-empty"), src: $("hero-src"),
      window: $("hero-window"), info: $("hero-info"), countdown: $("hero-countdown"),
      min: $("cd-min"), sec: $("cd-sec"), beat: $("px-beat"), now: $("px-now"), delta: $("px-delta"),
      wins: $("hero-wins"), dots: $("past-dots"), pop: $("past-pop"), pastBtn: $("past-btn"), pastWrap: $("past-wrap"),
    };
    if (!els.hero || !els.svg) return false;
    buildChart();
    new ResizeObserver(sizeChart).observe(els.svg);
    sizeChart();

    const closePast = () => { els.pop.classList.add("hidden"); els.pastBtn.setAttribute("aria-expanded", "false"); };
    els.pastBtn.addEventListener("click", () => {
      const open = els.pop.classList.toggle("hidden") === false;
      els.pastBtn.setAttribute("aria-expanded", String(open));
    });
    document.addEventListener("click", (e) => { if (!els.pastWrap.contains(e.target)) closePast(); });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") closePast(); });
    renderPast();
    return true;
  }

  function frame(ts) {
    S.raf = requestAnimationFrame(frame);
    if (document.hidden || !els.hero.offsetParent) return; // other page or tab hidden
    if (reduceMotion && ts - S.lastDraw < 500) return;
    S.lastDraw = ts;
    draw();
  }

  function start(api) {
    S.api = api;
    if (!S.built) {
      S.built = build();
      if (!S.built) return;
      renderStatic();
      S.raf = requestAnimationFrame(frame);
    }
    loadChart();
    loadPast();
  }

  window.Hero = { start, update };
})();