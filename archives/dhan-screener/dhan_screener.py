"""
Dhan-powered NSE stock screener with Large / Mid / Small cap buckets and an
"explain why" table for every pick.

  python dhan_screener.py --demo                         # no credentials needed, synthetic data
  python dhan_screener.py --mode swing --top 10          # real data via Dhan
  python dhan_screener.py --mode intraday --live --top 8 --capital 200000 --risk-pct 0.5
  python dhan_screener.py --mode swing --md              # also print Markdown tables
  python dhan_screener.py --config "Swing - large caps"  # run a saved configuration (configs.json)
  python dhan_screener.py --list-configs
  streamlit run app.py                                   # browser UI to edit, save and run configurations

Credentials come from .env (see .env.example). Outputs: screener_report.html (tables with
reasons), screener_scores.csv, console summary.

Cap buckets use NSE's Nifty 100 / Midcap 150 / Smallcap 250 lists, which follow SEBI's
definition (rank 1–100 large, 101–250 mid, 251–500 small by full market cap).
Pass --amfi with AMFI's official list (symbol,category CSV) to override.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import html
import json
import math
import pickle
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import StringIO
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd
import requests

HERE = Path(__file__).resolve().parent
CACHE = HERE / ".dhan_cache"
CACHE.mkdir(exist_ok=True)
CONFIGS_PATH = HERE / "configs.json"
CAPS = ["large", "mid", "small"]
CAP_LABEL = {"large": "Large caps", "mid": "Mid caps", "small": "Small caps"}
INDEX_FILES = {
    "large": "ind_nifty100list.csv",
    "mid": "ind_niftymidcap150list.csv",
    "small": "ind_niftysmallcap250list.csv",
}
INDEX_BASE = "https://niftyindices.com/IndexConstituent/"
UA = {"User-Agent": "Mozilla/5.0"}

# ════════════════════════════ 1. Universe + cap buckets ═══════════════════════

def _read_index_csv(text: str) -> pd.DataFrame:
    df = pd.read_csv(StringIO(text))
    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]
    return df.rename(columns={"company_name": "name", "isin_code": "isin"})


def load_universe(index_dir: str | None = None, amfi_csv: str | None = None) -> pd.DataFrame:
    """symbol-indexed frame: name, industry, isin, cap."""
    frames = []
    for cap, fname in INDEX_FILES.items():
        cached = CACHE / fname
        if index_dir:
            text = (Path(index_dir) / fname).read_text()
        elif cached.exists() and time.time() - cached.stat().st_mtime < 7 * 86400:
            text = cached.read_text()
        else:
            r = requests.get(INDEX_BASE + fname, headers=UA, timeout=30)
            r.raise_for_status()
            text = r.text
            cached.write_text(text)
        frames.append(_read_index_csv(text).assign(cap=cap))
    uni = pd.concat(frames).drop_duplicates("symbol").set_index("symbol")
    uni = uni[["name", "industry", "isin", "cap"]]
    if amfi_csv:
        amfi = pd.read_csv(amfi_csv)
        amfi.columns = [c.strip().lower() for c in amfi.columns]
        cat = amfi.set_index("symbol")["category"].str.lower().str.split().str[0]
        uni["cap"] = cat.reindex(uni.index).where(lambda s: s.isin(CAPS)).fillna(uni["cap"])
    return uni


def attach_security_ids(uni: pd.DataFrame, equities: pd.DataFrame, log: Callable = print) -> pd.DataFrame:
    """Map NSE symbols to Dhan security IDs, by ISIN first, then by symbol."""
    by_isin = equities.dropna(subset=["isin"]).drop_duplicates("isin").set_index("isin")
    by_sym = equities.drop_duplicates("symbol").set_index("symbol")
    out = uni.copy()
    out["security_id"] = out["isin"].map(by_isin["security_id"])
    out["security_id"] = out["security_id"].fillna(pd.Series(out.index, index=out.index).map(by_sym["security_id"]))
    flag = out["isin"].map(by_isin["asm_gsm"]).fillna(pd.Series(out.index, index=out.index).map(by_sym["asm_gsm"]))
    out["asm_gsm"] = flag.fillna("N").eq("Y")
    missing = out["security_id"].isna().sum()
    if missing:
        log(f"  note: {missing} symbols not found in Dhan instrument master, skipped")
    return out.dropna(subset=["security_id"])

# ════════════════════════════ 2. Market data via Dhan ═════════════════════════

def fetch_history(client, uni: pd.DataFrame, days: int = 400, log: Callable = print,
                  refresh: bool = False) -> dict[str, pd.DataFrame]:
    """Daily candles for every stock, cached for the day. ~500 stocks ≈ 2–3 min first run."""
    f = CACHE / f"history_{dt.date.today():%Y%m%d}.pkl"
    if f.exists() and not refresh:
        log("  using today's cached history (tick 'Refresh data' to re-download)")
        return pickle.loads(f.read_bytes())
    out, failed = {}, []
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(client.daily, row.security_id, days): sym for sym, row in uni.iterrows()}
        for i, fut in enumerate(as_completed(futs), 1):
            sym = futs[fut]
            try:
                d = fut.result()
                if len(d) >= 130:
                    out[sym] = d
            except Exception as e:                      # keep going; report at the end
                failed.append(f"{sym}: {e}")
            if i % 50 == 0:
                log(f"  fetched {i}/{len(futs)}")
    if failed:
        log(f"  {len(failed)} failed, e.g. {failed[:3]}")
        if any("401" in x for x in failed):
            raise RuntimeError("Dhan returned 401: the access token is invalid or expired. "
                               "Update DHAN_ACCESS_TOKEN in .env (or in the UI sidebar).")
    f.write_bytes(pickle.dumps(out))
    return out


def live_overlay(client, uni: pd.DataFrame, metrics: pd.DataFrame) -> pd.DataFrame:
    """Add gap %, today's change and time-adjusted relative volume from a live quote snapshot."""
    q = client.quotes(uni.loc[metrics.index, "security_id"].tolist())
    if q.empty:
        return metrics
    q = q.join(uni.reset_index().set_index("security_id")["symbol"]).set_index("symbol")
    m = metrics.join(q, how="left")
    now = dt.datetime.now(dt.timezone(dt.timedelta(hours=5, minutes=30)))
    elapsed = (now.hour * 60 + now.minute) - (9 * 60 + 15)
    frac = min(max(elapsed / 375, 0.05), 1.0)           # share of the 375-min session done
    m["gap_pct"] = (m["day_open"] / m["prev_close"] - 1) * 100
    m["chg_today"] = (m["ltp"] / m["prev_close"] - 1) * 100
    m["rvol"] = (m["day_volume"] / (m["avg_vol20"] * frac)).fillna(m["rvol"])  # crude: volume isn't linear intraday
    m["close"] = m["ltp"].fillna(m["close"])
    return m

# ════════════════════════════ 3. Metrics ══════════════════════════════════════

def _rsi(c: pd.Series, n: int = 14) -> float:
    d = c.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return float((100 - 100 / (1 + up / dn.replace(0, np.nan))).iloc[-1])


def compute_metrics(prices: dict[str, pd.DataFrame]) -> pd.DataFrame:
    today = pd.Timestamp(dt.date.today())
    rows = []
    for sym, d in prices.items():
        d = d[d.index < today] if len(d) and d.index[-1] >= today else d   # completed days only
        c, h, l, v = d["close"], d["high"], d["low"], d["volume"]
        last = float(c.iloc[-1])
        tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
        ret = lambda n: (last / c.iloc[-n - 1] - 1) * 100 if len(c) > n else np.nan
        dma50 = c.rolling(50).mean().iloc[-1]
        dma200 = c.rolling(200).mean().iloc[-1] if len(c) >= 200 else np.nan
        rng = h.iloc[-1] - l.iloc[-1]
        rows.append({
            "symbol": sym, "close": last,
            "ret_1m": ret(21), "ret_3m": ret(63), "ret_6m": ret(126),
            "above_50": bool(last > dma50),
            "above_200": bool(last > dma200) if not np.isnan(dma200) else False,
            "golden": bool(dma50 > dma200) if not np.isnan(dma200) else False,
            "rsi": _rsi(c),
            "atr": float(tr.rolling(14).mean().iloc[-1]),
            "turnover_cr": float((c * v).iloc[-20:].mean() / 1e7),
            "avg_vol20": float(v.iloc[-21:-1].mean()),
            "rvol": float(v.iloc[-1] / v.iloc[-21:-1].mean()),
            "off_high": (last / c.iloc[-252:].max() - 1) * 100,
            "clv": float((c.iloc[-1] - l.iloc[-1]) / rng) if rng > 0 else 0.5,
            "nr7": bool((h - l).iloc[-1] <= (h - l).iloc[-7:].min()),
        })
    m = pd.DataFrame(rows).set_index("symbol")
    m["atr_pct"] = m["atr"] / m["close"] * 100
    return m

# ════════════════════════════ 4. Scoring ══════════════════════════════════════

# Each factor: how to compute a "higher is better" value for percentile ranking.
FACTORS = {
    "ret_6m":    lambda m: m["ret_6m"],
    "ret_3m":    lambda m: m["ret_3m"],
    "ret_1m":    lambda m: m["ret_1m"],
    "near_high": lambda m: m["off_high"],
    "trend":     lambda m: m["above_50"].astype(int) + m["above_200"].astype(int) + m["golden"].astype(int),
    "liquidity": lambda m: m["turnover_cr"],
    "atr_fit":   lambda m: -(m["atr_pct"] - 2.75).abs(),   # sweet spot ≈ 1.5–4 %
    "rvol":      lambda m: m["rvol"],
    "close_strength": lambda m: m["clv"],
}

MODES = {
    "swing": {
        "weights": {"ret_6m": .20, "ret_3m": .20, "ret_1m": .10, "near_high": .20,
                    "trend": .15, "liquidity": .15},
        "min_turnover_cr": {"large": 0, "mid": 10, "small": 5},
        "stop_atr": 1.5,
    },
    "intraday": {
        "weights": {"liquidity": .25, "rvol": .25, "atr_fit": .20, "ret_1m": .10,
                    "trend": .10, "close_strength": .10},
        "min_turnover_cr": {"large": 50, "mid": 50, "small": 50},
        "stop_atr": 0.5,
    },
}
MIN_PRICE = 50

FACTOR_LABEL = {
    "ret_6m": "6-month return", "ret_3m": "3-month return", "ret_1m": "1-month return",
    "near_high": "Near 52W high", "trend": "Trend (DMA stack)", "liquidity": "Liquidity (turnover)",
    "atr_fit": "Daily range fit (ATR%)", "rvol": "Relative volume", "close_strength": "Close strength",
}

# ════════════════════════════ 4a. Configurations ══════════════════════════════
#
# A configuration is a plain dict (JSON-friendly). Anything left out falls back to the
# defaults of its mode, so a preset can be as small as {"mode": "swing", "cap": "large"}.

RUN_DEFAULTS = {
    "mode": "swing", "cap": "all", "top": 10, "capital": 100_000, "risk_pct": 1.0,
    "live": False, "demo": False, "refresh": False, "index_dir": None, "amfi": None,
    "min_price": MIN_PRICE, "exclude_asm_gsm": True,
}


def resolve_config(cfg: dict | None = None) -> dict:
    """Fill a (partial) configuration with defaults and normalise the weights to sum to 1."""
    cfg = copy.deepcopy(cfg or {})
    out = {**RUN_DEFAULTS, **{k: v for k, v in cfg.items() if v is not None and v != ""}}
    if out["mode"] not in MODES:
        raise ValueError(f"unknown mode {out['mode']!r}; choose from {list(MODES)}")
    base = MODES[out["mode"]]
    weights = cfg.get("weights") or base["weights"]
    weights = {f: float(w) for f, w in weights.items() if f in FACTORS and float(w) > 0}
    if not weights:
        raise ValueError("at least one factor needs a weight above zero")
    total = sum(weights.values())
    out["weights"] = {f: w / total for f, w in weights.items()}
    out["min_turnover_cr"] = {**base["min_turnover_cr"], **(cfg.get("min_turnover_cr") or {})}
    out["stop_atr"] = float(cfg.get("stop_atr") or base["stop_atr"])
    return out


def load_configs(path: Path | str = CONFIGS_PATH) -> dict[str, dict]:
    p = Path(path)
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def save_configs(configs: dict[str, dict], path: Path | str = CONFIGS_PATH) -> None:
    Path(path).write_text(json.dumps(configs, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def score(m: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    keep = (m["close"] >= cfg["min_price"]) & (m["turnover_cr"] >= m["cap"].map(cfg["min_turnover_cr"]))
    if cfg["exclude_asm_gsm"]:
        keep &= ~m["asm_gsm"]
    m = m[keep].copy()
    for f in cfg["weights"]:
        m[f"f_{f}"] = FACTORS[f](m)
        m[f"p_{f}"] = m.groupby("cap")[f"f_{f}"].rank(pct=True)       # percentile within bucket
        m[f"c_{f}"] = m[f"p_{f}"] * cfg["weights"][f] * 100             # points contributed
    m["score"] = m[[f"c_{f}" for f in cfg["weights"]]].sum(axis=1)
    return m.sort_values("score", ascending=False)

# ════════════════════════════ 5. Explanations ═════════════════════════════════

# A factor is only quoted as a reason if it is good in absolute terms too,
# not merely better than weak peers.
GOOD_ENOUGH = {
    "ret_6m": lambda r: r.ret_6m > 0,
    "ret_3m": lambda r: r.ret_3m > 0,
    "ret_1m": lambda r: r.ret_1m > 0,
    "near_high": lambda r: r.off_high >= -15,
    "trend": lambda r: r.above_50 and r.above_200,
    "liquidity": lambda r: r.turnover_cr >= 25,
    "atr_fit": lambda r: 1.5 <= r.atr_pct <= 4,
    "rvol": lambda r: r.rvol >= 1.2,
    "close_strength": lambda r: r.clv >= 0.7,
}


def _pct_phrase(p: float, cap: str) -> str:
    return f"top {max(1, round((1 - p) * 100))}% of {cap} caps"


def factor_reason(f: str, r: pd.Series) -> str:
    p, cap = r[f"p_{f}"], r["cap"]
    match f:
        case "ret_6m":    return f"6-month return {r.ret_6m:+.0f}% ({_pct_phrase(p, cap)})"
        case "ret_3m":    return f"3-month return {r.ret_3m:+.0f}% ({_pct_phrase(p, cap)})"
        case "ret_1m":    return f"1-month return {r.ret_1m:+.0f}% ({_pct_phrase(p, cap)})"
        case "near_high":
            return ("At or near its 52-week high" if r.off_high >= -3
                    else f"{abs(r.off_high):.0f}% below 52-week high, closer than most peers")
        case "trend":
            parts = [s for s, ok in (("above 50 DMA", r.above_50), ("above 200 DMA", r.above_200),
                                     ("50 DMA over 200 DMA", r.golden)) if ok]
            return "Uptrend: " + ", ".join(parts) if parts else "No trend support"
        case "liquidity": return f"Highly liquid: ₹{r.turnover_cr:,.0f} Cr traded per day"
        case "atr_fit":   return f"Daily range {r.atr_pct:.1f}%, inside the 1.5–4% band that suits day trading"
        case "rvol":      return f"Volume {r.rvol:.1f}× its 20-day average"
        case "close_strength":
            return f"Closed in the top {max(1, round((1 - r.clv) * 100))}% of yesterday's range"
    return f


def setups(r: pd.Series) -> list[str]:
    tags = []
    if r.off_high >= -2 and r.rvol >= 1.5:
        tags.append("52W-high breakout")
    elif r.off_high >= -5:
        tags.append("Near 52W high")
    if r.above_50 and r.above_200 and -8 <= r.ret_1m <= 0 and 40 <= r.rsi <= 55:
        tags.append("Pullback in uptrend")
    if r.rvol >= 2:
        tags.append("Volume surge")
    if r.nr7:
        tags.append("NR7 squeeze")
    if abs(r.get("gap_pct", 0) or 0) >= 1.5:
        tags.append("Gap up" if r.gap_pct > 0 else "Gap down")
    if not tags:
        tags.append("Trend follow" if r.above_50 and r.above_200 else "Watchlist")
    return tags


def watch_outs(r: pd.Series) -> list[str]:
    w = []
    if r.score < 60: w.append(f"Low overall score ({r.score:.0f}): weak bucket, low conviction")
    if r.rsi > 75:  w.append(f"RSI {r.rsi:.0f}: overbought, wait for a pullback")
    if r.rsi < 30:  w.append(f"RSI {r.rsi:.0f}: oversold, trend is weak")
    if r.ret_1m > 25: w.append(f"Up {r.ret_1m:.0f}% in a month: extended")
    if r.atr_pct > 5: w.append(f"Very volatile ({r.atr_pct:.1f}% daily range): reduce size")
    if r.turnover_cr < 25: w.append(f"Thin liquidity (₹{r.turnover_cr:.0f} Cr/day): expect slippage")
    if r.off_high < -30: w.append(f"{abs(r.off_high):.0f}% below 52W high: overhead supply")
    if abs(r.get("gap_pct", 0) or 0) >= 3: w.append(f"Gapped {r.gap_pct:+.1f}%: don't chase the open")
    return w


def risk_plan(r: pd.Series, cfg: dict) -> str:
    capital, risk_pct = cfg["capital"], cfg["risk_pct"]
    stop = cfg["stop_atr"] * r.atr
    qty = math.floor(capital * risk_pct / 100 / stop) if stop > 0 else 0
    qty = min(qty, math.floor(capital / r.close))          # no leverage
    return f"Stop ₹{stop:,.1f} away ({cfg['stop_atr']:g}× ATR), qty {qty} ≈ ₹{qty * r.close:,.0f}"


def explain(scored: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    weights = cfg["weights"]
    rows = []
    for sym, r in scored.iterrows():
        ranked = sorted(weights, key=lambda f: r[f"c_{f}"], reverse=True)
        strong = [f for f in ranked if r[f"p_{f}"] >= 0.6 and GOOD_ENOUGH[f](r)][:3]
        why = [factor_reason(f, r) for f in strong] or ["No standout factor; ranked on overall balance vs peers"]
        rows.append({
            "symbol": sym, "name": r["name"], "industry": r["industry"], "cap": r["cap"],
            "score": round(r.score, 1), "price": round(r.close, 2),
            "setup": setups(r),
            "why": why,
            "watch": watch_outs(r),
            "plan": risk_plan(r, cfg),
            "breakdown": {f: round(r[f"c_{f}"], 1) for f in weights},
        })
    return pd.DataFrame(rows).set_index("symbol")

# ════════════════════════════ 6. Output ═══════════════════════════════════════

def pick_top(expl: pd.DataFrame, cap: str, top: int) -> dict[str, pd.DataFrame]:
    buckets = CAPS if cap == "all" else [cap]
    return {c: expl[expl["cap"] == c].head(top) for c in buckets}


def print_console(picks: dict[str, pd.DataFrame], mode: str) -> None:
    for cap, df in picks.items():
        print(f"\n{CAP_LABEL[cap]} — top {len(df)} ({mode})")
        print("-" * 100)
        for i, (sym, r) in enumerate(df.iterrows(), 1):
            print(f"{i:>2}. {sym:<12} score {r.score:>5}  ₹{r.price:<10,.2f} [{', '.join(r.setup)}]")
            for reason in r.why:
                print(f"      + {reason}")
            for w in r.watch:
                print(f"      ! {w}")
            print(f"      > {r.plan}")


def to_markdown(picks: dict[str, pd.DataFrame]) -> str:
    out = []
    for cap, df in picks.items():
        out.append(f"\n### {CAP_LABEL[cap]}\n")
        out.append("| # | Stock | Score | Setup | Why selected | Watch out | Risk plan |")
        out.append("|---|---|---|---|---|---|---|")
        for i, (sym, r) in enumerate(df.iterrows(), 1):
            out.append(f"| {i} | **{sym}** ({r.industry}) | {r.score} | {', '.join(r.setup)} | "
                       f"{'<br>'.join(r.why)} | {'<br>'.join(r.watch) or '—'} | {r.plan} |")
    return "\n".join(out)


def to_html(picks: dict[str, pd.DataFrame], cfg: dict, stats: dict) -> str:
    e = html.escape
    mode, demo = cfg["mode"], cfg["demo"]
    title = e(stats.get("config_name") or ("Intraday" if mode == "intraday" else "Swing") + " screener")
    sections, nav = [], []
    for cap, df in picks.items():
        nav.append(f'<a href="#{cap}" class="nav-{cap}">{CAP_LABEL[cap]} <span>{len(df)}</span></a>')
        body = []
        for i, (sym, r) in enumerate(df.iterrows(), 1):
            tags = "".join(f'<span class="tag">{e(t)}</span>' for t in r.setup)
            why = "".join(f"<li>{e(x)}</li>" for x in r.why)
            watch = "".join(f"<li>{e(x)}</li>" for x in r.watch) or '<li class="none">Nothing flagged</li>'
            bd = ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in r.breakdown.items())
            body.append(f"""
<tr>
  <td class="rank">{i}</td>
  <td class="stock"><strong>{e(sym)}</strong><small>{e(str(r['name']))}</small><small>{e(str(r.industry))}</small></td>
  <td class="score" title="Points by factor: {e(bd)}"><b>{r.score:.0f}</b><i style="--w:{r.score:.0f}%"></i></td>
  <td class="num">₹{r.price:,.2f}</td>
  <td>{tags}</td>
  <td><ul class="why">{why}</ul></td>
  <td><ul class="watch">{watch}</ul></td>
  <td class="plan">{e(r.plan)}</td>
</tr>""")
        empty = '<tr><td colspan="8" class="empty">No stock in this bucket passed the filters. Lower the turnover filter or switch mode.</td></tr>'
        sections.append(f"""
<section id="{cap}" class="bucket bucket-{cap}">
  <h2>{CAP_LABEL[cap]}</h2>
  <div class="scroll"><table>
    <thead><tr><th>#</th><th>Stock</th><th>Score</th><th>Price</th><th>Setup</th>
      <th>Why selected</th><th>Watch out</th><th>Risk plan</th></tr></thead>
    <tbody>{''.join(body) or empty}</tbody>
  </table></div>
</section>""")

    demo_note = '<p class="demo">Demo run on synthetic data. Symbols are not real stocks.</p>' if demo else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Screener, {stats['date']}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
:root {{
  --bg:#F2F4F3; --paper:#FFFFFF; --ink:#18262B; --muted:#5A696E; --rule:#D6DDDB;
  --good:#1D7A55; --warn:#A85A08; --large:#2F4B8F; --mid:#15767A; --small:#8A3D6C;
}}
@media (prefers-color-scheme: dark) {{
  :root {{ --bg:#121A1D; --paper:#1A2428; --ink:#E4ECEA; --muted:#98A8AC; --rule:#2C3A3F;
          --good:#5CC795; --warn:#E6A15A; --large:#8FA8EC; --mid:#62C7C9; --small:#DB8FBE; }}
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--ink);
  font:15px/1.5 "IBM Plex Sans", system-ui, -apple-system, "Segoe UI", sans-serif;
  font-variant-numeric: tabular-nums; }}
header, main, footer {{ max-width:1320px; margin:0 auto; padding:0 20px; }}
header {{ padding-top:36px; }}
h1 {{ font-size:30px; line-height:1.15; font-weight:700; margin:0 0 6px; letter-spacing:-.01em; }}
.sub {{ color:var(--muted); margin:0 0 20px; max-width:72ch; }}
.demo {{ color:var(--warn); font-weight:500; margin:0 0 16px; }}
nav {{ display:flex; gap:10px; flex-wrap:wrap; margin-bottom:28px; }}
nav a {{ text-decoration:none; color:var(--ink); background:var(--paper); border:1px solid var(--rule);
  border-left:4px solid var(--c); padding:6px 12px; border-radius:6px; font-weight:500; }}
nav a span {{ color:var(--muted); font-weight:400; margin-left:4px; }}
nav a:focus-visible, a:focus-visible {{ outline:2px solid var(--ink); outline-offset:2px; }}
.nav-large, .bucket-large {{ --c:var(--large); }}
.nav-mid, .bucket-mid {{ --c:var(--mid); }}
.nav-small, .bucket-small {{ --c:var(--small); }}
.bucket {{ margin-bottom:40px; }}
.bucket h2 {{ font-size:21px; margin:0 0 10px; padding-left:12px; border-left:5px solid var(--c); }}
.scroll {{ overflow-x:auto; background:var(--paper); border:1px solid var(--rule); border-radius:8px; }}
table {{ border-collapse:collapse; width:100%; min-width:1080px; }}
th {{ text-align:left; font-size:13px; font-weight:600; color:var(--muted); padding:10px 12px;
  border-bottom:1px solid var(--rule); white-space:nowrap; }}
td {{ padding:12px; border-bottom:1px solid var(--rule); vertical-align:top; }}
tbody tr:last-child td {{ border-bottom:0; }}
.rank {{ color:var(--muted); width:28px; }}
.stock strong {{ display:block; font-size:15px; }}
.stock small {{ display:block; color:var(--muted); font-size:12.5px; line-height:1.35; }}
.score {{ width:92px; }}
.score b {{ font-size:20px; font-weight:700; color:var(--c); }}
.score i {{ display:block; height:4px; background:var(--rule); border-radius:2px; margin-top:4px; position:relative; }}
.score i::after {{ content:""; position:absolute; inset:0 auto 0 0; width:var(--w); background:var(--c); border-radius:2px; }}
.num {{ white-space:nowrap; }}
.tag {{ display:inline-block; font-size:12.5px; padding:2px 8px; margin:0 4px 4px 0; border-radius:999px;
  border:1px solid var(--c); color:var(--c); white-space:nowrap; }}
ul {{ margin:0; padding:0; list-style:none; }}
ul li {{ position:relative; padding-left:16px; margin-bottom:3px; font-size:13.5px; }}
.why li::before {{ content:"+"; position:absolute; left:0; color:var(--good); font-weight:700; }}
.watch li::before {{ content:"!"; position:absolute; left:3px; color:var(--warn); font-weight:700; }}
.watch li.none {{ color:var(--muted); }}
.watch li.none::before {{ content:""; }}
.plan {{ font-size:13px; color:var(--muted); max-width:220px; }}
.empty {{ color:var(--muted); padding:20px; }}
footer {{ color:var(--muted); font-size:13px; padding-bottom:40px; max-width:900px; margin-left:max(20px, calc((100vw - 1320px)/2)); }}
</style></head>
<body>
<header>
  <h1>{title}, {stats['date']}</h1>
  <p class="sub">Scanned {stats['scanned']} {'demo' if demo else 'NSE'} stocks, {stats['passed']} passed the filters.
  Stocks are ranked against others in the same cap bucket. Hover a score to see the points each factor added.</p>
  {demo_note}
  <nav>{''.join(nav)}</nav>
</header>
<main>{''.join(sections)}</main>
<footer>
  <p>Method ({mode} mode): percentile rank within bucket for each factor, weighted ({', '.join(f'{k.replace("_", " ")} {round(v * 100)}%' for k, v in cfg['weights'].items())}).
  Filters: price ≥ ₹{cfg['min_price']:g}{', not under ASM/GSM' if cfg['exclude_asm_gsm'] else ''}, minimum daily turnover
  ({', '.join(f'{c} ₹{v:g} Cr' for c, v in cfg['min_turnover_cr'].items())}). Risk plan assumes
  ₹{cfg['capital']:,.0f} capital, {cfg['risk_pct']:g}% risk per trade, stop {cfg['stop_atr']:g}× ATR and no leverage.</p>
  <p>This is a screening tool, not investment advice. A stock appearing here is a candidate to study, not a signal to buy.</p>
</footer>
</body></html>"""

# ════════════════════════════ 7. Demo data ════════════════════════════════════

def demo_data(n_per_cap: int = 25, seed: int = 7):
    rng = np.random.default_rng(seed)
    inds = ["Banks", "IT", "Pharma", "Auto", "FMCG", "Capital Goods", "Chemicals", "Metals"]
    uni_rows, prices = [], {}
    idx = pd.bdate_range(end=dt.date.today() - dt.timedelta(days=1), periods=280)
    for ci, cap in enumerate(CAPS):
        for j in range(n_per_cap):
            sym = f"DEMO{cap[0].upper()}{j + 1:02d}"
            uni_rows.append({"symbol": sym, "name": f"Demo {cap.title()} Co {j + 1}", "industry": rng.choice(inds),
                             "isin": None, "cap": cap, "security_id": sym, "asm_gsm": bool(rng.random() < .04)})
            vol = rng.uniform(0.010, 0.022 + 0.008 * ci)
            c = rng.uniform(80, 3000) * np.exp(np.cumsum(rng.normal(rng.normal(4e-4, 1e-3), vol, len(idx))))
            h = c * (1 + rng.uniform(0, vol, len(idx)))
            l = c * (1 - rng.uniform(0, vol, len(idx)))
            v = rng.lognormal(14.5 - 1.3 * ci, .5, len(idx)) * (1 + (rng.random(len(idx)) < .05) * 2)
            prices[sym] = pd.DataFrame({"open": c, "high": h, "low": l, "close": c, "volume": v}, index=idx)
    return pd.DataFrame(uni_rows).set_index("symbol"), prices

# ════════════════════════════ 8. Run (shared by CLI and UI) ══════════════════

def _nse_equities(DhanClient, log: Callable) -> pd.DataFrame:
    """Dhan instrument master (NSE EQ slice), cached for the day — the full file is large."""
    f = CACHE / f"equities_{dt.date.today():%Y%m%d}.pkl"
    if f.exists():
        return pd.read_pickle(f)
    log("  downloading Dhan instrument master…")
    eq = DhanClient.nse_equities()
    eq.to_pickle(f)
    return eq


def run_screener(config: dict | None = None, log: Callable = print, name: str | None = None) -> dict:
    """Run one configuration end to end. Returns the resolved config, the tables and the report HTML."""
    cfg = resolve_config(config)
    t0 = time.time()
    if cfg["demo"]:
        log("Demo mode: generating synthetic prices (no Dhan calls)…")
        uni, prices = demo_data()
        client = None
    else:
        from dhan_client import DhanClient
        client = DhanClient()
        log("Loading universe and Dhan instrument master…")
        uni = attach_security_ids(load_universe(cfg["index_dir"], cfg["amfi"]), _nse_equities(DhanClient, log), log)
        log(f"Fetching daily history for {len(uni)} stocks…")
        prices = fetch_history(client, uni, log=log, refresh=cfg["refresh"])
        if not prices:
            raise RuntimeError("No price history came back from Dhan. Check the token and your network.")

    metrics = compute_metrics(prices).join(uni[["name", "industry", "cap", "asm_gsm"]])
    if cfg["live"] and client:
        log("Overlaying live quotes…")
        metrics = live_overlay(client, uni, metrics)
    scored = score(metrics, cfg)
    expl = explain(scored, cfg)
    picks = pick_top(expl, cfg["cap"], int(cfg["top"]))
    stats = {"date": dt.date.today().strftime("%d %b %Y"), "scanned": len(metrics), "passed": len(scored),
             "config_name": name}
    flat = expl.assign(**{k: expl[k].map(" | ".join) for k in ("setup", "why", "watch")})
    table = flat.drop(columns=["breakdown"]).join(
        scored.filter(regex="^(ret_|rsi|atr_pct|turnover_cr|rvol|off_high|gap_pct|chg_today)"))
    log(f"Done in {time.time() - t0:.1f}s: {stats['passed']} of {stats['scanned']} stocks passed the filters.")
    return {"config": cfg, "stats": stats, "picks": picks, "table": table,
            "html": to_html(picks, cfg, stats)}

# ════════════════════════════ 9. CLI ══════════════════════════════════════════

def main() -> None:
    ap = argparse.ArgumentParser(description="Dhan-powered NSE screener with explanations")
    ap.add_argument("--config", help="name of a saved configuration in configs.json; other flags override it")
    ap.add_argument("--list-configs", action="store_true", help="list saved configurations and exit")
    ap.add_argument("--mode", choices=list(MODES))
    ap.add_argument("--cap", choices=["all", *CAPS])
    ap.add_argument("--top", type=int)
    ap.add_argument("--capital", type=float)
    ap.add_argument("--risk-pct", type=float)
    ap.add_argument("--live", action="store_true", default=None, help="overlay live gap/volume from Dhan quotes (market hours)")
    ap.add_argument("--demo", action="store_true", default=None, help="synthetic data, no credentials needed")
    ap.add_argument("--refresh", action="store_true", default=None, help="ignore today's cached history")
    ap.add_argument("--index-dir", help="folder with the three NSE index CSVs, if download is blocked")
    ap.add_argument("--amfi", help="AMFI categorisation CSV (symbol,category) to override buckets")
    ap.add_argument("--html", default="screener_report.html")
    ap.add_argument("--csv", default="screener_scores.csv")
    ap.add_argument("--md", action="store_true", help="also print Markdown tables")
    a = ap.parse_args()

    configs = load_configs()
    if a.list_configs:
        for n, c in configs.items():
            print(f"{n:<32} {c.get('description', '')}")
        return
    base = {}
    if a.config:
        if a.config not in configs:
            ap.error(f"no configuration named {a.config!r}. Saved: {', '.join(configs) or 'none'}")
        base = configs[a.config]
    overrides = {k: getattr(a, k) for k in ("mode", "cap", "top", "capital", "risk_pct", "live",
                                            "demo", "refresh", "index_dir", "amfi")}
    if overrides["mode"] and base.get("mode") and overrides["mode"] != base["mode"]:
        base = {k: v for k, v in base.items() if k not in ("weights", "min_turnover_cr", "stop_atr")}
    cfg = {**base, **{k: v for k, v in overrides.items() if v is not None}}

    res = run_screener(cfg, name=a.config)
    print_console(res["picks"], res["config"]["mode"])
    if a.md:
        print(to_markdown(res["picks"]))
    Path(a.html).write_text(res["html"], encoding="utf-8")
    res["table"].to_csv(a.csv)
    print(f"\nReport: {a.html}   Scores: {a.csv}")


if __name__ == "__main__":
    main()
