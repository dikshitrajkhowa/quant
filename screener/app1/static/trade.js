/* Trade page - live paper trading on Megabull. Depends on app.js. */
"use strict";

const BT_PARAMS_KEY = "nse-backtest-params-v1";   // shared with the Backtest page: "the strategy currently used in backtest"
const tr = { tickers: [], engines: {}, providers: {}, status: null, urlParams: null, origin: "" };

/* ------------------------------------------------------------------ setup */
function normTicker(raw) {
  let t = String(raw).trim().toUpperCase().replace(/\s+/g, "");
  if (!t) return null;
  if (!/\.(NS|BO)$/.test(t)) t += ".NS";
  return /^[A-Z0-9&_\-]+\.(NS|BO)$/.test(t) ? t : null;
}
const sym = (t) => t.replace(/\.(NS|BO)$/, "");
function lastBacktest() { try { return JSON.parse(localStorage.getItem(BT_PARAMS_KEY)) || {}; } catch { return {}; } }

function readUrl() {
  const q = new URLSearchParams(location.search);
  const tickers = (q.get("tickers") || "").split(",").map(normTicker).filter(Boolean);
  let params = null;
  try { params = q.get("params") ? JSON.parse(q.get("params")) : null; } catch { params = null; }
  return { tickers: [...new Set(tickers)], strategy: q.get("strategy"), source: q.get("source"), params, origin: q.get("origin") || "" };
}

function renderChips() {
  $("#tr-chips").innerHTML = tr.tickers.length
    ? tr.tickers.map((t) => `<span class="chip">${esc(sym(t))}<button data-rm="${esc(t)}" aria-label="Remove ${esc(sym(t))}">×</button></span>`).join("")
    : `<span class="hint">No stocks yet — add tickers, or pick them in the <a href="/screener">Screener</a> / <a href="/backtest">Backtest</a>.</span>`;
  $("#tr-chips").querySelectorAll("[data-rm]").forEach((b) => b.addEventListener("click", () => {
    tr.tickers = tr.tickers.filter((x) => x !== b.dataset.rm); renderChips();
  }));
  $("#tr-start").disabled = !tr.tickers.length;
}

function renderParams() {
  const e = tr.engines[$("#tr-strategy").value];
  if (!e) return;
  $("#tr-rules").innerHTML = e.rules.map((r) => `<li>${esc(r)}</li>`).join("");
  const lb = lastBacktest();
  const fromUrl = tr.urlParams && tr.urlParams.strategy === e.key ? tr.urlParams.params : null;
  const saved = fromUrl || lb[e.key] || {};
  const box = $("#tr-params"); box.innerHTML = "";
  let lastGroup = "";
  for (const p of e.params) {
    if (p.key === "days" || p.key === "cost_pct") continue;   // backtest-only settings
    if (p.group && p.group !== lastGroup) {
      lastGroup = p.group;
      const h = document.createElement("div"); h.className = "param-group"; h.textContent = p.group; box.appendChild(h);
    }
    const val = saved[p.key] ?? p.default;
    const wrap = document.createElement("label");
    if (p.help) wrap.title = p.help;
    if (p.kind === "choice") {
      wrap.className = "field";
      wrap.innerHTML = `<span>${esc(p.label)}</span><select data-tp="${p.key}">${(p.options || []).map(([v, l]) =>
        `<option value="${esc(v)}">${esc(l)}</option>`).join("")}</select>`;
      const sel = wrap.querySelector("select");
      sel.value = (p.options || []).some(([v]) => v === val) ? val : p.default;
      box.appendChild(wrap);
      continue;
    }
    if (p.kind === "bool") {
      wrap.className = "field toggle";
      wrap.innerHTML = `<span>${esc(p.label)}</span><span class="switch"><input type="checkbox" data-tp="${p.key}"><span class="slider"></span></span>`;
      wrap.querySelector("input").checked = !!val;
    } else if ((p.max - p.min) / p.step > 400) {
      wrap.className = "field";
      wrap.innerHTML = `<span>${esc(p.label)}</span><input type="number" data-tp="${p.key}" min="${p.min}" max="${p.max}" step="${p.step}">`;
      wrap.querySelector("input").value = val;
    } else {
      wrap.className = "field";
      wrap.innerHTML = `<span>${esc(p.label)} <output></output></span><input type="range" data-tp="${p.key}" min="${p.min}" max="${p.max}" step="${p.step}">`;
      const input = wrap.querySelector("input"), out = wrap.querySelector("output");
      input.value = val;
      const dec = p.kind === "int" ? 0 : (String(p.step).split(".")[1] || "").length;
      const show = () => (out.textContent = Number(input.value).toFixed(dec));
      show(); input.addEventListener("input", show);
    }
    box.appendChild(wrap);
  }
}

function currentParams() {
  const out = {};
  document.querySelectorAll("#tr-params [data-tp]").forEach((el) => {
    out[el.dataset.tp] = el.tagName === "SELECT" ? el.value : el.type === "checkbox" ? el.checked : Number(el.value);
  });
  return out;
}

/* ------------------------------------------------------------------ status */
function pill(text, cls) { return `<span class="pill ${cls}">${text}</span>`; }

function renderConnection(b) {
  const el = $("#conn");
  if (!b.configured) {
    el.innerHTML = `<div class="conn-row bad"><b>No API key.</b> Generate one under Profile at
      <a href="https://trade.megabull.in" target="_blank" rel="noopener">trade.megabull.in</a>, put it in
      <code>${esc(b.env_file)}</code> as <code>MEGABULL_API_KEY=your_key</code>, then click <b>Reload API key</b>.
      You can still start sessions in dry-run mode.</div>`;
  } else if (b.ok === false) {
    el.innerHTML = `<div class="conn-row bad"><b>Key ${esc(b.key_hint || "")} not accepted.</b> ${esc(b.error || "")}</div>`;
  } else if (b.ok) {
    const p = b.profile || {};
    el.innerHTML = `<div class="conn-row ok"><b>Connected</b>${p.name ? " as " + esc(p.name) : ""} · key ${esc(b.key_hint || "")}
      ${p.balance !== null && p.balance !== undefined ? ` · virtual balance <b>${typeof p.balance === "number" ? inr(p.balance, 0) : esc(p.balance)}</b>` : ""}</div>`;
  } else {
    el.innerHTML = `<div class="conn-row">Key ${esc(b.key_hint || "")} configured · checking…</div>`;
  }
}

function renderPills(st) {
  const e = st.engine;
  const live = e.sessions.filter((s) => !["stopped", "error"].includes(s.status)).length;
  $("#status-pills").innerHTML =
    pill(e.market_open ? "Market open" : "Market closed", e.market_open ? "ok" : "muted") +
    pill(e.running ? `Engine running · ${live} active` : "Engine stopped", e.running ? "ok" : "bad") +
    pill(`${e.now.slice(11, 16)} IST · square-off ${e.square_off}`, "muted");
}

const STATUS_LABEL = { waiting: ["Waiting for signal", "muted"], in_position: ["In position", "ok"],
  done_for_day: ["Done for today", "muted"], stopped: ["Stopped", "bad"], error: ["Error", "bad"] };

function renderSessions(st) {
  const ss = st.engine.sessions;
  $("#sess-count").textContent = ss.length ? `(${ss.length})` : "";
  const total = ss.reduce((a, s) => a + s.realized_pnl, 0);
  $("#pnl-total").innerHTML = ss.length ? `Realized P&amp;L <b class="${total >= 0 ? "pos" : "neg"}">${inr(total, 0)}</b>` : "";
  $("#stop-all").disabled = !ss.some((s) => s.status !== "stopped");
  const head = `<thead><tr><th class="text">Stock · strategy</th><th class="text">Status</th>
    <th>Position</th><th>Stop</th><th>Target</th><th>Trades</th><th>Realized P&amp;L</th><th class="text">Last update</th><th></th></tr></thead>`;
  const body = ss.map((s) => {
    const [label, cls] = STATUS_LABEL[s.status] || [s.status, "muted"];
    const p = s.position;
    const eng = tr.engines[s.strategy];
    const actions = [
      p ? `<button class="btn small" data-act="exit" data-id="${s.id}">Exit now</button>` : "",
      s.status !== "stopped" ? `<button class="btn small" data-act="stop" data-id="${s.id}">Stop</button>` : "",
      s.status === "stopped" && !p ? `<button class="btn small" data-act="remove" data-id="${s.id}">Remove</button>` : "",
    ].join(" ");
    const stratShort = (eng ? eng.name : s.strategy) + (s.params.rr ? ` · R:R ${s.params.rr}` : "");
    return `<tr>
      <td class="text"><span class="sym">${esc(s.symbol)}</span>${s.dry_run ? ' <span class="pill muted small">dry run</span>' : ""}
        <div class="sub-line">${esc(stratShort)} · ${esc(tr.providers[s.source]?.name || s.source)} · ${s.params.risk_pct ? `${inr(s.capital, 0)} account, ${s.params.risk_pct}% risk` : `${inr(s.capital, 0)}/trade`} · ${esc(s.timeframe_label || "15 minutes")} · ${s.square_off ? `square-off ${s.square_off}` : "held overnight (delivery)"}</div></td>
      <td class="text"><span class="pill ${cls}">${label}</span></td>
      <td>${p ? `<span class="side ${p.side < 0 ? "short" : "long"}">${p.side < 0 ? "Short" : "Long"}</span> ${p.qty} @ ${inr(p.entry)}${p.be ? ' <span class="pill muted small">BE</span>' : ""}` : "–"}</td><td>${p ? inr(p.stop) : "–"}</td><td>${p ? inr(p.target) : "–"}</td>
      <td>${s.trades.length}</td>
      <td class="${s.realized_pnl > 0 ? "pos" : s.realized_pnl < 0 ? "neg" : ""}">${s.trades.length ? inr(s.realized_pnl, 0) : "–"}</td>
      <td class="text muted msg">${esc(s.message || "")}</td>
      <td class="actions">${actions}</td></tr>`;
  }).join("");
  $("#sess-table").innerHTML = head + `<tbody>${body || `<tr><td colspan="9" class="text muted">No sessions yet. Choose stocks above and click <b>Start paper trading</b>.</td></tr>`}</tbody>`;
  $("#sess-table").querySelectorAll("[data-act]").forEach((b) => b.addEventListener("click", () => sessionAction(b.dataset.act, b.dataset.id)));
}

function renderLog(st) {
  const items = st.engine.log.slice(0, 120);
  $("#activity").innerHTML = items.length ? items.map((l) =>
    `<li class="${l.level}"><time>${esc(l.time.slice(5, 16))}</time>${l.symbol ? `<b>${esc(l.symbol)}</b>` : ""}<span>${esc(l.message)}</span></li>`).join("")
    : `<li class="muted"><span>Nothing yet.</span></li>`;
}

function renderTrades(st) {
  const rows = st.engine.sessions.flatMap((s) => s.trades.map((t) => ({ ...t, symbol: s.symbol }))).sort((a, b) => b.exit_time.localeCompare(a.exit_time));
  tr.closed = rows;
  const head = `<thead><tr><th class="text">Symbol</th><th class="text">Side</th><th class="text">Entry time</th><th>Qty</th><th>Entry</th><th>Stop</th><th>Target</th>
    <th class="text">Exit time</th><th>Exit</th><th class="text">Reason</th><th>R</th><th>P&amp;L</th></tr></thead>`;
  const body = rows.map((t) => {
    const cls = t.pnl > 0 ? "pos" : t.pnl < 0 ? "neg" : "";
    return `<tr><td class="text sym">${esc(t.symbol)}${t.dry_run ? ' <span class="pill muted small">dry</span>' : ""}</td>
      <td class="text"><span class="side ${t.side || "long"}">${t.side === "short" ? "Short" : "Long"}</span></td>
      <td class="text">${t.entry_time.slice(0, 16)}</td><td>${t.qty}</td><td>${t.entry.toFixed(2)}</td><td>${t.stop.toFixed(2)}</td>
      <td>${t.target.toFixed(2)}</td><td class="text">${t.exit_time.slice(0, 16)}</td><td>${t.exit.toFixed(2)}</td>
      <td class="text muted">${esc(t.reason)}</td><td class="${cls}">${(t.r >= 0 ? "+" : "") + t.r.toFixed(2)}R</td><td class="${cls}">${inr(t.pnl, 0)}</td></tr>`;
  }).join("");
  $("#trades-table").innerHTML = head + `<tbody>${body || `<tr><td colspan="11" class="text muted">No closed trades yet.</td></tr>`}</tbody>`.replace('colspan="11"', 'colspan="12"');
}

async function refresh(checkBroker = false) {
  try {
    const st = await api("/api/trade/status" + (checkBroker ? "?check_broker=true" : ""));
    tr.status = st;
    if (checkBroker || !tr.brokerShown) { renderConnection(st.broker); tr.brokerShown = checkBroker; }
    renderPills(st); renderSessions(st); renderLog(st); renderTrades(st);
  } catch (e) {
    showAlert("Lost contact with the server: " + e.message, "error");
  }
}

/* ------------------------------------------------------------------ broker panels */
function genericTable(rows, prefer) {
  if (!rows || !rows.length) return `<p class="hint">None.</p>`;
  const keys = Object.keys(rows[0]);
  const pickKeys = prefer.map((c) => keys.find((k) => k.toLowerCase().replace(/[^a-z]/g, "") === c)).filter(Boolean);
  const cols = (pickKeys.length >= 3 ? pickKeys : keys.filter((k) => typeof rows[0][k] !== "object")).slice(0, 8);
  return `<div class="table-wrap small"><table><thead><tr>${cols.map((c) => `<th class="text">${esc(c)}</th>`).join("")}</tr></thead><tbody>${
    rows.slice(0, 50).map((r) => `<tr>${cols.map((c) => `<td class="text">${esc(r[c] ?? "")}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
}

async function loadBroker() {
  const box = $("#broker");
  box.innerHTML = `<p class="hint">Loading from Megabull…</p>`;
  try {
    const b = await api("/api/trade/broker");
    const err = b.errors ? `<p class="alert error">${esc(Object.values(b.errors)[0])}</p>` : "";
    box.innerHTML = err +
      `<h4>Positions</h4>` + genericTable(b.positions, ["tradingsymbol", "symbol", "quantity", "qty", "netqty", "averageprice", "avgprice", "ltp", "lastprice", "pnl", "mtm"]) +
      `<h4>Orders</h4>` + genericTable(b.orders, ["orderid", "tradingsymbol", "symbol", "transactiontype", "side", "quantity", "qty", "ordertype", "price", "averageprice", "status", "ordertime", "createdat"]);
  } catch (e) {
    box.innerHTML = `<p class="hint">${esc(e.message)}</p>`;
  }
}

async function showMapping() {
  const box = $("#mapping");
  if (!box.hidden) { box.hidden = true; return; }
  box.hidden = false; box.innerHTML = `<p class="hint">Reading Megabull's API spec…</p>`;
  try {
    const m = await api("/api/trade/mapping?refresh=true");
    const ours = { instrument_id: "Instrument id", symbol: "Symbol", exchange: "Exchange", side: "Buy/Sell", quantity: "Quantity",
      order_type: "Order type", price: "Price", trigger_price: "Trigger price", product: "Product", validity: "Validity" };
    const rows = Object.entries(ours).map(([k, label]) => {
      const f = m.fields[k];
      const vals = m.values[k] ? Object.entries(m.values[k]).map(([a, b]) => `${a}→${b}`).join(", ") : "";
      return `<tr><td class="text">${label}</td><td class="text">${f ? `<code>${esc(f)}</code>` : '<span class="muted">not sent</span>'}</td><td class="text muted">${esc(vals)}</td></tr>`;
    }).join("");
    box.innerHTML = `<p class="hint">Source: <b>${esc(m.source)}</b>${m.source.startsWith("defaults") ? " — Megabull's spec couldn't be downloaded, so conventional field names are used. Send a test order to confirm." : ""}</p>
      ${m.unmapped_required.length ? `<p class="alert">Required by Megabull but not filled: <code>${m.unmapped_required.map(esc).join("</code>, <code>")}</code>. Add them to <code>app1/megabull_mapping.json</code> (see megabull.py).</p>` : ""}
      <div class="table-wrap small"><table><thead><tr><th class="text">Our field</th><th class="text">Megabull field</th><th class="text">Values</th></tr></thead><tbody>${rows}</tbody></table></div>`;
  } catch (e) {
    box.innerHTML = `<p class="alert error">${esc(e.message)}</p>`;
  }
}

async function testOrder() {
  const t = prompt("Test with which NSE stock? A 1-share BUY then SELL is sent to Megabull (virtual money).", "SBIN");
  if (!t) return;
  const out = $("#test-result");
  out.hidden = false; out.textContent = "Sending…";
  try {
    const r = await api("/api/trade/test-order", { ticker: t, round_trip: true });
    out.textContent = (r.ok ? "✓ Orders accepted by Megabull.\n\n" : "✕ " + r.error + "\n\n") + JSON.stringify(r, null, 2);
    out.className = "raw " + (r.ok ? "ok" : "bad");
    loadBroker();
  } catch (e) {
    out.textContent = "✕ " + e.message; out.className = "raw bad";
  }
}

/* ------------------------------------------------------------------ actions */
async function start() {
  const dry = $("#tr-dry").checked;
  const params = currentParams();
  const n = tr.tickers.length;
  const msg = dry
    ? `Start a DRY RUN on ${n} stock(s)? Signals are logged; no orders are sent.`
    : `Start paper trading ${n} stock(s) on Megabull with up to ${inr(params.capital, 0)} (virtual) per trade?\nOrders are sent automatically when the strategy signals.`;
  if (!confirm(msg)) return;
  const btn = $("#tr-start"); btn.disabled = true;
  try {
    const r = await api("/api/trade/sessions", { tickers: tr.tickers, strategy: $("#tr-strategy").value, params,
      source: $("#tr-source").value, dry_run: dry, origin: tr.origin || "manual" });
    showAlert(r.created.length ? null : "Those stocks already have active sessions.");
    $("#tr-start-hint").textContent = r.created.length ? `Started ${r.created.length} session(s).` : "";
    await refresh();
  } catch (e) {
    showAlert(e.message, "error");
  } finally {
    btn.disabled = !tr.tickers.length;
  }
}

async function sessionAction(act, id) {
  try {
    if (act === "exit" && confirm("Sell this position at market now?")) await api(`/api/trade/sessions/${id}/exit`, {});
    if (act === "stop" && confirm("Stop this session? Any open position is sold at market.")) await api(`/api/trade/sessions/${id}/stop`, { square_off: true });
    if (act === "remove") await fetch(`/api/trade/sessions/${id}`, { method: "DELETE" });
    await refresh();
  } catch (e) { showAlert(e.message, "error"); }
}

function tfNote() {
  const tf = document.querySelector('[data-tp="timeframe"]')?.value || "15m";
  $("#tr-start-hint").textContent = ["5m", "15m", "30m", "1h"].includes(tf)
    ? "Intraday: checks each completed candle, squares off at 15:15."
    : "Daily/weekly/monthly: checks the last completed bar each morning after the open and orders at market; "
      + "delivery (CNC), long only, positions held across days. Start sessions before 09:15 so a fresh signal isn't missed.";
}

/* ------------------------------------------------------------------ init */
async function init() {
  const u = readUrl();
  tr.urlParams = u; tr.origin = u.origin;
  tr.tickers = u.tickers.length ? u.tickers : Selection.load().map((s) => s.ticker);
  renderChips();

  const [engines, prov] = await Promise.all([api("/api/backtest/strategies"), api("/api/backtest/providers").catch(() => ({}))]);
  engines.forEach((e) => (tr.engines[e.key] = e));
  tr.providers = prov;
  $("#tr-strategy").innerHTML = engines.map((e) => `<option value="${e.key}">${esc(e.name)}</option>`).join("");
  $("#tr-source").innerHTML = Object.entries(prov).map(([k, p]) =>
    `<option value="${k}" ${p.available ? "" : "disabled"}>${esc(p.name)}${p.available ? "" : " (not installed)"}</option>`).join("");
  const lb = lastBacktest();
  const strat = (u.strategy && tr.engines[u.strategy]) ? u.strategy : (lb._strategy && tr.engines[lb._strategy] ? lb._strategy : engines[0]?.key);
  if (strat) $("#tr-strategy").value = strat;
  const src = u.source || lb._source;
  if (src && prov[src]?.available) $("#tr-source").value = src;
  renderParams();
  tfNote();
  $("#origin-note").textContent = u.origin === "backtest" ? "Strategy and settings copied from your backtest."
    : (lb[strat] ? "Using the settings from your last backtest." : "");

  $("#tr-strategy").addEventListener("change", () => { renderParams(); tfNote(); });
  document.addEventListener("change", (e) => { if (e.target.dataset?.tp === "timeframe") tfNote(); });
  $("#tr-add").addEventListener("submit", (e) => {
    e.preventDefault();
    const add = $("#tr-add-input").value.split(/[,\s]+/).map(normTicker).filter(Boolean);
    tr.tickers = [...new Set([...tr.tickers, ...add])]; $("#tr-add-input").value = ""; renderChips();
  });
  $("#tr-start").addEventListener("click", start);
  $("#stop-all").addEventListener("click", async () => {
    if (!confirm("Stop ALL sessions and sell every open position at market?")) return;
    await api("/api/trade/stop-all", { square_off: true }); refresh();
  });
  $("#reload-env").addEventListener("click", async () => {
    const b = await api("/api/trade/reload-env", {}); renderConnection(b); if (b.ok) loadBroker();
  });
  $("#show-mapping").addEventListener("click", showMapping);
  $("#test-order").addEventListener("click", testOrder);
  $("#broker-refresh").addEventListener("click", loadBroker);
  $("#trades-csv").addEventListener("click", () => {
    const cols = ["symbol", "side", "entry_time", "qty", "entry", "stop", "target", "exit_time", "exit", "reason", "r", "pnl", "entry_order", "exit_order", "dry_run"];
    const csv = [cols.join(","), ...(tr.closed || []).map((t) => cols.map((k) => t[k]).join(","))].join("\n");
    const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([csv], { type: "text/csv" }));
    a.download = `paper_trades_${new Date().toISOString().slice(0, 10)}.csv`; a.click();
  });

  await refresh(true);
  if (tr.status?.broker.ok) loadBroker(); else $("#broker").innerHTML = `<p class="hint">Connect your API key to see Megabull orders and positions.</p>`;
  setInterval(() => { if (!document.hidden) refresh(false); }, 5000);
}

init();
