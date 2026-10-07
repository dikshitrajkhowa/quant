"""
FastAPI backend for the NSE multi-strategy screener.

Strategy logic and data download are shared with the Streamlit app in
../streamlit_scanner (strategies.py, near_52w_high.py) - add a strategy there
and it appears in both UIs.

Run:  python main.py        (http://localhost:8000)
"""

from __future__ import annotations

import logging
import re
import sys
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

HERE = Path(__file__).resolve().parent
SHARED = HERE.parent / "streamlit_scanner"
sys.path.insert(0, str(SHARED))

import near_52w_high as core  # noqa: E402
import strategies as strat  # noqa: E402
import data_providers as dp  # noqa: E402

import backtest_engine as bte  # noqa: E402  (lives next to this file)
import megabull as mb  # noqa: E402

mb.ensure_env_file()
mb.load_env()                # before trading_engine, which reads TRADE_* settings at import
import trading_engine as te  # noqa: E402

log = logging.getLogger("screener.api")

UNIVERSES = {
    "all": "All NSE equities (EQ series)",
    "nifty50": "Nifty 50",
    "nifty100": "Nifty 100",
    "nifty500": "Nifty 500",
}
HISTORY_PERIOD = "2y"          # enough for 200-DMA strategies; 52W metrics use the last 252 sessions
DATA_TTL_SECONDS = 3600
PRICE_COLS = {"Current Price", "52W High", "52W Low", "Prior 52W High"}
BASE_COLS = ["Current Price", "% From 52W High", "52W High", "52W Low", "1M Return %", "Avg Vol (20D)"]
HIDDEN_COLS = {"Ticker", "Passed", "52W High Date", "% Above 52W Low", "Last Date"}


# --------------------------------------------------------------------------- #
# In-memory data store (single-user local app)
# --------------------------------------------------------------------------- #
SourceField = Field("yahoo", pattern="^(yahoo|tradingview)$")


class Dataset:
    def __init__(self, universe: str, source: str = "yahoo"):
        self.universe = universe
        self.source = source
        self.state = "loading"          # loading | ready | error
        self.done = 0
        self.total = 0
        self.message = "Fetching NSE stock list…"
        self.history: dict[str, pd.DataFrame] = {}
        self.n_tickers = 0
        self.loaded_at = 0.0
        self.token = ""

    def status(self) -> dict:
        last = max((df.index[-1] for df in self.history.values()), default=None)
        return {
            "universe": self.universe, "source": self.source, "source_name": dp.PROVIDERS[self.source]["name"],
            "state": self.state, "done": self.done, "total": self.total,
            "message": self.message, "n_tickers": self.n_tickers, "n_loaded": len(self.history),
            "data_as_of": last.strftime("%Y-%m-%d") if last is not None else None,
            "loaded_at": self.loaded_at,
        }


class Store:
    def __init__(self):
        self.lock = threading.Lock()
        self.datasets: dict[str, Dataset] = {}
        self.eval_cache: OrderedDict[tuple, pd.DataFrame] = OrderedDict()
        self.extra: dict[tuple[str, str], tuple[float, pd.DataFrame]] = {}   # (source, ticker) fetched on demand

    def get(self, universe: str, source: str = "yahoo") -> Dataset | None:
        return self.datasets.get(f"{source}:{universe}")

    def start_load(self, universe: str, limit: int, batch_size: int, force: bool, source: str = "yahoo") -> Dataset:
        with self.lock:
            ds = self.datasets.get(f"{source}:{universe}")
            fresh = ds and ds.state == "ready" and time.time() - ds.loaded_at < DATA_TTL_SECONDS
            if ds and (ds.state == "loading" or (fresh and not force)):
                return ds
            ds = Dataset(universe, source)
            self.datasets[f"{source}:{universe}"] = ds
        threading.Thread(target=self._load, args=(ds, limit, batch_size), daemon=True).start()
        return ds

    def _load(self, ds: Dataset, limit: int, batch_size: int) -> None:
        try:
            tickers = core.get_nse_tickers(ds.universe)
            if limit:
                tickers = tickers[:limit]
            ds.n_tickers = len(tickers)
            src = dp.PROVIDERS[ds.source]["name"]
            ds.message = f"Downloading price history from {src}…"

            def cb(done, total):
                ds.done, ds.total = done, total
                ds.message = f"Downloading price history from {src}… batch {done}/{total}"

            hist = dp.download_daily(tickers, period=HISTORY_PERIOD, source=ds.source,
                                     batch_size=batch_size, progress_callback=cb)
            if not hist:
                raise RuntimeError(f"No price data downloaded - check internet / {src} access.")
            ds.history = hist
            ds.loaded_at = time.time()
            ds.token = f"{ds.source}-{ds.universe}-{ds.n_tickers}-{ds.loaded_at:.0f}"
            ds.state, ds.message = "ready", f"Loaded {len(hist):,} of {ds.n_tickers:,} stocks"
        except Exception as exc:  # surfaced to the UI via /api/data/status
            log.exception("Data load failed")
            ds.state, ds.message = "error", str(exc)

    def evaluate(self, ds: Dataset, s: strat.Strategy, params: dict, min_hist: int) -> pd.DataFrame:
        key = (ds.token, s.key, tuple(sorted(params.items())), min_hist)
        with self.lock:
            if key in self.eval_cache:
                self.eval_cache.move_to_end(key)
                return self.eval_cache[key]
        df = strat.run_strategy(ds.history, s, params, min_hist)
        with self.lock:
            self.eval_cache[key] = df
            while len(self.eval_cache) > 40:
                self.eval_cache.popitem(last=False)
        return df


    def history_for(self, tickers: list[str], source: str = "yahoo") -> dict[str, pd.DataFrame]:
        """Daily history for specific tickers from `source`: reuse any loaded universe, download the rest."""
        found: dict[str, pd.DataFrame] = {}
        for ds in list(self.datasets.values()):
            if ds.state == "ready" and ds.source == source:
                for t in tickers:
                    if t not in found and t in ds.history:
                        found[t] = ds.history[t]
        now = time.time()
        for t in tickers:
            hit = self.extra.get((source, t))
            if t not in found and hit and now - hit[0] < DATA_TTL_SECONDS:
                found[t] = hit[1]
        missing = [t for t in tickers if t not in found]
        if missing:
            fetched = dp.download_daily(missing, period=HISTORY_PERIOD, source=source, batch_size=50)
            for t, df in fetched.items():
                self.extra[(source, t)] = (now, df)
                found[t] = df
        return found


STORE = Store()
TICKER_RE = re.compile(r"^[A-Z0-9&_\-]+\.(NS|BO)$")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def clean(v: Any) -> Any:
    """Make numpy / pandas values JSON-safe (NaN -> None)."""
    if isinstance(v, (np.floating, float)):
        return None if not np.isfinite(v) else round(float(v), 4)
    if isinstance(v, np.integer):
        return int(v)
    if isinstance(v, np.bool_):
        return bool(v)
    if isinstance(v, (pd.Timestamp,)):
        return v.strftime("%Y-%m-%d")
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return v


def series_list(s: pd.Series) -> list:
    return [clean(x) for x in s.to_numpy()]


def dates_list(idx: pd.Index) -> list[str]:
    return [d.strftime("%Y-%m-%d") for d in idx]


def strategy_meta(s: strat.Strategy) -> dict:
    return {
        "key": s.key, "name": s.name, "description": s.description,
        "score_col": s.score_col, "ascending": s.ascending, "score_fmt": s.score_fmt,
        "overlays": s.overlays, "panel": s.panel, "has_levels": s.levels is not None,
        "params": [p.__dict__ for p in s.params],
    }


def resolve_params(s: strat.Strategy, given: dict) -> dict:
    out = {}
    for p in s.params:
        v = given.get(p.key, p.default)
        if p.kind == "choice":
            allowed = [o[0] if isinstance(o, (list, tuple)) else o for o in (p.options or [])]
            if v not in allowed:
                raise HTTPException(422, f"Invalid value for {p.key}: {v!r} (choose from {allowed})")
            out[p.key] = v
            continue
        try:
            v = bool(v) if p.kind == "bool" else int(v) if p.kind == "int" else float(v)
        except (TypeError, ValueError):
            raise HTTPException(422, f"Invalid value for {p.key}: {v!r}")
        if p.kind != "bool":
            if p.min is not None:
                v = max(v, type(v)(p.min))
            if p.max is not None:
                v = min(v, type(v)(p.max))
        out[p.key] = v
    return out


def get_strategy(key: str) -> strat.Strategy:
    if key not in strat.STRATEGIES:
        raise HTTPException(404, f"Unknown strategy '{key}'")
    return strat.STRATEGIES[key]


def ready_dataset(universe: str, source: str = "yahoo") -> Dataset:
    ds = STORE.get(universe, source)
    if not ds or ds.state != "ready":
        raise HTTPException(409, "Data for this universe is not loaded yet - call /api/data/load first.")
    return ds


def overlays_for(full: pd.DataFrame, s: strat.Strategy, days: int) -> dict[str, list]:
    out = {}
    for ov in s.overlays:
        for name, ser in strat.overlay_series(full, ov).items():
            out[name] = series_list(ser.tail(days))
    return out


def levels_for(row: pd.Series, s: strat.Strategy, params: dict) -> list[dict]:
    if not s.levels:
        return []
    return [{k: clean(v) for k, v in lv.items()} for lv in s.levels(row, params)]


# --------------------------------------------------------------------------- #
# API models
# --------------------------------------------------------------------------- #
class LoadRequest(BaseModel):
    universe: str = "all"
    source: str = SourceField
    limit: int = Field(0, ge=0)
    batch_size: int = Field(100, ge=10, le=500)
    force: bool = False


class ScreenRequest(BaseModel):
    universe: str = "all"
    source: str = SourceField
    strategy: str = "near_52w_high"
    params: dict[str, Any] = {}
    min_price: float = 0.0
    min_avg_volume: float = 0.0
    min_history: int = Field(200, ge=20, le=500)
    top_n: int = Field(20, ge=1, le=100)
    chart_days: int = Field(120, ge=20, le=500)


class StockRequest(ScreenRequest):
    ticker: str


class BacktestRequest(BaseModel):
    strategy: str = "breakout_15m"
    tickers: list[str] = Field(..., min_length=1, max_length=50)
    params: dict[str, Any] = {}
    source: str = SourceField


class CandlesRequest(BaseModel):
    strategy: str = "breakout_15m"
    ticker: str
    days: int = Field(59, ge=1, le=7300)
    source: str = SourceField
    interval: str | None = None      # candle timeframe; defaults to the strategy's


class QuotesRequest(BaseModel):
    tickers: list[str] = Field(..., max_length=100)
    source: str = SourceField


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #
APP_VERSION = "2026.10.08"
app = FastAPI(title="NSE Strategy Screener", version=APP_VERSION)


@app.middleware("http")
async def no_stale_frontend(request, call_next):
    """Make browsers re-check pages/JS/CSS on every load so code updates show up immediately."""
    resp = await call_next(request)
    if not request.url.path.startswith("/api"):
        resp.headers["Cache-Control"] = "no-cache, must-revalidate"
    return resp


@app.get("/api/meta")
def meta():
    return {"version": APP_VERSION, "universes": UNIVERSES, "providers": dp.available(),
            "strategies": [strategy_meta(s) for s in strat.STRATEGIES.values()]}


def require_source(source: str) -> None:
    try:
        dp.check(source)
    except Exception as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/data/load")
def load_data(req: LoadRequest):
    if req.universe not in UNIVERSES:
        raise HTTPException(404, f"Unknown universe '{req.universe}'")
    require_source(req.source)
    return STORE.start_load(req.universe, req.limit, req.batch_size, req.force, req.source).status()


@app.get("/api/data/status")
def data_status(universe: str = "all", source: str = "yahoo"):
    ds = STORE.get(universe, source)
    return ds.status() if ds else {"universe": universe, "source": source, "state": "idle"}


@app.post("/api/screen")
def screen(req: ScreenRequest):
    s = get_strategy(req.strategy)
    ds = ready_dataset(req.universe, req.source)
    params = resolve_params(s, req.params)
    all_df = STORE.evaluate(ds, s, params, req.min_history)
    result = strat.rank_matches(all_df, s, min_price=req.min_price, min_avg_volume=req.min_avg_volume)

    new_highs = int((all_df["52W High Date"] == all_df["Last Date"]).sum()) if not all_df.empty else 0
    kpis = {
        "evaluated": len(all_df), "matches": len(result), "n_tickers": ds.n_tickers, "new_highs": new_highs,
        "data_as_of": ds.status()["data_as_of"], "source": ds.source, "source_name": dp.PROVIDERS[ds.source]["name"],
    }

    # column order: symbol, score, strategy-specific, then common columns
    strategy_cols = [c for c in result.columns
                     if c not in BASE_COLS and c not in HIDDEN_COLS and c not in ("Symbol", s.score_col)]
    columns = list(dict.fromkeys(["Symbol", s.score_col, *strategy_cols, *[c for c in BASE_COLS if c != s.score_col]]))

    rows = []
    for rank, r in result.iterrows():
        rec = {"Rank": int(rank), "Ticker": r["Ticker"], **{c: clean(r[c]) for c in columns}}
        rec["Trend"] = series_list(ds.history[r["Ticker"]]["Close"].tail(req.chart_days).round(2))
        rows.append(rec)

    charts = []
    for rank, r in result.head(req.top_n).iterrows():
        full = ds.history[r["Ticker"]]
        view = full.tail(req.chart_days)
        charts.append({
            "rank": int(rank), "ticker": r["Ticker"], "symbol": r["Symbol"], "score": clean(r[s.score_col]),
            "dates": dates_list(view.index), "close": series_list(view["Close"]),
            "overlays": overlays_for(full, s, req.chart_days), "levels": levels_for(r, s, params),
        })

    dist = {"score": [], "passed": [], "pct_from_high": []}
    if not all_df.empty:
        dist = {"score": series_list(all_df[s.score_col].astype(float)),
                "passed": [bool(x) for x in all_df["Passed"]],
                "pct_from_high": series_list(all_df["% From 52W High"])}

    return {"strategy": strategy_meta(s), "params": params, "kpis": kpis, "columns": columns,
            "price_cols": sorted(PRICE_COLS), "results": rows, "charts": charts, "distribution": dist}


@app.post("/api/stock")
def stock(req: StockRequest):
    s = get_strategy(req.strategy)
    ds = ready_dataset(req.universe, req.source)
    if req.ticker not in ds.history:
        raise HTTPException(404, f"No data for {req.ticker}")
    params = resolve_params(s, req.params)
    all_df = STORE.evaluate(ds, s, params, req.min_history)
    match = all_df[all_df["Ticker"] == req.ticker]
    full = ds.history[req.ticker]
    view = full.tail(req.chart_days)

    panel = {"type": s.panel}
    if s.panel == "rsi":
        panel["rsi"] = series_list(strat.rsi(full["Close"]).tail(req.chart_days))
    elif s.panel == "macd":
        m = strat.macd(full["Close"]).tail(req.chart_days)
        panel.update(macd=series_list(m["MACD"]), signal=series_list(m["Signal"]), hist=series_list(m["Hist"]))

    row = match.iloc[0] if not match.empty else None
    return {
        "ticker": req.ticker, "symbol": req.ticker.removesuffix(".NS"),
        "dates": dates_list(view.index),
        "ohlcv": {c: series_list(view[c]) for c in ("Open", "High", "Low", "Close", "Volume")},
        "overlays": overlays_for(full, s, req.chart_days),
        "levels": levels_for(row, s, params) if row is not None else [],
        "metrics": {k: clean(v) for k, v in row.items()} if row is not None else {},
        "panel": panel,
    }


@app.post("/api/quotes")
def quotes(req: QuotesRequest):
    """Summary + 1-year closes for a list of tickers (used by the Backtest page)."""
    tickers = []
    for raw in req.tickers:
        t = raw.strip().upper()
        if t and "." not in t:
            t += ".NS"
        if not TICKER_RE.match(t):
            raise HTTPException(422, f"Invalid ticker: {raw!r}")
        if t not in tickers:
            tickers.append(t)
    require_source(req.source)
    hist = STORE.history_for(tickers, req.source)
    out = []
    for t in tickers:
        df = hist.get(t)
        if df is None or df.empty:
            continue
        yr = df.tail(strat.YEAR)
        close = df["Close"]
        high = float(yr["High"].max())
        last = float(close.iloc[-1])
        out.append({
            "ticker": t, "symbol": t.rsplit(".", 1)[0],
            "last_price": clean(last), "last_date": df.index[-1].strftime("%Y-%m-%d"),
            "ret_1m": clean(strat.pct_return(close, 21)),
            "ret_1y": clean((last / float(yr["Close"].iloc[0]) - 1) * 100),
            "pct_from_high": clean((high - last) / high * 100) if high > 0 else None,
            "dates": dates_list(yr.index), "close": series_list(yr["Close"].round(2)),
        })
    return {"quotes": out, "missing": [t for t in tickers if t not in hist or hist[t].empty]}


def normalize_tickers(raw: list[str]) -> list[str]:
    out = []
    for r in raw:
        t = r.strip().upper()
        if t and "." not in t:
            t += ".NS"
        if not TICKER_RE.match(t):
            raise HTTPException(422, f"Invalid ticker: {r!r}")
        if t not in out:
            out.append(t)
    return out


def get_backtest(key: str) -> bte.BacktestStrategy:
    if key not in bte.BACKTESTS:
        raise HTTPException(404, f"Unknown backtest strategy '{key}'")
    return bte.BACKTESTS[key]


@app.get("/api/backtest/providers")
def backtest_providers():
    return dp.available()


@app.get("/api/backtest/strategies")
def backtest_strategies():
    return [{"key": s.key, "name": s.name, "description": s.description, "rules": s.rules,
             "interval": s.interval, "max_days": s.max_days, "params": [p.__dict__ for p in s.params]}
            for s in bte.BACKTESTS.values()]


@app.post("/api/backtest/run")
def backtest_run(req: BacktestRequest):
    s = get_backtest(req.strategy)
    tickers = normalize_tickers(req.tickers)
    params = resolve_params(s, req.params)
    require_source(req.source)
    try:
        return bte.run_backtest(s.key, tickers, params, source=req.source)
    except Exception as exc:
        log.exception("Backtest failed")
        raise HTTPException(502, f"Backtest failed: {exc}")


@app.post("/api/backtest/candles")
def backtest_candles(req: CandlesRequest):
    s = get_backtest(req.strategy)
    t = normalize_tickers([req.ticker])[0]
    require_source(req.source)
    tf = req.interval if req.interval in bte.dp.TIMEFRAMES else s.interval
    data = bte.fetch_candles([t], interval=tf, days=req.days, source=req.source)
    if t not in data:
        raise HTTPException(404, f"No {tf} data for {t}")
    return {"ticker": t, "interval": tf, "intraday": bte.dp.is_intraday(tf), **bte.candles_json(data[t])}


# --------------------------------------------------------------------------- #
# Paper trading (Megabull)
# --------------------------------------------------------------------------- #
class _Broker:
    """Holds the Megabull client; rebuilt when .env is reloaded (keys expire monthly)."""
    def __init__(self):
        self.client = mb.MegabullClient()

    def reload(self) -> None:
        mb.load_env(override=True)
        self.client = mb.MegabullClient()


BROKER = _Broker()
ENGINE = te.TradingEngine(client_factory=lambda: BROKER.client)


@app.on_event("startup")
def _start_engine():
    ENGINE.start()


@app.on_event("shutdown")
def _stop_engine():
    ENGINE.shutdown()


class TradeStartRequest(BaseModel):
    tickers: list[str] = Field(..., min_length=1, max_length=25)
    strategy: str = "breakout_15m"
    params: dict[str, Any] = {}
    source: str = SourceField
    dry_run: bool = False
    origin: str = ""


class TradeStopRequest(BaseModel):
    square_off: bool = True


class TestOrderRequest(BaseModel):
    ticker: str = "SBIN"
    round_trip: bool = True


def _broker_info(check: bool) -> dict:
    c = BROKER.client
    key = c.api_key or ""
    info = {"configured": c.configured, "key_hint": f"…{key[-4:]}" if len(key) >= 8 else ("set" if key else None),
            "env_file": str(mb.ENV_FILE), "base_url": c.base_url, "ok": None, "error": None, "profile": None}
    if c.configured and check:
        try:
            info["profile"] = {k: v for k, v in mb.summarize_profile(c.profile()).items() if k != "raw"}
            info["ok"] = True
        except mb.MegabullError as exc:
            info["ok"], info["error"] = False, str(exc)
    return info


@app.get("/api/trade/status")
def trade_status(check_broker: bool = False):
    return {"broker": _broker_info(check_broker), "engine": ENGINE.status()}


@app.get("/api/trade/broker")
def trade_broker():
    """Profile, orders and positions straight from Megabull."""
    c = BROKER.client
    if not c.configured:
        raise HTTPException(400, f"No API key - add MEGABULL_API_KEY to {mb.ENV_FILE}")
    out: dict[str, Any] = {}
    for name, fn in (("profile", lambda: mb.summarize_profile(c.profile())), ("orders", c.orders),
                     ("positions", c.positions)):
        try:
            out[name] = fn()
        except mb.MegabullError as exc:
            out[name] = None
            out.setdefault("errors", {})[name] = str(exc)
    return out


@app.get("/api/trade/mapping")
def trade_mapping(refresh: bool = False):
    c = BROKER.client
    m = c.mapping(refresh=refresh)
    return {**m.report(), "spec_cached": mb.SPEC_CACHE.exists(), "last_exchange": c.last_exchange}


@app.post("/api/trade/reload-env")
def trade_reload_env():
    BROKER.reload()
    return _broker_info(True)


@app.post("/api/trade/test-order")
def trade_test_order(req: TestOrderRequest):
    """BUY 1 share at market on Megabull (virtual money), then SELL it back - verifies the order mapping."""
    c = BROKER.client
    t = normalize_tickers([req.ticker])[0]
    steps = []
    try:
        buy = c.place_order(mb.Order(t, "BUY", 1))
        steps.append({"step": "BUY 1", **buy})
        if req.round_trip:
            time.sleep(1.0)
            sell = c.place_order(mb.Order(t, "SELL", 1))
            steps.append({"step": "SELL 1", **sell})
        return {"ok": True, "steps": steps}
    except mb.MegabullError as exc:
        return {"ok": False, "error": str(exc), "steps": steps, "last_exchange": c.last_exchange,
                "payload": _safe_payload(c, t)}


def _safe_payload(c, t):
    try:
        return c.build_payload(mb.Order(t, "BUY", 1))
    except Exception as exc:
        return {"error": str(exc)}


@app.post("/api/trade/sessions")
def trade_start(req: TradeStartRequest):
    get_backtest(req.strategy)
    require_source(req.source)
    if not req.dry_run and not BROKER.client.configured:
        raise HTTPException(400, f"No Megabull API key - add MEGABULL_API_KEY to {mb.ENV_FILE} "
                                 "(or start in dry-run mode)")
    tickers = normalize_tickers(req.tickers)
    params = resolve_params(get_backtest(req.strategy), req.params)
    try:
        created = ENGINE.create_sessions(tickers, req.strategy, params, req.source, params.get("capital", 100000),
                                         req.dry_run, req.origin)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"created": [s.public() for s in created], "engine": ENGINE.status()}


@app.post("/api/trade/sessions/{sid}/stop")
def trade_stop(sid: str, req: TradeStopRequest):
    if sid not in ENGINE.sessions:
        raise HTTPException(404, "No such session")
    return ENGINE.stop_session(sid, req.square_off).public()


@app.post("/api/trade/sessions/{sid}/exit")
def trade_exit(sid: str):
    if sid not in ENGINE.sessions:
        raise HTTPException(404, "No such session")
    return ENGINE.exit_now(sid).public()


@app.delete("/api/trade/sessions/{sid}")
def trade_remove(sid: str):
    try:
        ENGINE.remove_session(sid)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True}


@app.post("/api/trade/stop-all")
def trade_stop_all(req: TradeStopRequest):
    return {"stopped": ENGINE.stop_all(req.square_off)}


# --------------------------------------------------------------------------- #
# Frontend pages
# --------------------------------------------------------------------------- #
STATIC = HERE / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")
PAGES = {"/": "index.html", "/screener": "screener.html", "/backtest": "backtest.html", "/trade": "trade.html"}


def _page(name: str):
    return lambda: FileResponse(STATIC / name)


for _route, _file in PAGES.items():
    app.add_api_route(_route, _page(_file), methods=["GET"], include_in_schema=False)
