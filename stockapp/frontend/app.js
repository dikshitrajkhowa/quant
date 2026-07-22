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
  chartImg: document.getElementById("chartImg"),
  tapeTrack: document.getElementById("tapeTrack"),
};

let strategies = [];
let selectedExchange = "NSE";

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

  els.chartImg.src = `data:image/png;base64,${payload.chart_png_base64}`;
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
