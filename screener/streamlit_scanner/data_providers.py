"""
Market-data providers: Yahoo Finance (yfinance) and TradingView.

Both return the same shape so the screener and backtester don't care where data came from:
  - daily:    {ticker: DataFrame[Open, High, Low, Close, Volume]} indexed by tz-naive dates
  - intraday: {ticker: DataFrame[Open, High, Low, Close, Volume]} indexed by tz-aware IST timestamps
Tickers use the yfinance convention throughout (RELIANCE.NS, 500325.BO); TradingView symbols
are derived from them (NSE:RELIANCE).

TradingView has no official public data API. Two unofficial clients are supported:
  - tvkit            (preferred; maintained, Python >= 3.11)   pip install tvkit
  - tvDatafeed       (fallback)                                 pip install tradingview-datafeed
Both use TradingView's chart websocket. Without login, history is capped at ~5,000 bars per
symbol and some data may be delayed or limited. Optional login:
  - tvkit:      set TV_BROWSER=chrome (reads your TradingView cookies) or TV_AUTH_TOKEN=<token>
  - tvDatafeed: set TV_USERNAME / TV_PASSWORD
Use for personal research; TradingView's terms don't permit automated data collection at scale.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import time
from datetime import datetime
from typing import Callable

import pandas as pd

log = logging.getLogger("data_providers")
IST = "Asia/Kolkata"

PROVIDERS = {
    "yahoo": {"name": "Yahoo Finance", "intraday_max_days": 59,
              "note": "Free, no login. 15-minute candles limited to the last ~60 days."},
    "tradingview": {"name": "TradingView", "intraday_max_days": 180,
                    "note": "Unofficial websocket API. ~5,000 bars per symbol without login "
                            "(≈200 days of 15-minute candles). Slower for large universes."},
}

ProgressCB = Callable[[int, int], None] | None

# Candle timeframes. Intraday ones are downloaded directly; 1d/1wk/1mo are built from daily bars
# (weekly/monthly are resampled here so both providers produce identical bars).
TIMEFRAMES: dict[str, dict] = {
    "5m":  {"label": "5 minutes",  "minutes": 5},
    "15m": {"label": "15 minutes", "minutes": 15},
    "30m": {"label": "30 minutes", "minutes": 30},
    "1h":  {"label": "1 hour",     "minutes": 60},
    "1d":  {"label": "1 day",      "minutes": None, "days_per_bar": 1},
    "1wk": {"label": "1 week",     "minutes": None, "days_per_bar": 5},
    "1mo": {"label": "1 month",    "minutes": None, "days_per_bar": 21},
}
YAHOO_PERIODS = [("1mo", 30), ("3mo", 91), ("6mo", 182), ("1y", 365), ("2y", 730), ("5y", 1826), ("10y", 3652), ("max", 10**6)]


def is_intraday(tf: str) -> bool:
    return TIMEFRAMES[tf]["minutes"] is not None


def tf_minutes(tf: str) -> int | None:
    return TIMEFRAMES[tf]["minutes"]


def max_history_days(source: str, tf: str) -> int:
    """How far back each source can go for a timeframe (calendar days for daily+, sessions for intraday)."""
    m = tf_minutes(tf)
    if m is None:
        return 20 * 365 if source == "yahoo" else int(5000 * 365 / 252)
    if source == "yahoo":
        return 59 if m < 60 else 729                      # Yahoo: ~60 days of <1h bars, ~730 days of 1h
    return int(5000 // math.ceil(375 / m))                 # TradingView: ~5,000 bars without login


# --------------------------------------------------------------------------- #
# Availability
# --------------------------------------------------------------------------- #
def _tv_backend() -> str | None:
    """Which TradingView client is installed: 'tvkit', 'tvdatafeed' or None."""
    try:
        import tvkit.api.chart.ohlcv  # noqa: F401
        return "tvkit"
    except Exception:
        pass
    try:
        import tvDatafeed  # noqa: F401
        return "tvdatafeed"
    except Exception:
        return None


def available() -> dict[str, dict]:
    tv = _tv_backend()
    out = {}
    for key, meta in PROVIDERS.items():
        ok = True if key == "yahoo" else tv is not None
        out[key] = {**meta, "available": ok, "max_days": {tf: max_history_days(key, tf) for tf in TIMEFRAMES},
                    "backend": "yfinance" if key == "yahoo" else tv,
                    "install_hint": None if ok else "pip install tvkit   (or: pip install tradingview-datafeed)"}
    return out


def check(source: str) -> None:
    if source not in PROVIDERS:
        raise ValueError(f"Unknown data source '{source}'")
    if source == "tradingview" and _tv_backend() is None:
        raise RuntimeError("TradingView support needs the tvkit package: pip install tvkit "
                           "(or the fallback: pip install tradingview-datafeed)")


# --------------------------------------------------------------------------- #
# Symbol mapping
# --------------------------------------------------------------------------- #
def to_tv_symbol(ticker: str) -> tuple[str, str]:
    """RELIANCE.NS -> ('NSE', 'RELIANCE');  M&M.NS -> ('NSE', 'M_M');  BAJAJ-AUTO.NS -> ('NSE', 'BAJAJ_AUTO')."""
    base, _, suffix = ticker.upper().rpartition(".")
    if not base:
        base, suffix = ticker.upper(), "NS"
    exchange = {"NS": "NSE", "BO": "BSE"}.get(suffix, "NSE")
    return exchange, re.sub(r"[^A-Z0-9_]", "_", base)


def _period_to_days(period: str) -> int:
    m = re.fullmatch(r"(\d+)\s*(d|mo|y)", period.strip().lower())
    if not m:
        return 365
    n, unit = int(m.group(1)), m.group(2)
    return n * {"d": 1, "mo": 31, "y": 366}[unit]


# --------------------------------------------------------------------------- #
# Frame normalisation
# --------------------------------------------------------------------------- #
COLS = ["Open", "High", "Low", "Close", "Volume"]


def _finish_daily(df: pd.DataFrame, days: int) -> pd.DataFrame:
    idx = df.index
    if idx.tz is not None:
        idx = idx.tz_convert(IST).tz_localize(None)
    df.index = pd.DatetimeIndex(idx).normalize()
    df.index.name = None
    df = df[~df.index.duplicated(keep="last")].sort_index()
    cutoff = df.index.max() - pd.Timedelta(days=days)
    return df[df.index > cutoff][COLS].dropna(subset=["Close"])


def _finish_intraday(df: pd.DataFrame, days: int) -> pd.DataFrame:
    idx = df.index
    idx = idx.tz_localize("UTC") if idx.tz is None else idx
    df.index = idx.tz_convert(IST)
    df.index.name = None
    df = df[~df.index.duplicated(keep="last")].sort_index()
    sessions = sorted(set(df.index.date))[-days:]
    return df[pd.Index(df.index.date).isin(sessions)][COLS].dropna(subset=["Close"])


# --------------------------------------------------------------------------- #
# TradingView
# --------------------------------------------------------------------------- #
def _tv_fetch(tickers: list[str], interval: str, bars: int, concurrency: int,
              progress_callback: ProgressCB) -> dict[str, pd.DataFrame]:
    backend = _tv_backend()
    if backend == "tvkit":
        return asyncio.run(_tvkit_fetch(tickers, interval, bars, concurrency, progress_callback))
    if backend == "tvdatafeed":
        return _tvdatafeed_fetch(tickers, interval, bars, progress_callback)
    check("tradingview")
    return {}


async def _tvkit_fetch(tickers, interval, bars, concurrency, progress_callback) -> dict[str, pd.DataFrame]:
    from tvkit.api.chart.ohlcv import OHLCV

    auth = {}
    if os.getenv("TV_AUTH_TOKEN"):
        auth["auth_token"] = os.environ["TV_AUTH_TOKEN"]
    elif os.getenv("TV_BROWSER"):
        auth["browser"] = os.environ["TV_BROWSER"]

    out: dict[str, pd.DataFrame] = {}
    done = 0
    sem = asyncio.Semaphore(max(1, concurrency))
    total_batches = math.ceil(len(tickers) / 25) or 1

    async def one(ticker: str):
        nonlocal done
        ex, sym = to_tv_symbol(ticker)
        async with sem:
            for attempt in range(2):
                try:
                    async with OHLCV(max_attempts=2, **auth) as client:
                        rows = await client.get_historical_ohlcv(exchange_symbol=f"{ex}:{sym}",
                                                                 interval=interval, bars_count=bars)
                    if rows:
                        df = pd.DataFrame([(b.timestamp, b.open, b.high, b.low, b.close, b.volume) for b in rows],
                                          columns=["ts", *COLS])
                        df.index = pd.to_datetime(df.pop("ts"), unit="s", utc=True)
                        out[ticker] = df
                    break
                except Exception as exc:  # unknown symbol, rate limit, network
                    if attempt == 1:
                        log.debug("TradingView fetch failed for %s: %s", ticker, exc)
                    else:
                        await asyncio.sleep(1.0)
        done += 1
        if progress_callback and (done % 25 == 0 or done == len(tickers)):
            progress_callback(math.ceil(done / 25), total_batches)

    await asyncio.gather(*(one(t) for t in tickers))
    return out


def _tvdatafeed_fetch(tickers, interval, bars, progress_callback) -> dict[str, pd.DataFrame]:
    from tvDatafeed import Interval, TvDatafeed

    imap = {"1": Interval.in_1_minute, "5": Interval.in_5_minute, "15": Interval.in_15_minute,
            "30": Interval.in_30_minute, "60": Interval.in_1_hour, "1D": Interval.in_daily}
    tv = TvDatafeed(os.getenv("TV_USERNAME"), os.getenv("TV_PASSWORD")) if os.getenv("TV_USERNAME") else TvDatafeed()
    local_tz = datetime.now().astimezone().tzinfo   # tvDatafeed returns naive local-time datetimes
    out: dict[str, pd.DataFrame] = {}
    total_batches = math.ceil(len(tickers) / 25) or 1
    for i, t in enumerate(tickers, 1):
        ex, sym = to_tv_symbol(t)
        try:
            df = tv.get_hist(symbol=sym, exchange=ex, interval=imap[interval], n_bars=bars)
            if df is not None and not df.empty:
                df = df.rename(columns=str.title)
                df.index = pd.DatetimeIndex(df.index).tz_localize(local_tz).tz_convert("UTC")
                out[t] = df[COLS]
        except Exception as exc:
            log.debug("tvDatafeed fetch failed for %s: %s", t, exc)
        if progress_callback and (i % 25 == 0 or i == len(tickers)):
            progress_callback(math.ceil(i / 25), total_batches)
        time.sleep(0.05)
    return out


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def download_daily(tickers: list[str], period: str = "1y", source: str = "yahoo", batch_size: int = 100,
                   progress_callback: ProgressCB = None, concurrency: int = 6) -> dict[str, pd.DataFrame]:
    check(source)
    if source == "yahoo":
        import near_52w_high as core  # yfinance implementation lives there
        return core.download_history(tickers, period=period, batch_size=batch_size,
                                     progress_callback=progress_callback)
    days = _period_to_days(period)
    bars = min(5000, int(days * 252 / 365) + 10)
    raw = _tv_fetch(tickers, "1D", bars, concurrency, progress_callback)
    out = {t: _finish_daily(df, days) for t, df in raw.items()}
    log.info("TradingView daily: %d / %d tickers", len(out), len(tickers))
    return {t: df for t, df in out.items() if not df.empty}


def download_intraday(tickers: list[str], minutes: int = 15, days: int = 59, source: str = "yahoo",
                      concurrency: int = 6) -> dict[str, pd.DataFrame]:
    check(source)
    tf = next((k for k, v in TIMEFRAMES.items() if v["minutes"] == minutes), "15m")
    days = min(days, max_history_days(source, tf))
    if source == "yahoo":
        import yfinance as yf
        raw = yf.download(tickers, period=f"{days}d", interval=f"{minutes}m", group_by="ticker",
                          auto_adjust=False, threads=True, progress=False)
        out = {}
        for t in tickers:
            try:
                df = raw[t] if isinstance(raw.columns, pd.MultiIndex) else raw
            except KeyError:
                continue
            df = df[COLS].dropna(subset=["Open", "High", "Low", "Close"])
            if not df.empty:
                out[t] = _finish_intraday(df.copy(), days)
        return out
    bars_per_day = math.ceil(375 / minutes)        # NSE session 09:15-15:30 = 375 minutes
    bars = min(5000, days * bars_per_day + bars_per_day)
    raw = _tv_fetch(tickers, str(minutes), bars, concurrency, None)
    return {t: _finish_intraday(df, days) for t, df in raw.items() if not df.empty}


# --------------------------------------------------------------------------- #
# Any timeframe
# --------------------------------------------------------------------------- #
def resample_bars(daily: pd.DataFrame, tf: str) -> pd.DataFrame:
    """Daily -> weekly/monthly bars labelled by period start (Monday / 1st of month)."""
    if tf == "1d" or daily.empty:
        return daily
    per = daily.index.to_period("W-SUN" if tf == "1wk" else "M")
    g = daily.groupby(per)
    out = pd.DataFrame({"Open": g["Open"].first(), "High": g["High"].max(), "Low": g["Low"].min(),
                        "Close": g["Close"].last(), "Volume": g["Volume"].sum()})
    out.index = out.index.to_timestamp(how="start")
    out.index.name = None
    return out


def complete_bars(df: pd.DataFrame, tf: str, today: "pd.Timestamp | None" = None) -> pd.DataFrame:
    """Drop the still-forming daily/weekly/monthly bar (today / this week / this month)."""
    if is_intraday(tf) or df.empty:
        return df
    today = pd.Timestamp(today or pd.Timestamp.now(tz=IST).date()).normalize()
    if tf == "1d":
        end = df.index + pd.Timedelta(days=1)
    elif tf == "1wk":
        end = df.index + pd.Timedelta(days=7)
    else:
        end = df.index + pd.offsets.MonthBegin(1)
    return df[end <= today]


def download_bars(tickers: list[str], tf: str, days: int, source: str = "yahoo",
                  complete_only: bool = False, concurrency: int = 6) -> dict[str, pd.DataFrame]:
    """Candles for any timeframe. Intraday: tz-aware IST index. Daily+: tz-naive dates (period start)."""
    check(source)
    if tf not in TIMEFRAMES:
        raise ValueError(f"Unknown timeframe '{tf}'")
    days = min(int(days), max_history_days(source, tf))
    m = tf_minutes(tf)
    if m is not None:
        return download_intraday(tickers, minutes=m, days=days, source=source, concurrency=concurrency)
    if source == "yahoo":
        period = next(p for p, d in YAHOO_PERIODS if d >= days)
    else:
        period = f"{days}d"
    daily = download_daily(tickers, period=period, source=source, concurrency=concurrency)
    out = {}
    cutoff = pd.Timestamp.now().normalize() - pd.Timedelta(days=days)
    for t, df in daily.items():
        df = df[df.index >= cutoff]
        df = resample_bars(df, tf)
        if complete_only:
            df = complete_bars(df, tf)
        if not df.empty:
            out[t] = df
    return out
