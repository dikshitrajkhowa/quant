/* Shared utilities for every page: nav, theme, API, formatting, chart defaults, stock selection. */
"use strict";

const $ = (sel) => document.querySelector(sel);
const THEME_KEY = "nse-screener-settings-v1-theme";
const SELECTION_KEY = "nse-screener-selection-v1";

/* ---------- Theme (applied immediately to avoid a flash) ---------- */
try { const t = localStorage.getItem(THEME_KEY); if (t) document.documentElement.dataset.theme = t; } catch { /* ignore */ }

function toggleTheme() {
  const root = document.documentElement;
  const dark = root.dataset.theme ? root.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
  root.dataset.theme = dark ? "light" : "dark";
  try { localStorage.setItem(THEME_KEY, root.dataset.theme); } catch { /* ignore */ }
  document.dispatchEvent(new CustomEvent("themechange"));
}

/* ---------- Top navigation ---------- */
const NAV = [
  { href: "/screener", label: "Screener" },
  { href: "/backtest", label: "Backtest" },
  { href: "/trade", label: "Trade" },
];
function renderNav() {
  const el = document.getElementById("topnav");
  if (!el) return;
  const path = location.pathname.replace(/\/$/, "") || "/";
  el.innerHTML = `
    <a class="brand" href="/"><span class="brand-mark" aria-hidden="true"></span><span>NSE Quant</span></a>
    <nav class="nav-links">${NAV.map((n) => `<a href="${n.href}" class="${path === n.href ? "active" : ""}">${n.label}</a>`).join("")}</nav>
    <button class="btn icon" id="theme-toggle" title="Toggle light/dark theme" aria-label="Toggle theme">
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M12 3a9 9 0 0 0 0 18z" fill="currentColor"/></svg>
    </button>`;
  el.querySelector("#theme-toggle").addEventListener("click", toggleTheme);
}
document.addEventListener("DOMContentLoaded", renderNav);

/* ---------- Utilities ---------- */
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const colors = () => ({
  text: css("--text"), text2: css("--text-2"), muted: css("--muted"), grid: css("--grid"),
  accent: css("--accent"), band: css("--accent-soft"), up: css("--up"), down: css("--down"),
  others: css("--others"), surface: css("--surface"),
  overlay: { "20 DMA": css("--ma20"), "50 DMA": css("--ma50"), "150 DMA": css("--ma150"), "200 DMA": css("--ma200") },
  series: [1, 2, 3, 4, 5, 6, 7, 8].map((i) => css(`--series-${i}`)),
});

function fmtPy(fmt, v) {            // python "{:.2f}%" -> JS
  if (v === null || v === undefined || Number.isNaN(v)) return "–";
  return fmt.replace(/\{:\.(\d+)f\}/, (_, d) => Number(v).toFixed(+d));
}
const inr = (v, d = 2) => (v === null || v === undefined) ? "–"
  : (v < 0 ? "−₹" : "₹") + Math.abs(Number(v)).toLocaleString("en-IN", { minimumFractionDigits: d, maximumFractionDigits: d });
const pct = (v, d = 2) => (v === null || v === undefined) ? "–" : (v >= 0 ? "+" : "") + Number(v).toFixed(d) + "%";
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(path, body) {
  const res = await fetch(path, body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  if (!res.ok) {
    let msg = res.statusText;
    try { const j = await res.json(); msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail); } catch { /* ignore */ }
    const err = new Error(msg); err.status = res.status; throw err;
  }
  return res.json();
}

const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function showAlert(msg, kind = "warn") {
  const el = $("#alert");
  if (!el) return;
  if (!msg) { el.hidden = true; return; }
  el.textContent = msg; el.className = "alert" + (kind === "error" ? " error" : ""); el.hidden = false;
}

/* ---------- Chart defaults ---------- */
function baseLayout(extra = {}) {
  const c = colors();
  return {
    paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)",
    font: { family: "Inter, system-ui, -apple-system, Segoe UI, Roboto, sans-serif", size: 12, color: c.text2 },
    margin: { l: 50, r: 16, t: 10, b: 36 },
    xaxis: { gridcolor: c.grid, zerolinecolor: c.grid, linecolor: c.grid },
    yaxis: { gridcolor: c.grid, zerolinecolor: c.grid, linecolor: c.grid },
    hoverlabel: { bgcolor: c.surface, bordercolor: c.grid, font: { color: c.text } },
    showlegend: false, ...extra,
  };
}
const plotConfig = { displayModeBar: false, responsive: true };

function sparkline(vals, w = 110, h = 26) {
  const v = (vals || []).filter((x) => x !== null);
  if (v.length < 2) return "";
  const min = Math.min(...v), max = Math.max(...v), span = max - min || 1;
  const pts = vals.map((x, i) => x === null ? null : `${(i / (vals.length - 1) * (w - 2) + 1).toFixed(1)},${(h - 2 - (x - min) / span * (h - 4)).toFixed(1)}`).filter(Boolean).join(" ");
  const up = v[v.length - 1] >= v[0];
  return `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" aria-hidden="true"><polyline points="${pts}" fill="none" stroke="${up ? "var(--up)" : "var(--down)"}" stroke-width="1.5" stroke-linejoin="round"/></svg>`;
}

/* ---------- Stock selection (shared between Screener and Backtest) ----------
   Stored as an ordered list of {ticker, symbol, source} in sessionStorage so it
   survives strategy changes and page navigation within the browser tab. */
const Selection = {
  load() { try { return JSON.parse(sessionStorage.getItem(SELECTION_KEY)) || []; } catch { return []; } },
  save(list) { try { sessionStorage.setItem(SELECTION_KEY, JSON.stringify(list)); } catch { /* ignore */ } },
  has(ticker) { return this.load().some((s) => s.ticker === ticker); },
  add(items) {
    const list = this.load();
    for (const it of items) if (!list.some((s) => s.ticker === it.ticker)) list.push(it);
    this.save(list); return list;
  },
  remove(tickers) {
    const drop = new Set(tickers);
    const list = this.load().filter((s) => !drop.has(s.ticker));
    this.save(list); return list;
  },
  clear() { this.save([]); return []; },
  backtestUrl(list = this.load()) {
    // commas kept literal so the URL stays readable: /backtest?tickers=TCS.NS,INFY.NS&source=golden_cross
    const enc = (xs) => xs.map(encodeURIComponent).join(",");
    const parts = [];
    if (list.length) parts.push("tickers=" + enc(list.map((s) => s.ticker)));
    const sources = [...new Set(list.map((s) => s.source).filter(Boolean))];
    if (sources.length) parts.push("source=" + enc(sources));
    return "/backtest" + (parts.length ? "?" + parts.join("&") : "");
  },
  /* /trade?tickers=A.NS,B.NS&origin=backtest&strategy=breakout_15m&source=yahoo&params=<json> */
  tradeUrl(tickers, extra = {}) {
    const parts = [];
    if (tickers.length) parts.push("tickers=" + tickers.map(encodeURIComponent).join(","));
    for (const [k, v] of Object.entries(extra)) {
      if (v === undefined || v === null || v === "") continue;
      parts.push(k + "=" + encodeURIComponent(typeof v === "object" ? JSON.stringify(v) : v));
    }
    return "/trade" + (parts.length ? "?" + parts.join("&") : "");
  },
};
