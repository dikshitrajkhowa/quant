const API_BASE = ""; // same-origin; change if backend runs on a different host/port

const els = {
  exchangeToggle: document.getElementById("exchangeToggle"),
  stockSelect: document.getElementById("stockSelect"),
  strategySelect: document.getElementById("strategySelect"),
  strategyHint: document.getElementById("strategyHint"),
  paramFields: document.getElementById("paramFields"),
  startDate: document.getElementById("startDate"),
  capital: document.getElementById("capital"),
  runBtn: document.getElementById("runBtn"),
  errorMsg: document.getElementById("errorMsg"),
  statusDot: document.getElementById("statusDot"),
  statusText: document.getElementById("statusText"),
  emptyState: document.getElementById("emptyState"),
  resultsContent: document.getElementById("resultsContent"),
  decisionBadge: document.getElementById("decisionBadge"),
  decisionLabel: document.getElementById("decisionLabel"),
  tickerName: document.getElementById("tickerName"),
  strategyName: document.getElementById("strategyName"),
  explanationText: document.getElementById("explanationText"),
  periodText: document.getElementById("periodText"),
  metricsGrid: document.getElementById("metricsGrid"),
  priceChartCanvas: document.getElementById("priceChart"),
  equityChartCanvas: document.getElementById("equityChart"),
  priceChartTitle: document.getElementById("priceChartTitle"),
  tapeTrack: document.getElementById("tapeTrack"),
};

let strategies = [];
let selectedExchange = "NSE";
let priceChart = null;
let equityChart = null;

const INDICATOR_COLORS = ["#f2994a", "#bb6bd9", "#e0a458", "#5ec8e0"];

function setStatus(state) {
  els.statusDot.className = "dot " + (state === "busy" ? "busy" : state === "live" ? "live" : "");
  els.statusText.textContent = state;
}

function buildTape(stocks) {
  const items = stocks.map((s) => {
    const up = Math.random() > 0.5;
    const pct = (Math.random() * 2.5).toFixed(2);
    return `<span>${s.symbol} <span class="${up ? "up" : "down"}">${up ? "▲" : "▼"} ${pct}%</span></span>`;
  });
  els.tapeTrack.innerHTML = items.join("") + items.join(""); // duplicate for seamless loop
}

async function loadStocks() {
  const res = await fetch(`${API_BASE}/api/stocks`);
  const stocks = await res.json();
  els.stockSelect.innerHTML = stocks
    .map((s) => `<option value="${s.symbol}">${s.symbol} — ${s.name}</option>`)
    .join("");
  buildTape(stocks);
}

async function loadStrategies() {
  const res = await fetch(`${API_BASE}/api/strategies`);
  strategies = await res.json();
  els.strategySelect.innerHTML = strategies
    .map((s) => `<option value="${s.key}">${s.label}</option>`)
    .join("");
  renderStrategyParams();
}

function renderStrategyParams() {
  const strat = strategies.find((s) => s.key === els.strategySelect.value);
  if (!strat) return;
  els.strategyHint.textContent = strat.description;
  els.paramFields.innerHTML = strat.params
    .map(
      (p) => `
      <div class="param-field">
        <label for="param-${p.name}">${p.label}</label>
        <input type="number" id="param-${p.name}" value="${p.default}" />
      </div>`
    )
    .join("");
}

function collectParams() {
  const strat = strategies.find((s) => s.key === els.strategySelect.value);
  const params = {};
  strat.params.forEach((p) => {
    const el = document.getElementById(`param-${p.name}`);
    params[p.name] = Number(el.value);
  });
  return params;
}

function decisionClass(decision) {
  switch (decision) {
    case "BUY": return "buy";
    case "SELL": return "sell";
    case "HOLD": return "hold";
    default: return "stay-out";
  }
}

function metricCard(label, value, cls) {
  return `
    <div class="metric-card">
      <div class="metric-label">${label}</div>
      <div class="metric-value ${cls || ""}">${value}</div>
    </div>`;
}

function renderResults(payload) {
  els.emptyState.hidden = true;
  els.resultsContent.hidden = false;

  els.decisionBadge.className = "decision-badge " + decisionClass(payload.decision);
  els.decisionLabel.textContent = payload.decision;
  els.tickerName.textContent = payload.ticker;
  els.strategyName.textContent = payload.strategy;
  els.explanationText.textContent = payload.explanation;
  els.periodText.textContent = `${payload.period.start} → ${payload.period.end}`;

  const m = payload.metrics;
  els.metricsGrid.innerHTML = [
    metricCard("Total return", `${m.total_return_pct}%`, m.total_return_pct >= 0 ? "pos" : "neg"),
    metricCard("CAGR", `${m.CAGR_pct}%`, m.CAGR_pct >= 0 ? "pos" : "neg"),
    metricCard("Sharpe", m.sharpe),
    metricCard("Max drawdown", `${m.max_drawdown_pct}%`, "neg"),
    metricCard("Trades", m.trades),
    metricCard("Buy & hold CAGR", `${m.benchmark_CAGR_pct}%`),
  ].join("");

  renderCharts(payload);
}

const CHART_FONT = { family: "IBM Plex Mono", size: 11 };
const GRID_COLOR = "rgba(255,255,255,0.06)";
const TICK_COLOR = "#8890a0";

function baseChartOptions(yFormatter) {
  return {
    responsive: true,
    maintainAspectRatio: false,
    animation: { duration: 250 },
    interaction: { mode: "index", intersect: false },
    plugins: {
      legend: {
        labels: { color: TICK_COLOR, font: CHART_FONT, boxWidth: 12, usePointStyle: true },
      },
      tooltip: {
        mode: "index",
        intersect: false,
        backgroundColor: "#1e232e",
        borderColor: "#2a3040",
        borderWidth: 1,
        titleColor: "#e9eaee",
        bodyColor: "#e9eaee",
        titleFont: CHART_FONT,
        bodyFont: CHART_FONT,
        padding: 10,
        callbacks: yFormatter ? { label: (ctx) => yFormatter(ctx) } : undefined,
      },
    },
    scales: {
      x: {
        grid: { color: GRID_COLOR },
        ticks: { color: TICK_COLOR, font: CHART_FONT, maxTicksLimit: 8 },
      },
      y: {
        grid: { color: GRID_COLOR },
        ticks: { color: TICK_COLOR, font: CHART_FONT },
      },
    },
  };
}

function renderCharts(payload) {
  const cd = payload.chart_data;
  els.priceChartTitle.textContent = `${payload.ticker} — ${payload.strategy}`;

  const priceDatasets = [
    {
      label: "Close",
      data: cd.close,
      borderColor: "#4c9aff",
      backgroundColor: "transparent",
      borderWidth: 1.4,
      pointRadius: 0,
      tension: 0,
    },
  ];
  Object.keys(cd.indicators).forEach((name, i) => {
    priceDatasets.push({
      label: name,
      data: cd.indicators[name],
      borderColor: INDICATOR_COLORS[i % INDICATOR_COLORS.length],
      backgroundColor: "transparent",
      borderWidth: 1.2,
      pointRadius: 0,
      tension: 0,
    });
  });

  if (priceChart) priceChart.destroy();
  priceChart = new Chart(els.priceChartCanvas, {
    type: "line",
    data: { labels: cd.dates, datasets: priceDatasets },
    options: baseChartOptions((ctx) => `${ctx.dataset.label}: ${ctx.parsed.y ?? "—"}`),
  });

  if (equityChart) equityChart.destroy();
  equityChart = new Chart(els.equityChartCanvas, {
    type: "line",
    data: {
      labels: cd.dates,
      datasets: [
        {
          label: "Strategy",
          data: cd.equity,
          borderColor: "#4fb6a6",
          backgroundColor: "transparent",
          borderWidth: 1.6,
          pointRadius: 0,
          tension: 0,
        },
        {
          label: "Buy & Hold",
          data: cd.benchmark_equity,
          borderColor: "#888888",
          backgroundColor: "transparent",
          borderDash: [5, 4],
          borderWidth: 1.3,
          pointRadius: 0,
          tension: 0,
        },
      ],
    },
    options: baseChartOptions((ctx) => `${ctx.dataset.label}: ₹${ctx.parsed.y?.toLocaleString("en-IN") ?? "—"}`),
  });
}

async function runBacktest() {
  els.errorMsg.textContent = "";
  els.runBtn.disabled = true;
  setStatus("busy");

  const body = {
    symbol: els.stockSelect.value,
    exchange: selectedExchange,
    strategy: els.strategySelect.value,
    start: els.startDate.value || null,
    capital: Number(els.capital.value) || 100000,
    params: collectParams(),
  };

  try {
    const res = await fetch(`${API_BASE}/api/backtest`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(err.detail || "Request failed");
    }
    const payload = await res.json();
    renderResults(payload);
    setStatus("live");
  } catch (e) {
    els.errorMsg.textContent = e.message;
    setStatus("idle");
  } finally {
    els.runBtn.disabled = false;
  }
}

els.exchangeToggle.addEventListener("click", (e) => {
  const btn = e.target.closest(".seg-btn");
  if (!btn) return;
  [...els.exchangeToggle.children].forEach((b) => b.classList.remove("active"));
  btn.classList.add("active");
  selectedExchange = btn.dataset.value;
});

els.strategySelect.addEventListener("change", renderStrategyParams);
els.runBtn.addEventListener("click", runBacktest);

(async function init() {
  const threeYearsAgo = new Date();
  threeYearsAgo.setFullYear(threeYearsAgo.getFullYear() - 3);
  els.startDate.value = threeYearsAgo.toISOString().slice(0, 10);

  await Promise.all([loadStocks(), loadStrategies()]);
})();
