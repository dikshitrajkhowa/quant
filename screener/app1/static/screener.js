/* Screener page - depends on app.js (shared utilities) and Plotly. */
"use strict";

const state = {
  meta: null,            // {universes, strategies}
  strategies: {},        // key -> meta
  data: null,            // last /api/screen response
  sort: { col: null, desc: false },
  activeTab: "ranking",
  dirty: new Set(),      // tabs that need re-rendering
  screenSeq: 0,
};
const STORAGE_KEY = "nse-screener-settings-v1";

/* ------------------------------------------------------------------ */
/* Utilities                                                           */
/* ------------------------------------------------------------------ */
function fmtCell(col, v) {
  if (v === null || v === undefined) return "–";
  if (typeof v === "boolean") return v ? "✓" : "—";
  if (state.data?.price_cols.includes(col)) return inr(v);
  if (col.includes("%")) return Number(v).toFixed(2) + "%";
  if (col.includes("Ratio")) return Number(v).toFixed(2) + "×";
  if (col === "Avg Vol (20D)") return Number(v).toLocaleString("en-IN");
  if (col.startsWith("Days")) return Number(v).toFixed(0);
  if (typeof v === "number") return Number.isInteger(v) ? String(v) : v.toFixed(2);
  return String(v);
}

function showProgress(label, frac) {
  $("#progress").hidden = false;
  $("#progress-label").textContent = label;
  const bar = $("#progress-bar");
  if (frac === null) { bar.classList.add("indeterminate"); bar.style.width = ""; }
  else { bar.classList.remove("indeterminate"); bar.style.width = Math.round(frac * 100) + "%"; }
}
const hideProgress = () => { $("#progress").hidden = true; };

/* ------------------------------------------------------------------ */
/* Settings / sidebar                                                  */
/* ------------------------------------------------------------------ */
function loadSaved() { try { return JSON.parse(localStorage.getItem(STORAGE_KEY)) || {}; } catch { return {}; } }
function save() {
  try {
    const s = loadSaved();
    s.strategy = $("#strategy").value; s.universe = $("#universe").value; s.source = $("#source").value;
    s.params = s.params || {}; s.params[s.strategy] = currentParams();
    for (const id of ["top_n", "min_price", "min_avg_volume", "min_history", "chart_days", "limit", "batch_size"]) s[id] = $("#" + id).value;
    localStorage.setItem(STORAGE_KEY, JSON.stringify(s));
  } catch { /* storage unavailable */ }
}

function renderParams() {
  const s = state.strategies[$("#strategy").value];
  $("#strategy-desc").textContent = s.description;
  const saved = (loadSaved().params || {})[s.key] || {};
  const box = $("#params"); box.innerHTML = "";
  for (const p of s.params) {
    const val = saved[p.key] ?? p.default;
    const wrap = document.createElement("label");
    if (p.kind === "bool") {
      wrap.className = "field toggle";
      wrap.innerHTML = `<span>${p.label}</span><span class="switch"><input type="checkbox" data-param="${p.key}"><span class="slider"></span></span>`;
      wrap.querySelector("input").checked = !!val;
    } else {
      wrap.className = "field";
      wrap.innerHTML = `<span>${p.label} <output></output></span><input type="range" data-param="${p.key}" min="${p.min}" max="${p.max}" step="${p.step}">`;
      const input = wrap.querySelector("input"), out = wrap.querySelector("output");
      input.value = val;
      const show = () => { out.textContent = p.kind === "int" ? input.value : Number(input.value).toFixed(p.step < 1 ? (String(p.step).split(".")[1] || "").length : 0); };
      show(); input.addEventListener("input", show);
    }
    if (p.help) wrap.title = p.help;
    box.appendChild(wrap);
  }
  box.querySelectorAll("[data-param]").forEach((el) => el.addEventListener(el.type === "range" ? "change" : "input", onSettingChange));
}

function currentParams() {
  const out = {};
  document.querySelectorAll("#params [data-param]").forEach((el) => {
    out[el.dataset.param] = el.type === "checkbox" ? el.checked : Number(el.value);
  });
  return out;
}

function screenRequest() {
  return {
    universe: $("#universe").value, source: $("#source").value, strategy: $("#strategy").value, params: currentParams(),
    min_price: +$("#min_price").value || 0, min_avg_volume: +$("#min_avg_volume").value || 0,
    min_history: +$("#min_history").value || 200, top_n: +$("#top_n").value, chart_days: +$("#chart_days").value,
  };
}

const statusUrl = () => `/api/data/status?universe=${$("#universe").value}&source=${$("#source").value}`;
const sourceName = () => state.meta?.providers?.[$("#source").value]?.name || "Yahoo Finance";

function renderSourceHint() {
  const prov = state.meta?.providers || {};
  const p = prov[$("#source").value];
  const missing = Object.values(prov).filter((x) => !x.available);
  let html = p ? esc(p.note) : "";
  if ($("#source").value === "tradingview") html += " Use Nifty 500 or smaller for speed.";
  if (missing.some((x) => x.outdated)) html += `<br><b>TradingView is unavailable because the server is running old code — stop it and run <code>python main.py</code> again.</b>`;
  else if (missing.length) html += `<br>TradingView not installed — run <code>pip install tvkit</code> and restart the server.`;
  $("#source-hint").innerHTML = html;
}

/* ------------------------------------------------------------------ */
/* Data loading + screening                                            */
/* ------------------------------------------------------------------ */
async function ensureData(force = false) {
  const universe = $("#universe").value, source = $("#source").value;
  let st = await api(statusUrl());
  if (st.state === "ready" && !force) return st;
  st = await api("/api/data/load", {
    universe, source, limit: +$("#limit").value || 0, batch_size: +$("#batch_size").value || 100, force,
  });
  while (st.state === "loading") {
    showProgress(st.message || "Loading…", st.total ? st.done / st.total : null);
    await sleep(800);
    st = await api(statusUrl());
  }
  hideProgress();
  if (st.state === "error") throw new Error(st.message);
  return st;
}

function updateDataInfo(st) {
  $("#data-info").textContent = st && st.state === "ready"
    ? `Data loaded from ${st.source_name}: ${st.n_loaded.toLocaleString("en-IN")} stocks · as of ${st.data_as_of}. Strategy changes re-run instantly.`
    : "";
}

async function run(force = false) {
  const btns = [$("#run"), $("#reload")];
  btns.forEach((b) => (b.disabled = true));
  showAlert(null);
  try {
    const st = await ensureData(force);
    updateDataInfo(st);
    await screen();
  } catch (e) {
    hideProgress();
    showAlert("Data download failed: " + e.message, "error");
  } finally {
    btns.forEach((b) => (b.disabled = false));
  }
}

async function screen() {
  const seq = ++state.screenSeq;
  const req = screenRequest();
  let data;
  try {
    data = await api("/api/screen", req);
  } catch (e) {
    if (e.status === 409) { showAlert("Data for this universe and source isn't loaded yet — click Run screener."); return; }
    showAlert("Screening failed: " + e.message, "error"); return;
  }
  if (seq !== state.screenSeq) return;  // a newer request superseded this one
  state.data = data;
  state.sort = { col: null, desc: false };
  showAlert(null);
  renderAll();
}

const onSettingChange = debounce(async () => {
  save();
  if (!state.data) return;
  const st = await api(statusUrl()).catch(() => null);
  if (st && st.state === "ready") { updateDataInfo(st); screen(); }
  else showAlert(`${state.meta.universes[$("#universe").value]} data from ${sourceName()} isn't loaded — click Run screener.`);
}, 250);

/* ------------------------------------------------------------------ */
/* Rendering                                                           */
/* ------------------------------------------------------------------ */
function renderAll() {
  const d = state.data, s = d.strategy, k = d.kpis;
  $("#empty").hidden = true; $("#results").hidden = false;
  $("#title").textContent = s.name;
  $("#subtitle").textContent = s.description;

  $("#k-evaluated").textContent = k.evaluated.toLocaleString("en-IN");
  $("#k-evaluated-sub").textContent = `${k.n_tickers.toLocaleString("en-IN")} tickers loaded`;
  $("#k-matches").textContent = k.matches.toLocaleString("en-IN");
  $("#k-matches-sub").textContent = k.evaluated ? `${(100 * k.matches / k.evaluated).toFixed(1)}% of evaluated` : "";
  $("#k-highs").textContent = k.new_highs.toLocaleString("en-IN");
  $("#k-date").textContent = k.data_as_of ? new Date(k.data_as_of).toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" }) : "–";
  $("#k-date-sub").textContent = `${$("#universe").selectedOptions[0].textContent} · ${k.source_name || sourceName()}`;

  if (!d.results.length) showAlert("No stocks match this strategy with the current parameters and filters.");
  state.dirty = new Set(["ranking", "charts", "detail", "breadth"]);
  renderTab(state.activeTab);
}

function renderTab(tab) {
  if (!state.data || !state.dirty.has(tab)) return;
  state.dirty.delete(tab);
  ({ ranking: renderRanking, charts: renderMultiples, detail: renderDetailSelect, breadth: renderBreadth })[tab]();
}

/* ---- Ranking ---- */
function renderRanking() {
  const d = state.data, s = d.strategy, c = colors();
  const top = d.results.slice(0, +$("#top_n").value).reverse();
  $("#rank-title").textContent = `Top ${top.length} by ${s.score_col}`;
  const vals = top.map((r) => r[s.score_col]);
  Plotly.react("rank-chart", [{
    type: "bar", orientation: "h", x: vals, y: top.map((r) => r.Symbol),
    marker: { color: c.accent, cornerradius: 4 },
    text: vals.map((v) => fmtPy(s.score_fmt, v)), textposition: "outside", cliponaxis: false,
    textfont: { color: c.text2 },
    customdata: top.map((r) => [r["Current Price"], r["% From 52W High"]]),
    hovertemplate: `<b>%{y}</b><br>${s.score_col}: %{x}<br>Price ₹%{customdata[0]:,.2f}<br>%{customdata[1]:.2f}% below 52W high<extra></extra>`,
  }], baseLayout({
    height: Math.max(320, 26 * top.length + 70), bargap: 0.35,
    margin: { l: 96, r: 56, t: 6, b: 44 },
    xaxis: { ...baseLayout().xaxis, title: { text: `${s.score_col} (${s.ascending ? "lower" : "higher"} ranks higher)` } },
    yaxis: { ...baseLayout().yaxis, showgrid: false, automargin: true },
  }), plotConfig);
  renderTable();
}

function renderTable() {
  queueMicrotask(syncSelectionUI);
  const d = state.data, s = d.strategy;
  const cols = ["Rank", ...d.columns, "Trend"];
  const q = $("#table-search").value.trim().toUpperCase();
  let rows = d.results.filter((r) => !q || r.Symbol.includes(q));
  if (state.sort.col) {
    const { col, desc } = state.sort;
    rows = [...rows].sort((a, b) => {
      const x = a[col], y = b[col];
      if (x === null || x === undefined) return 1; if (y === null || y === undefined) return -1;
      const r = typeof x === "string" ? x.localeCompare(y) : x - y;
      return desc ? -r : r;
    });
  }
  $("#result-count").textContent = `${rows.length.toLocaleString("en-IN")} of ${d.results.length.toLocaleString("en-IN")}`;

  const nearHigh = s.key === "near_52w_high";
  const thr = d.params.threshold;
  const allSel = rows.length > 0 && rows.every((r) => Selection.has(r.Ticker));
  const head = "<thead><tr><th class='chk'><input type='checkbox' id='sel-all' title='Select all shown' " + (allSel ? "checked" : "") + "></th>" + cols.map((c) => {
    const cls = [c === "Symbol" ? "text" : "", state.sort.col === c ? "sorted" + (state.sort.desc ? " desc" : "") : ""].join(" ");
    return `<th class="${cls}" data-col="${c}">${c === "Trend" ? `Last ${$("#chart_days").value}D` : c}</th>`;
  }).join("") + "</tr></thead>";

  const body = "<tbody>" + rows.map((r) => {
    const on = Selection.has(r.Ticker);
    return `<tr data-ticker="${r.Ticker}" class="${on ? "selected" : ""}"><td class="chk"><input type="checkbox" data-sel="${r.Ticker}" aria-label="Select ${r.Symbol}" ${on ? "checked" : ""}></td>` + cols.map((c) => {
    if (c === "Trend") return `<td class="spark">${sparkline(r.Trend)}</td>`;
    if (c === "Symbol") return `<td class="text sym">${r.Symbol}</td>`;
    if (c === "% From 52W High" && nearHigh && r[c] !== null) {
      const pct = Math.min(100, (r[c] / thr) * 100);
      return `<td><span class="bar-cell"><span class="bar-track"><span class="bar-fill" style="width:${pct}%"></span></span>${fmtCell(c, r[c])}</span></td>`;
    }
    return `<td>${fmtCell(c, r[c])}</td>`;
  }).join("") + "</tr>"; }).join("") + "</tbody>";

  const table = $("#table");
  table.innerHTML = head + body;
  table.querySelectorAll("th").forEach((th) => th.addEventListener("click", () => {
    const col = th.dataset.col; if (col === "Trend") return;
    state.sort = state.sort.col === col ? { col, desc: !state.sort.desc } : { col, desc: false };
    renderTable();
  }));
  table.querySelectorAll("tbody tr").forEach((tr) => tr.addEventListener("click", (e) => {
    if (e.target.closest(".chk")) return;
    openDetail(tr.dataset.ticker);
  }));
  table.querySelectorAll("tbody .chk").forEach((td) => td.addEventListener("click", (e) => {
    const cb = td.querySelector("input");
    if (e.target !== cb) cb.checked = !cb.checked;   // whole cell is a hit target
    toggleSelect(cb.dataset.sel, cb.checked);
  }));
  $("#sel-all").addEventListener("change", (e) => {
    if (e.target.checked) Selection.add(rows.map(selItem));
    else Selection.remove(rows.map((r) => r.Ticker));
    syncSelectionUI();
  });
}

function downloadCsv() {
  const d = state.data; if (!d) return;
  const cols = ["Rank", "Ticker", ...d.columns];
  const esc = (v) => v === null || v === undefined ? "" : /[",\n]/.test(String(v)) ? `"${String(v).replace(/"/g, '""')}"` : String(v);
  const csv = [cols.join(","), ...d.results.map((r) => cols.map((c) => esc(r[c])).join(","))].join("\n");
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([csv], { type: "text/csv" }));
  a.download = `${d.strategy.key}_${$("#universe").value}_${new Date().toISOString().slice(0, 10).replace(/-/g, "")}.csv`;
  a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

/* ---- Small multiples ---- */
function overlayTraces(dates, overlays, width) {
  const c = colors();
  return Object.entries(overlays).map(([name, ys]) => ({
    type: "scatter", mode: "lines", x: dates, y: ys, name, hoverinfo: "skip",
    line: { color: c.overlay[name] || c.muted, width, dash: name.startsWith("BB") ? "dot" : "solid" },
  }));
}
function levelShapes(levels, yref = "y") {
  const c = colors(), shapes = [];
  for (const lv of levels) {
    if (lv.y0 !== undefined && lv.y0 !== null) {
      shapes.push({ type: "rect", xref: "paper", x0: 0, x1: 1, yref, y0: Math.min(lv.y0, lv.y), y1: Math.max(lv.y0, lv.y),
        fillcolor: c.band, line: { width: 0 }, layer: "below" });
    }
    shapes.push({ type: "line", xref: "paper", x0: 0, x1: 1, yref, y0: lv.y, y1: lv.y, line: { color: c.muted, width: 1.2, dash: "dash" } });
  }
  return shapes;
}

function renderMultiples() {
  const d = state.data, s = d.strategy, c = colors();
  $("#charts-caption").textContent = `Last ${$("#chart_days").value} trading days with this strategy's indicators. Tick a chart to select it for backtesting; click it for the detail view.`;
  const legend = [`<span><i style="border-color:${c.accent}"></i>Close</span>`];
  const names = d.charts[0] ? Object.keys(d.charts[0].overlays) : [];
  for (const n of names) legend.push(`<span><i class="${n.startsWith("BB") ? "dash" : ""}" style="border-color:${c.overlay[n] || c.muted}"></i>${n}</span>`);
  if (s.has_levels) legend.push(`<span><i class="dash" style="border-color:${c.muted}"></i>Reference level</span><span><i class="band"></i>Threshold zone</span>`);
  $("#legend-row").innerHTML = legend.join("");

  const grid = $("#multiples");
  grid.querySelectorAll(".mini-chart").forEach((el) => Plotly.purge(el));
  grid.innerHTML = "";
  for (const ch of d.charts) {
    const card = document.createElement("div");
    card.className = "mini";
    card.dataset.ticker = ch.ticker;
    card.classList.toggle("selected", Selection.has(ch.ticker));
    card.innerHTML = `<div class="mini-title"><label class="mini-check" title="Select for backtest"><input type="checkbox" data-sel="${ch.ticker}" ${Selection.has(ch.ticker) ? "checked" : ""}><b>#${ch.rank} ${ch.symbol}</b></label><span>${fmtPy(s.score_fmt, ch.score)}</span></div><div class="mini-chart"></div>`;
    card.querySelector(".mini-check").addEventListener("click", (e) => e.stopPropagation());
    card.querySelector("input").addEventListener("change", (e) => toggleSelect(ch.ticker, e.target.checked));
    card.addEventListener("click", () => openDetail(ch.ticker));
    grid.appendChild(card);
    Plotly.newPlot(card.querySelector(".mini-chart"), [
      ...overlayTraces(ch.dates, ch.overlays, 1),
      { type: "scatter", mode: "lines", x: ch.dates, y: ch.close, line: { color: c.accent, width: 2 },
        hovertemplate: `<b>${ch.symbol}</b><br>%{x|%d %b %Y}<br>₹%{y:,.2f}<extra></extra>` },
    ], baseLayout({
      height: 170, margin: { l: 40, r: 6, t: 4, b: 22 }, hovermode: "x",
      xaxis: { ...baseLayout().xaxis, showgrid: false, tickformat: "%b", nticks: 4 },
      yaxis: { ...baseLayout().yaxis, nticks: 4 },
      shapes: levelShapes(ch.levels),
    }), plotConfig);
  }
}

/* ---- Stock detail ---- */
function renderDetailSelect() {
  const d = state.data, sel = $("#stock-select");
  const prev = sel.value;
  const top = d.results.slice(0, +$("#top_n").value);
  sel.innerHTML = top.map((r) => `<option value="${r.Ticker}">#${r.Rank}  ${r.Symbol}</option>`).join("");
  if (top.some((r) => r.Ticker === prev)) sel.value = prev;
  if (top.length) loadDetail(sel.value);
  else { Plotly.purge("detail-chart"); $("#detail-kpis").innerHTML = ""; }
  syncSelectionUI();
}

function openDetail(ticker) {
  switchTab("detail");
  const sel = $("#stock-select");
  if (![...sel.options].some((o) => o.value === ticker)) {
    const r = state.data.results.find((x) => x.Ticker === ticker);
    sel.insertAdjacentHTML("beforeend", `<option value="${ticker}">#${r ? r.Rank : "?"}  ${ticker.replace(".NS", "")}</option>`);
  }
  sel.value = ticker;
  loadDetail(ticker);
}

async function loadDetail(ticker) {
  const s = state.data.strategy, c = colors();
  let d;
  try { d = await api("/api/stock", { ...screenRequest(), ticker }); }
  catch (e) { showAlert("Could not load stock: " + e.message, "error"); return; }
  syncSelectionUI();
  const m = d.metrics;
  const kpi = (label, value, sub = "", cls = "") => `<div class="kpi"><span class="kpi-label">${label}</span><span class="kpi-value">${value}</span><span class="kpi-sub ${cls}">${sub}</span></div>`;
  const r1m = m["1M Return %"];
  $("#detail-kpis").innerHTML =
    kpi("Price", inr(m["Current Price"]), r1m === null ? "" : `${r1m >= 0 ? "▲ +" : "▼ "}${r1m.toFixed(2)}% (1M)`, r1m >= 0 ? "up" : "down") +
    kpi(s.score_col, fmtPy(s.score_fmt, m[s.score_col]), m.Passed ? "passes strategy" : "does not pass") +
    kpi("52W High", inr(m["52W High"]), `${m["% From 52W High"]?.toFixed(2)}% below`) +
    kpi("52W Low", inr(m["52W Low"]), `${m["% Above 52W Low"]?.toFixed(1)}% above`);

  const o = d.ohlcv, extra = d.panel.type === "rsi" || d.panel.type === "macd";
  const dom = extra ? { p: [0.42, 1], v: [0.25, 0.39], i: [0, 0.22] } : { p: [0.3, 1], v: [0, 0.26] };
  const traces = [
    { type: "candlestick", x: d.dates, open: o.Open, high: o.High, low: o.Low, close: o.Close, name: "OHLC",
      increasing: { line: { color: c.up } }, decreasing: { line: { color: c.down } }, xaxis: "x", yaxis: "y" },
    ...overlayTraces(d.dates, d.overlays, 1.5).map((t) => ({ ...t, hoverinfo: undefined, hovertemplate: `${t.name}: ₹%{y:,.2f}<extra></extra>` })),
    { type: "bar", x: d.dates, y: o.Volume, name: "Volume", yaxis: "y2",
      marker: { color: o.Close.map((cl, i) => (cl >= o.Open[i] ? c.up : c.down)) },
      hovertemplate: "Vol %{y:,.0f}<extra></extra>" },
  ];
  const shapes = levelShapes(d.levels, "y");
  const annotations = d.levels.map((lv) => ({ xref: "paper", x: 0, y: lv.y, yref: "y", xanchor: "left", yanchor: "bottom",
    text: `${lv.label} ₹${Number(lv.y).toLocaleString("en-IN", { maximumFractionDigits: 2 })}`, showarrow: false, font: { size: 11, color: c.text2 } }));
  const layout = baseLayout({
    height: extra ? 640 : 560, margin: { l: 60, r: 16, t: 30, b: 30 }, hovermode: "x unified", showlegend: true,
    legend: { orientation: "h", y: 1.06, x: 0 },
    xaxis: { ...baseLayout().xaxis, rangeslider: { visible: false }, rangebreaks: [{ bounds: ["sat", "mon"] }], anchor: extra ? "y3" : "y2" },
    yaxis: { ...baseLayout().yaxis, domain: dom.p, title: { text: "Price (₹)" } },
    yaxis2: { ...baseLayout().yaxis, domain: dom.v, title: { text: "Volume" } },
    shapes, annotations,
  });
  if (d.panel.type === "rsi") {
    traces.push({ type: "scatter", mode: "lines", x: d.dates, y: d.panel.rsi, name: "RSI(14)", yaxis: "y3", line: { color: c.accent, width: 1.5 } });
    layout.yaxis3 = { ...baseLayout().yaxis, domain: dom.i, range: [0, 100], title: { text: "RSI" } };
    layout.shapes.push(
      { type: "rect", xref: "paper", x0: 0, x1: 1, yref: "y3", y0: 30, y1: 70, fillcolor: c.band, line: { width: 0 }, layer: "below" },
      ...[30, 70].map((y) => ({ type: "line", xref: "paper", x0: 0, x1: 1, yref: "y3", y0: y, y1: y, line: { color: c.muted, width: 1, dash: "dash" } })));
  } else if (d.panel.type === "macd") {
    traces.push(
      { type: "bar", x: d.dates, y: d.panel.hist, name: "Histogram", yaxis: "y3", marker: { color: d.panel.hist.map((h) => (h >= 0 ? c.up : c.down)) } },
      { type: "scatter", mode: "lines", x: d.dates, y: d.panel.macd, name: "MACD", yaxis: "y3", line: { color: c.accent, width: 1.5 } },
      { type: "scatter", mode: "lines", x: d.dates, y: d.panel.signal, name: "Signal", yaxis: "y3", line: { color: c.overlay["20 DMA"], width: 1.5 } });
    layout.yaxis3 = { ...baseLayout().yaxis, domain: dom.i, title: { text: "MACD" } };
  }
  Plotly.react("detail-chart", traces, layout, plotConfig);
}

/* ---- Breadth ---- */
function renderBreadth() {
  const d = state.data, s = d.strategy, c = colors(), dist = d.distribution;
  $("#dist-title").textContent = `${s.score_col} across the universe`;
  const vals = dist.score.filter((v) => v !== null).sort((a, b) => a - b);
  if (!vals.length) { Plotly.purge("dist-chart"); Plotly.purge("high-chart"); return; }
  const q = (p) => vals[Math.min(vals.length - 1, Math.max(0, Math.round(p * (vals.length - 1))))];
  const lo = q(0.01), hi = q(0.99), size = hi > lo ? (hi - lo) / 40 : 1;
  const clip = (v) => Math.min(hi, Math.max(lo, v));
  const pick = (flag) => dist.score.map((v, i) => (v !== null && dist.passed[i] === flag ? clip(v) : null)).filter((v) => v !== null);
  const bins = { start: lo, end: hi + size, size };
  Plotly.react("dist-chart", [
    { type: "histogram", x: pick(false), xbins: bins, name: "Other stocks", marker: { color: c.others }, hovertemplate: "%{x}<br>%{y} stocks<extra>Others</extra>" },
    { type: "histogram", x: pick(true), xbins: bins, name: "Strategy matches", marker: { color: c.accent }, hovertemplate: "%{x}<br>%{y} stocks<extra>Matches</extra>" },
  ], baseLayout({
    barmode: "stack", bargap: 0.06, height: 360, showlegend: true, legend: { orientation: "h", y: 1.12, x: 0 },
    margin: { l: 50, r: 16, t: 30, b: 44 },
    xaxis: { ...baseLayout().xaxis, title: { text: `${s.score_col} (1st–99th percentile)` } },
    yaxis: { ...baseLayout().yaxis, title: { text: "Number of stocks" } },
  }), plotConfig);

  Plotly.react("high-chart", [{
    type: "histogram", x: dist.pct_from_high.filter((v) => v !== null).map((v) => Math.min(v, 80)),
    xbins: { start: 0, end: 80, size: 2.5 }, marker: { color: c.accent },
    hovertemplate: "%{x}% below high<br>%{y} stocks<extra></extra>",
  }], baseLayout({
    height: 360, bargap: 0.06, margin: { l: 50, r: 16, t: 30, b: 44 },
    xaxis: { ...baseLayout().xaxis, title: { text: "% below 52-week high (80%+ grouped)" } },
    yaxis: { ...baseLayout().yaxis, title: { text: "Number of stocks" } },
  }), plotConfig);
}

/* ------------------------------------------------------------------ */
/* Selection for backtest                                              */
/* ------------------------------------------------------------------ */
function selItem(r) {
  return { ticker: r.Ticker || r.ticker, symbol: r.Symbol || r.symbol, source: state.data?.strategy.key || "" };
}

function toggleSelect(ticker, on) {
  if (on) {
    const r = state.data?.results.find((x) => x.Ticker === ticker) || { Ticker: ticker, Symbol: ticker.replace(/\.NS$/, "") };
    Selection.add([selItem(r)]);
  } else Selection.remove([ticker]);
  syncSelectionUI();
}

function syncSelectionUI() {
  const list = Selection.load();
  const set = new Set(list.map((s) => s.ticker));
  // table rows + header
  document.querySelectorAll("#table tbody tr").forEach((tr) => {
    const on = set.has(tr.dataset.ticker);
    tr.classList.toggle("selected", on);
    const cb = tr.querySelector("input[data-sel]"); if (cb) cb.checked = on;
  });
  const all = $("#sel-all");
  if (all) {
    const boxes = [...document.querySelectorAll("#table tbody input[data-sel]")];
    const n = boxes.filter((b) => b.checked).length;
    all.checked = boxes.length > 0 && n === boxes.length;
    all.indeterminate = n > 0 && n < boxes.length;
  }
  // chart cards
  document.querySelectorAll(".mini[data-ticker]").forEach((card) => {
    const on = set.has(card.dataset.ticker);
    card.classList.toggle("selected", on);
    const cb = card.querySelector("input[data-sel]"); if (cb) cb.checked = on;
  });
  // detail button
  const btn = $("#detail-select-btn"), cur = $("#stock-select")?.value;
  if (btn) {
    const on = cur && set.has(cur);
    btn.textContent = on ? "✓ Selected for backtest" : "+ Select for backtest";
    btn.classList.toggle("on", !!on);
    btn.disabled = !cur;
  }
  // bottom bar
  const bar = $("#selection-bar");
  bar.hidden = list.length === 0;
  $("#sel-count").textContent = `${list.length} stock${list.length === 1 ? "" : "s"} selected`;
  const shown = list.slice(0, 8);
  $("#sel-chips").innerHTML = shown.map((s) =>
    `<span class="chip">${esc(s.symbol)}<button data-unsel="${esc(s.ticker)}" aria-label="Remove ${esc(s.symbol)}">×</button></span>`).join("")
    + (list.length > shown.length ? `<span class="chip more">+${list.length - shown.length} more</span>` : "");
  $("#sel-chips").querySelectorAll("[data-unsel]").forEach((b) => b.addEventListener("click", () => toggleSelect(b.dataset.unsel, false)));
}

/* ------------------------------------------------------------------ */
/* Tabs, init                                                          */
/* ------------------------------------------------------------------ */
function switchTab(tab) {
  state.activeTab = tab;
  document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  document.querySelectorAll(".tab-panel").forEach((p) => (p.hidden = p.id !== "tab-" + tab));
  renderTab(tab);
  // plotly charts drawn while hidden need a resize once visible
  document.querySelectorAll(`#tab-${tab} .js-plotly-plot`).forEach((el) => Plotly.Plots.resize(el));
}

async function init() {
  try { state.meta = await api("/api/meta"); }
  catch (e) { showAlert("Could not reach the API: " + e.message, "error"); return; }
  state.meta.strategies.forEach((s) => (state.strategies[s.key] = s));

  const saved = loadSaved();
  $("#strategy").innerHTML = state.meta.strategies.map((s) => `<option value="${s.key}">${s.name}</option>`).join("");
  $("#universe").innerHTML = Object.entries(state.meta.universes).map(([k, v]) => `<option value="${k}">${v}</option>`).join("");
  if (saved.strategy && state.strategies[saved.strategy]) $("#strategy").value = saved.strategy;
  if (saved.universe && state.meta.universes[saved.universe]) $("#universe").value = saved.universe;
  // an older server process doesn't report providers: still list TradingView and explain
  if (!state.meta.providers) state.meta.providers = {
    yahoo: { name: "Yahoo Finance", available: true, note: "Free, no login." },
    tradingview: { name: "TradingView", available: false, outdated: true,
                   note: "", install_hint: "" },
  };
  const prov = state.meta.providers;
  $("#source").innerHTML = Object.entries(prov).map(([k, p]) =>
    `<option value="${k}" ${p.available ? "" : "disabled"}>${esc(p.name)}${p.available ? "" : p.outdated ? " (restart server)" : " (not installed)"}</option>`).join("");
  if (saved.source && prov[saved.source]?.available) $("#source").value = saved.source;
  renderSourceHint();
  for (const id of ["top_n", "min_price", "min_avg_volume", "min_history", "chart_days", "limit", "batch_size"]) if (saved[id] !== undefined) $("#" + id).value = saved[id];
  for (const id of ["top_n", "chart_days"]) {
    const sync = () => ($(`#${id}-out`).textContent = $("#" + id).value);
    sync(); $("#" + id).addEventListener("input", sync);
  }
  renderParams();

  $("#strategy").addEventListener("change", () => { renderParams(); onSettingChange(); });
  $("#universe").addEventListener("change", onSettingChange);
  $("#source").addEventListener("change", () => { renderSourceHint(); onSettingChange(); });
  for (const id of ["top_n", "chart_days", "min_price", "min_avg_volume", "min_history"]) $("#" + id).addEventListener("change", onSettingChange);
  for (const id of ["limit", "batch_size"]) $("#" + id).addEventListener("change", save);
  $("#run").addEventListener("click", () => { save(); run(false); });
  $("#reload").addEventListener("click", () => { save(); run(true); });
  $("#download").addEventListener("click", downloadCsv);
  $("#table-search").addEventListener("input", debounce(renderTable, 150));
  $("#stock-select").addEventListener("change", (e) => loadDetail(e.target.value));
  document.addEventListener("themechange", () => {
    if (state.data) { state.dirty = new Set(["ranking", "charts", "detail", "breadth"]); renderTab(state.activeTab); }
  });
  $("#sel-clear").addEventListener("click", () => { Selection.clear(); syncSelectionUI(); });
  $("#sel-backtest").addEventListener("click", () => { location.href = Selection.backtestUrl(); });
  $("#sel-trade").addEventListener("click", () => {
    location.href = Selection.tradeUrl(Selection.load().map((s) => s.ticker), { origin: "screener" });
  });
  $("#detail-select-btn").addEventListener("click", () => {
    const t = $("#stock-select").value; if (!t) return;
    toggleSelect(t, !Selection.has(t));
  });
  syncSelectionUI();
  document.querySelectorAll(".tab").forEach((b) => b.addEventListener("click", () => switchTab(b.dataset.tab)));

  // If the server already has data for the selected universe (e.g. after a page refresh), show results right away.
  const st = await api(statusUrl()).catch(() => null);
  if (st && st.state === "ready") { updateDataInfo(st); screen(); }
  else if (st && st.state === "loading") run(false);
}

init();
