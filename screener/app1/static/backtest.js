/* Backtest page - shows the stocks selected in the screener. Depends on app.js and Plotly.
   The URL (?tickers=A.NS,B.NS&source=...) is the source of truth so the page can be bookmarked/shared;
   it is kept in sync with the shared Selection basket. */
"use strict";

const bt = { list: [], quotes: {}, strategies: {}, engines: {}, result: null, candles: {} };
const BT_PARAMS_KEY = "nse-backtest-params-v1";
const btSource = () => $("#bt-source")?.value || "yahoo";

function normalizeTicker(raw) {
  let t = String(raw).trim().toUpperCase().replace(/\s+/g, "");
  if (!t) return null;
  if (!/\.(NS|BO)$/.test(t)) t += ".NS";
  return /^[A-Z0-9&_\-]+\.(NS|BO)$/.test(t) ? t : null;
}

function readUrl() {
  const p = new URLSearchParams(location.search);
  const tickers = (p.get("tickers") || "").split(",").map(normalizeTicker).filter(Boolean);
  const source = (p.get("source") || "").split(",").filter(Boolean);
  return { tickers: [...new Set(tickers)], source };
}

function writeUrl() {
  history.replaceState(null, "", Selection.backtestUrl(bt.list));
}

function initList() {
  const { tickers, source } = readUrl();
  const basket = Selection.load();
  if (tickers.length) {
    // URL wins; keep any strategy info the basket already has for these tickers
    bt.list = tickers.map((t) => basket.find((s) => s.ticker === t)
      || { ticker: t, symbol: t.replace(/\.(NS|BO)$/, ""), source: source.length === 1 ? source[0] : "" });
    Selection.save(bt.list);
  } else {
    bt.list = basket;
    if (bt.list.length) writeUrl();
  }
}

async function loadQuotes() {
  const need = bt.list.map((s) => s.ticker).filter((t) => !bt.quotes[t]);
  if (!need.length) return;
  $("#bt-loading").hidden = false;
  try {
    const res = await api("/api/quotes", { tickers: need, source: btSource() });
    for (const q of res.quotes) bt.quotes[q.ticker] = q;
    for (const t of res.missing) bt.quotes[t] = { ticker: t, missing: true };
    if (res.missing.length) showAlert(`No price data found for: ${res.missing.map((t) => t.replace(/\.(NS|BO)$/, "")).join(", ")}`);
  } catch (e) {
    showAlert("Could not load prices: " + e.message, "error");
  } finally {
    $("#bt-loading").hidden = true;
  }
}

function render() {
  const n = bt.list.length;
  $("#empty").hidden = n > 0;
  $("#bt-count").textContent = n ? `(${n})` : "";
  $("#clear-all").disabled = n === 0;

  const head = `<thead><tr>
    <th class="text">Symbol</th><th class="text">From strategy</th><th>Last price</th><th>1M return</th>
    <th>1Y return</th><th>% from 52W high</th><th>Last 1Y</th><th class="chk"></th></tr></thead>`;
  const rows = bt.list.map((s) => {
    const q = bt.quotes[s.ticker] || {};
    const strat = bt.strategies[s.source]?.name || s.source || "manual";
    const cls = (v) => (v === null || v === undefined) ? "" : v >= 0 ? "pos" : "neg";
    return `<tr>
      <td class="text sym">${esc(s.symbol)}</td>
      <td class="text muted">${esc(strat)}</td>
      <td>${q.missing ? "no data" : inr(q.last_price)}</td>
      <td class="${cls(q.ret_1m)}">${pct(q.ret_1m)}</td>
      <td class="${cls(q.ret_1y)}">${pct(q.ret_1y)}</td>
      <td>${q.pct_from_high === undefined || q.pct_from_high === null ? "–" : q.pct_from_high.toFixed(2) + "%"}</td>
      <td class="spark">${sparkline(q.close)}</td>
      <td class="chk"><button class="icon-x" data-remove="${esc(s.ticker)}" aria-label="Remove ${esc(s.symbol)}" title="Remove">×</button></td>
    </tr>`;
  }).join("");
  $("#bt-table").innerHTML = head + `<tbody>${rows || `<tr><td colspan="8" class="text muted">Nothing here yet.</td></tr>`}</tbody>`;
  $("#bt-table").querySelectorAll("[data-remove]").forEach((b) => b.addEventListener("click", () => removeTicker(b.dataset.remove)));
  renderPerf();
}

function renderPerf() {
  const c = colors();
  const withData = bt.list.map((s) => bt.quotes[s.ticker]).filter((q) => q && !q.missing && q.close?.length > 1);
  $("#perf-card").hidden = withData.length === 0;
  if (!withData.length) { Plotly.purge("perf-chart"); return; }

  // first 8 get a categorical colour (fixed order = selection order), the rest are muted
  const traces = withData.map((q, i) => {
    const base = q.close.find((v) => v !== null);
    const color = i < 8 ? c.series[i] : c.others;
    return {
      type: "scatter", mode: "lines", x: q.dates, y: q.close.map((v) => (v === null ? null : (v / base) * 100)),
      name: q.symbol, line: { color, width: i < 8 ? 2 : 1 },
      hovertemplate: `<b>${q.symbol}</b> %{y:.1f}<extra></extra>`,
    };
  }).reverse();  // draw coloured series on top
  $("#legend").innerHTML = withData.slice(0, 8).map((q, i) =>
    `<span><i style="border-color:${c.series[i]}"></i>${esc(q.symbol)}</span>`).join("")
    + (withData.length > 8 ? `<span><i style="border-color:${c.muted}"></i>${withData.length - 8} more (grey)</span>` : "");
  $("#perf-hint").textContent = "Each stock rebased to 100 at the start of the window."
    + (withData.length > 8 ? " The first 8 selected stocks are coloured; the rest are grey." : "");

  Plotly.react("perf-chart", traces, baseLayout({
    height: 380, hovermode: "x unified", margin: { l: 50, r: 16, t: 10, b: 36 },
    xaxis: { ...baseLayout().xaxis, showgrid: false },
    yaxis: { ...baseLayout().yaxis, title: { text: "Rebased (start = 100)" } },
    shapes: [{ type: "line", xref: "paper", x0: 0, x1: 1, y0: 100, y1: 100, line: { color: c.muted, width: 1, dash: "dash" } }],
  }), plotConfig);
}

async function addTickers(raw) {
  const tickers = raw.split(/[,\s]+/).map(normalizeTicker).filter(Boolean);
  const bad = raw.split(/[,\s]+/).filter((x) => x && !normalizeTicker(x));
  if (bad.length) showAlert(`Not a valid NSE ticker: ${bad.join(", ")}`);
  const fresh = tickers.filter((t) => !bt.list.some((s) => s.ticker === t));
  if (!fresh.length) return;
  bt.list = Selection.add(fresh.map((t) => ({ ticker: t, symbol: t.replace(/\.(NS|BO)$/, ""), source: "" })));
  writeUrl(); render();
  await loadQuotes(); render();
}

function removeTicker(t) {
  bt.list = Selection.remove([t]);
  writeUrl(); render();
}

async function init() {
  initList();
  render();
  try {
    const meta = await api("/api/meta");
    meta.strategies.forEach((s) => (bt.strategies[s.key] = s));
  } catch { /* names fall back to keys */ }
  render();
  await initEngine();
  await loadQuotes();
  render();

  $("#add-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const v = $("#add-input").value; $("#add-input").value = "";
    showAlert(null); addTickers(v);
  });
  $("#clear-all").addEventListener("click", () => { bt.list = Selection.clear(); writeUrl(); render(); });
  document.addEventListener("themechange", () => { renderPerf(); if (bt.result) renderResults(); });
}

init();

/* ================================================================== */
/* Backtest engine UI                                                  */
/* ================================================================== */
function savedParams() { try { return JSON.parse(localStorage.getItem(BT_PARAMS_KEY)) || {}; } catch { return {}; } }

async function initEngine() {
  try {
    const [list, prov] = await Promise.all([api("/api/backtest/strategies"),
      api("/api/backtest/providers").catch(() => ({
      yahoo: { name: "Yahoo Finance", available: true, note: "Free, no login." },
      tradingview: { name: "TradingView", available: false, outdated: true,
                     note: "", install_hint: "" },
    }))]);
    bt.providers = prov;
    $("#bt-source").innerHTML = Object.entries(prov).map(([k, p]) =>
      `<option value="${k}" ${p.available ? "" : "disabled"}>${esc(p.name)}${p.available ? "" : p.outdated ? " (restart server)" : " (not installed)"}</option>`).join("");
    const savedSrc = savedParams()._source;
    if (savedSrc && prov[savedSrc]?.available) $("#bt-source").value = savedSrc;
    renderSourceHint();
    list.forEach((e) => (bt.engines[e.key] = e));
    $("#bt-strategy").innerHTML = list.map((e) => `<option value="${e.key}">${esc(e.name)}</option>`).join("");
    const saved = savedParams();
    if (saved._strategy && bt.engines[saved._strategy]) $("#bt-strategy").value = saved._strategy;
    renderEngineParams();
  } catch (e) {
    showAlert("Could not load backtest strategies: " + e.message, "error");
  }
  $("#bt-strategy").addEventListener("change", renderEngineParams);
  $("#bt-source").addEventListener("change", async () => {
    renderSourceHint();
    try { const all = savedParams(); all._source = btSource(); localStorage.setItem(BT_PARAMS_KEY, JSON.stringify(all)); } catch { /* ignore */ }
    bt.quotes = {}; render(); await loadQuotes(); render();   // price table follows the chosen source
  });
  document.addEventListener("input", (e) => { if (["days", "timeframe"].includes(e.target.dataset?.bt)) renderSourceHint(); });
  document.addEventListener("change", (e) => { if (e.target.dataset?.bt === "timeframe") renderSourceHint(); });
  $("#bt-run").addEventListener("click", runBacktest);
  $("#chart-stock").addEventListener("change", (e) => renderTradeChart(e.target.value));
  $("#trade-filter").addEventListener("change", renderTradeTable);
  $("#trade-csv").addEventListener("click", downloadTrades);
}

function renderSourceHint() {
  const p = bt.providers?.[btSource()];
  if (!p) return;
  const days = +(document.querySelector('[data-bt="days"]')?.value || 0);
  const tf = document.querySelector('[data-bt="timeframe"]')?.value || "15m";
  const tfLabel = document.querySelector('[data-bt="timeframe"]')?.selectedOptions[0]?.textContent || "15 minutes";
  const maxd = p.max_days?.[tf] ?? p.intraday_max_days;
  let msg = `${p.name}: up to ${maxd.toLocaleString("en-IN")} days of ${tfLabel.toLowerCase()} candles.`;
  if (days > maxd) msg += ` Your ${days.toLocaleString("en-IN")} days will be capped to ${maxd.toLocaleString("en-IN")}.`;
  if (!["5m", "15m", "30m", "1h"].includes(tf)) msg += " Daily/weekly/monthly: long only, positions held across bars.";
  const missing = Object.values(bt.providers).filter((x) => !x.available);
  if (missing.some((x) => x.outdated)) msg += " TradingView is unavailable because the server is running old code — stop it and run python main.py again.";
  else if (missing.length) msg += " TradingView not installed: pip install tvkit, then restart the server.";
  $("#bt-source-hint").textContent = msg;
}

function renderEngineParams() {
  const e = bt.engines[$("#bt-strategy").value];
  if (!e) return;
  $("#bt-rules").innerHTML = e.rules.map((r) => `<li>${esc(r)}</li>`).join("");
  const saved = (savedParams()[e.key]) || {};
  const box = $("#bt-params"); box.innerHTML = "";
  let lastGroup = "";
  for (const p of e.params) {
    if (p.group && p.group !== lastGroup) {
      lastGroup = p.group;
      const h = document.createElement("div"); h.className = "param-group"; h.textContent = p.group; box.appendChild(h);
    }
    const val = saved[p.key] ?? p.default;
    const wrap = document.createElement("label");
    if (p.help) wrap.title = p.help;
    if (p.kind === "choice") {
      wrap.className = "field";
      wrap.innerHTML = `<span>${esc(p.label)}</span><select data-bt="${p.key}">${(p.options || []).map(([v, l]) =>
        `<option value="${esc(v)}">${esc(l)}</option>`).join("")}</select>`;
      const sel = wrap.querySelector("select");
      sel.value = (p.options || []).some(([v]) => v === val) ? val : p.default;
      box.appendChild(wrap);
      continue;
    }
    if (p.kind === "bool") {
      wrap.className = "field toggle";
      wrap.innerHTML = `<span>${esc(p.label)}</span><span class="switch"><input type="checkbox" data-bt="${p.key}"><span class="slider"></span></span>`;
      wrap.querySelector("input").checked = !!val;
    } else if ((p.max - p.min) / p.step > 400) {
      wrap.className = "field";
      wrap.innerHTML = `<span>${esc(p.label)}</span><input type="number" data-bt="${p.key}" min="${p.min}" max="${p.max}" step="${p.step}">`;
      wrap.querySelector("input").value = val;
    } else {
      wrap.className = "field";
      wrap.innerHTML = `<span>${esc(p.label)} <output></output></span><input type="range" data-bt="${p.key}" min="${p.min}" max="${p.max}" step="${p.step}">`;
      const input = wrap.querySelector("input"), out = wrap.querySelector("output");
      input.value = val;
      const dec = p.kind === "int" ? 0 : (String(p.step).split(".")[1] || "").length;
      const show = () => (out.textContent = Number(input.value).toFixed(dec));
      show(); input.addEventListener("input", show);
    }
    box.appendChild(wrap);
  }
  renderSourceHint();
}

function engineParams() {
  const out = {};
  document.querySelectorAll("#bt-params [data-bt]").forEach((el) => {
    out[el.dataset.bt] = el.tagName === "SELECT" ? el.value : el.type === "checkbox" ? el.checked : Number(el.value);
  });
  return out;
}

async function runBacktest() {
  const key = $("#bt-strategy").value, params = engineParams();
  if (!bt.list.length) { showAlert("Add at least one stock to backtest."); return; }
  try {
    const all = savedParams(); all[key] = params; all._strategy = key; all._source = btSource();
    localStorage.setItem(BT_PARAMS_KEY, JSON.stringify(all));
  } catch { /* ignore */ }
  const btn = $("#bt-run");
  btn.disabled = true; btn.textContent = "Running…";
  $("#bt-run-hint").textContent = `Downloading ${bt.engines[key].interval} candles for ${bt.list.length} stock${bt.list.length > 1 ? "s" : ""}…`;
  showAlert(null);
  try {
    bt.result = await api("/api/backtest/run", { strategy: key, tickers: bt.list.map((s) => s.ticker), params, source: btSource() });
    bt.candles = {};
    if (bt.result.missing.length) showAlert(`No intraday data for: ${bt.result.missing.map((t) => t.replace(/\.(NS|BO)$/, "")).join(", ")}`);
    renderResults();
    $("#bt-results").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (e) {
    showAlert(e.message, "error");
  } finally {
    btn.disabled = false; btn.textContent = "Run backtest"; $("#bt-run-hint").textContent = "";
  }
}

const fmtR = (v, d = 2) => (v === null || v === undefined) ? "–" : (v >= 0 ? "+" : "") + Number(v).toFixed(d) + "R";
const VERDICT_ICON = { success: "✓", marginal: "~", fail: "✕", inconclusive: "?", none: "–" };

function renderResults() {
  const r = bt.result, o = r.overall, v = r.verdict;
  $("#bt-results").hidden = false;

  // verdict
  $("#verdict").className = "verdict " + v.level;
  $("#verdict-icon").textContent = VERDICT_ICON[v.level] || "";
  $("#verdict-label").textContent = `${r.strategy.name}: ${v.label}`;
  $("#verdict-reason").textContent = v.reason;
  $("#verdict-meta").textContent = r.period.from
    ? `${r.period.source_name} · ${r.period.interval_label || r.period.interval} candles · ${r.period.from} → ${r.period.to} · ${r.per_stock.filter((p) => !p.no_data).length} stocks · costs ${r.params.cost_pct}% per side`
      + (r.period.days_capped ? ` · history capped at ${r.period.days} days for ${r.period.source_name}` : "")
    : "";

  // KPIs
  const kpi = (label, value, sub = "", cls = "") =>
    `<div class="kpi"><span class="kpi-label">${label}</span><span class="kpi-value ${cls}">${value}</span><span class="kpi-sub">${sub}</span></div>`;
  const signCls = (x) => (x > 0 ? "pos" : x < 0 ? "neg" : "");
  $("#bt-kpis").innerHTML = !o.trades ? kpi("Trades", "0", "no signals in this period") :
    kpi("Trades", o.trades.toLocaleString("en-IN"), `avg ${o.avg_bars_held} candles held`) +
    kpi("Win rate", o.win_rate.toFixed(1) + "%", o.breakeven_win_rate !== null ? `needs ${o.breakeven_win_rate.toFixed(1)}% to break even` : "") +
    kpi("Expectancy", fmtR(o.expectancy_r, 3), `per trade · ${fmtR(o.gross_expectancy_r, 3)} before costs`, signCls(o.expectancy_r)) +
    kpi("Profit factor", o.profit_factor === null ? "∞" : o.profit_factor.toFixed(2), `t-stat ${o.t_stat.toFixed(2)} (≥2 = significant)`) +
    kpi("Total", fmtR(o.total_r, 1), `${inr(o.net_pnl, 0)} ${r.params.risk_pct ? `on a ${inr(r.params.capital, 0)} account` : `at ${inr(r.params.capital, 0)}/trade`}`, signCls(o.total_r)) +
    kpi("Max drawdown", "−" + o.max_drawdown_r.toFixed(1) + "R", `avg win ${o.avg_win_r.toFixed(2)}R · avg loss ${o.avg_loss_r.toFixed(2)}R`);

  $("#bt-trade").onclick = () => {
    const ok = r.per_stock.filter((p) => !p.no_data).map((p) => p.ticker);
    location.href = tradeLink(ok.length ? ok : bt.list.map((s) => s.ticker));
  };

  // long vs short split (only shown when the strategy trades both sides)
  const bs = o.by_side || {};
  $("#side-split").hidden = !(bs.long && bs.short);
  if (bs.long && bs.short) {
    const row = (k, lbl) => { const x = bs[k]; const cls = x.expectancy_r > 0 ? "pos" : "neg";
      return `<div><span class="side ${k}">${lbl}</span> ${x.trades} trades · win ${x.win_rate.toFixed(1)}% · expectancy <b class="${cls}">${fmtR(x.expectancy_r, 3)}</b> · total <b class="${cls}">${fmtR(x.total_r, 1)}</b> · ${inr(x.net_pnl, 0)}</div>`; };
    $("#side-split").innerHTML = row("long", "Long") + row("short", "Short")
      + (o.skipped_daily_loss ? `<div class="muted">${o.skipped_daily_loss} entries skipped by the daily loss limit</div>` : "");
  }

  renderEquity();
  renderDistribution();
  renderStockTable();

  const withTrades = r.per_stock.filter((p) => p.trades > 0);
  const stocks = r.per_stock.filter((p) => !p.no_data);
  $("#chart-stock").innerHTML = stocks.map((p) => `<option value="${p.ticker}">${esc(p.symbol)} (${p.trades} trades)</option>`).join("");
  $("#trade-filter").innerHTML = `<option value="">All stocks</option>` + withTrades.map((p) => `<option value="${p.ticker}">${esc(p.symbol)}</option>`).join("");
  renderTradeTable();
  if (stocks.length) renderTradeChart((withTrades[0] || stocks[0]).ticker);
  else Plotly.purge("trade-chart");
}

function renderEquity() {
  const c = colors(), eq = bt.result.equity;
  if (!eq.length) { Plotly.purge("equity-chart"); return; }
  const last = eq[eq.length - 1].r;
  Plotly.react("equity-chart", [{
    type: "scatter", mode: "lines", x: eq.map((e, i) => i + 1), y: eq.map((e) => e.r),
    line: { color: last >= 0 ? c.up : c.down, width: 2 }, fill: "tozeroy",
    fillcolor: last >= 0 ? "rgba(27,175,122,0.10)" : "rgba(227,73,72,0.10)",
    customdata: eq.map((e) => e.time), hovertemplate: "Trade #%{x}<br>%{customdata}<br>Cumulative %{y:+.2f}R<extra></extra>",
  }], baseLayout({
    height: 320, margin: { l: 50, r: 16, t: 10, b: 40 },
    xaxis: { ...baseLayout().xaxis, title: { text: "Trade number" }, showgrid: false },
    yaxis: { ...baseLayout().yaxis, title: { text: "Cumulative R" }, zeroline: true, zerolinecolor: c.muted },
  }), plotConfig);
}

function renderDistribution() {
  const c = colors(), trades = bt.result.trades;
  if (!trades.length) { Plotly.purge("dist-chart"); return; }
  const reasons = bt.result.overall.exit_reasons;
  const known = ["target", "target (gap)", "end of day", "opposite signal", "breakeven stop", "breakeven stop (gap)", "stop", "stop (gap)"];
  const order = [...known.filter((k) => reasons[k]), ...Object.keys(reasons).filter((k) => !known.includes(k))];
  const rc = { "target": c.up, "target (gap)": c.up, "end of day": c.muted, "opposite signal": c.overlay["20 DMA"],
    "breakeven stop": c.overlay["50 DMA"], "breakeven stop (gap)": c.overlay["50 DMA"], "stop": c.down, "stop (gap)": c.down };
  const total = trades.length;
  Plotly.react("dist-chart", [
    { type: "bar", orientation: "h", y: order, x: order.map((k) => reasons[k]), xaxis: "x", yaxis: "y",
      marker: { color: order.map((k) => rc[k]), cornerradius: 3 },
      text: order.map((k) => `${reasons[k]} (${(100 * reasons[k] / total).toFixed(0)}%)`), textposition: "outside", cliponaxis: false,
      hovertemplate: "%{y}: %{x} trades<extra></extra>" },
    { type: "histogram", x: trades.map((t) => t.net_r), xaxis: "x2", yaxis: "y2",
      xbins: { size: 0.1 }, marker: { color: c.accent },
      hovertemplate: "%{x}R<br>%{y} trades<extra></extra>" },
  ], baseLayout({
    height: 340, margin: { l: 130, r: 90, t: 10, b: 40 }, bargap: 0.25,
    grid: { rows: 2, columns: 1, pattern: "independent", roworder: "top to bottom" },
    xaxis: { ...baseLayout().xaxis, showgrid: false, showticklabels: false },
    yaxis: { ...baseLayout().yaxis, domain: [0.6, 1], showgrid: false, autorange: "reversed" },
    xaxis2: { ...baseLayout().xaxis, anchor: "y2", title: { text: "Net R per trade" } },
    yaxis2: { ...baseLayout().yaxis, domain: [0, 0.48], title: { text: "Trades" } },
  }), plotConfig);
}

const VERDICT_TAG = { success: "pos", fail: "neg", marginal: "", inconclusive: "muted", none: "muted" };
/* Link to the Trade page carrying the strategy + parameters + data source used in this backtest */
function tradeLink(tickers) {
  const r = bt.result;
  return Selection.tradeUrl(tickers, { origin: "backtest", strategy: r.strategy.key, source: r.period.source, params: r.params });
}

function renderStockTable() {
  const rows = [...bt.result.per_stock].sort((a, b) => (b.total_r ?? -1e9) - (a.total_r ?? -1e9));
  const head = `<thead><tr><th class="text">Symbol</th><th>Trades</th><th>Win rate</th><th>Expectancy</th><th>Total</th>
    <th>Profit factor</th><th>Max DD</th><th>Net P&amp;L</th><th class="text">Verdict</th><th></th></tr></thead>`;
  const body = rows.map((p) => {
    const tradeBtn = `<td><a class="btn small" data-trade="${p.ticker}" href="${tradeLink([p.ticker])}">Paper trade</a></td>`;
    if (p.no_data) return `<tr><td class="text sym">${esc(p.symbol)}</td><td colspan="9" class="text muted">No intraday data</td></tr>`;
    if (!p.trades) return `<tr data-t="${p.ticker}"><td class="text sym">${esc(p.symbol)}</td><td>0</td><td colspan="7" class="text muted">No signals</td>${tradeBtn}</tr>`;
    const cls = (x) => (x > 0 ? "pos" : x < 0 ? "neg" : "");
    return `<tr data-t="${p.ticker}">
      <td class="text sym">${esc(p.symbol)}</td><td>${p.trades}</td><td>${p.win_rate.toFixed(1)}%</td>
      <td class="${cls(p.expectancy_r)}">${fmtR(p.expectancy_r, 3)}</td><td class="${cls(p.total_r)}">${fmtR(p.total_r, 1)}</td>
      <td>${p.profit_factor === null ? "∞" : p.profit_factor.toFixed(2)}</td><td>−${p.max_drawdown_r.toFixed(1)}R</td>
      <td class="${cls(p.net_pnl)}">${inr(p.net_pnl, 0)}</td>
      <td class="text ${VERDICT_TAG[p.verdict.level]}">${esc(p.verdict.label)}</td>${tradeBtn}</tr>`;
  }).join("");
  $("#stock-table").innerHTML = head + `<tbody>${body}</tbody>`;
  $("#stock-table").querySelectorAll("[data-trade]").forEach((a) => a.addEventListener("click", (e) => e.stopPropagation()));
  $("#stock-table").querySelectorAll("tr[data-t]").forEach((tr) => tr.addEventListener("click", () => {
    $("#chart-stock").value = tr.dataset.t; renderTradeChart(tr.dataset.t);
    $("#trade-chart").scrollIntoView({ behavior: "smooth", block: "center" });
  }));
}

async function renderTradeChart(ticker) {
  if (!ticker) return;
  $("#chart-stock").value = ticker;
  const c = colors();
  let cd = bt.candles[ticker];
  if (!cd) {
    try { cd = bt.candles[ticker] = await api("/api/backtest/candles", { strategy: bt.result.strategy.key, ticker, days: bt.result.period.days, source: bt.result.period.source, interval: bt.result.period.interval }); }
    catch (e) { showAlert("Could not load candles: " + e.message, "error"); return; }
  }
  const trades = bt.result.trades.filter((t) => t.ticker === ticker);
  $("#chart-hint").textContent = cd.intraday !== false
    ? `${bt.result.period.interval_label || "15-minute"} candles, showing the last 5 sessions — drag the slider below the chart to scroll back.`
    : `${bt.result.period.interval_label} candles, showing the last ${Math.min(120, cd.time.length)} — drag the slider below the chart to scroll back.`;
  // open zoomed to the last 5 sessions; the range slider below scrolls through the rest
  // intraday: open on the last 5 sessions; daily+: last ~120 bars
  let range;
  if (cd.intraday !== false) {
    const days = [...new Set(cd.time.map((x) => x.slice(0, 10)))];
    const startDay = days[Math.max(0, days.length - 5)];
    range = [startDay + " 09:00", cd.time[cd.time.length - 1].slice(0, 10) + " 15:45"];
  } else {
    const k = Math.max(0, cd.time.length - 120);
    range = [cd.time[k], cd.time[cd.time.length - 1]];
  }
  const won = (t) => t.net_r > 0;
  const tip = (t) => `Entry ₹${t.entry}<br>Stop ₹${t.stop} · Target ₹${t.target}<br>Exit ₹${t.exit} (${t.reason})<br>${fmtR(t.net_r)} net`;
  Plotly.react("trade-chart", [
    { type: "candlestick", x: cd.time, open: cd.open, high: cd.high, low: cd.low, close: cd.close, name: "15m",
      increasing: { line: { color: c.muted, width: 1 }, fillcolor: c.surface }, decreasing: { line: { color: c.muted, width: 1 }, fillcolor: c.muted },
      hoverinfo: "x+y" },
    { type: "scatter", mode: "markers", name: "Entry", x: trades.map((t) => t.entry_time || t.signal_time), y: trades.map((t) => t.entry),
      marker: { symbol: trades.map((t) => (t.side === "short" ? "triangle-down" : "triangle-up")), size: 11,
                color: trades.map((t) => (won(t) ? c.up : c.down)), line: { width: 1, color: c.surface } },
      text: trades.map((t) => `${t.side === "short" ? "SHORT" : "LONG"}<br>` + tip(t)), hovertemplate: "%{text}<extra>Entry %{x}</extra>" },
    { type: "scatter", mode: "markers", name: "Exit", x: trades.map((t) => t.exit_time), y: trades.map((t) => t.exit),
      marker: { symbol: "x-thin", size: 9, color: c.text, line: { width: 2, color: c.text } },
      text: trades.map((t) => `${t.reason}<br>${fmtR(t.net_r)} net`), hovertemplate: "Exit ₹%{y}<br>%{text}<extra>%{x}</extra>" },
  ], baseLayout({
    height: 560, margin: { l: 60, r: 16, t: 10, b: 40 }, hovermode: "closest",
    xaxis: { ...baseLayout().xaxis, type: "date", range, rangeslider: { visible: true, thickness: 0.07, yaxis: { rangemode: "auto" } },
      rangebreaks: cd.intraday !== false ? [{ bounds: ["sat", "mon"] }, { pattern: "hour", bounds: [15.5, 9.25] }]
        : cd.interval === "1d" ? [{ bounds: ["sat", "mon"] }] : [] },
    yaxis: { ...baseLayout().yaxis, title: { text: "Price (₹)" }, range: fitY(cd, range), fixedrange: false },
  }), { ...plotConfig, scrollZoom: true });

  // keep the y-axis fitted to whatever window is visible as the user drags/zooms
  const el = $("#trade-chart");
  if (!el._fitBound) {
    el._fitBound = true;
    el.on("plotly_relayout", (ev) => {
      const r = ev["xaxis.range"] || (ev["xaxis.range[0]"] && [ev["xaxis.range[0]"], ev["xaxis.range[1]"]]);
      const cur = bt.candles[$("#chart-stock").value];
      if (r && cur) Plotly.relayout(el, { "yaxis.range": fitY(cur, r) });
    });
  }
}

function fitY(cd, range) {
  const [a, b] = range.map((x) => String(x).slice(0, 16));
  let lo = Infinity, hi = -Infinity;
  cd.time.forEach((t, i) => { if (t >= a && t <= b) { lo = Math.min(lo, cd.low[i]); hi = Math.max(hi, cd.high[i]); } });
  if (!Number.isFinite(lo)) return undefined;
  const pad = (hi - lo) * 0.08 || hi * 0.01;
  return [lo - pad, hi + pad];
}

function filteredTrades() {
  const f = $("#trade-filter").value;
  return bt.result.trades.filter((t) => !f || t.ticker === f);
}

function renderTradeTable() {
  const all = filteredTrades();
  const shown = all.slice(-500).reverse();  // newest first
  $("#trade-count").textContent = `${all.length.toLocaleString("en-IN")} trades` + (all.length > shown.length ? ` · latest ${shown.length} shown` : "");
  const head = `<thead><tr><th class="text">Symbol</th><th class="text">Side</th><th class="text">Entry time</th><th>Qty</th><th>Entry</th><th>Stop</th><th>Target</th>
    <th class="text">Exit time</th><th>Exit</th><th class="text">Reason</th><th>Net R</th><th>P&amp;L</th></tr></thead>`;
  const body = shown.map((t) => {
    const cls = t.net_r > 0 ? "pos" : "neg";
    return `<tr><td class="text sym">${esc(t.ticker.replace(/\.(NS|BO)$/, ""))}</td>
      <td class="text"><span class="side ${t.side || "long"}">${t.side === "short" ? "Short" : "Long"}</span></td>
      <td class="text">${t.entry_time || t.signal_time}</td><td>${t.qty}</td><td>${t.entry.toFixed(2)}</td><td>${t.stop.toFixed(2)}</td><td>${t.target.toFixed(2)}</td>
      <td class="text">${t.exit_time}</td><td>${t.exit.toFixed(2)}</td><td class="text muted">${t.reason}</td>
      <td class="${cls}">${fmtR(t.net_r)}</td><td class="${cls}">${inr(t.pnl, 0)}</td></tr>`;
  }).join("");
  $("#trade-table").innerHTML = head + `<tbody>${body || `<tr><td colspan="10" class="text muted">No trades.</td></tr>`}</tbody>`.replace('colspan="10"', 'colspan="12"');
}

function downloadTrades() {
  if (!bt.result) return;
  const cols = ["ticker", "side", "signal_time", "entry_time", "entry", "stop", "target", "risk", "exit_time", "exit", "reason", "bars_held", "gross_r", "net_r", "qty", "pnl"];
  const csv = [cols.join(","), ...filteredTrades().map((t) => cols.map((k) => t[k]).join(","))].join("\n");
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([csv], { type: "text/csv" }));
  a.download = `backtest_${bt.result.strategy.key}_${new Date().toISOString().slice(0, 10)}.csv`;
  a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}
