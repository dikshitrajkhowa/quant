"""
Backtest engine for intraday strategies.

Strategies are registered in BACKTESTS (same pattern as the screener's strategies.py):
each has metadata, UI parameters and a `simulate(df, params) -> list[Trade]` function.
`run_backtest` downloads candles, simulates every ticker and scores the result.

All results are in R-multiples (profit / initial risk) after costs, plus rupee P&L
for a fixed amount of capital per trade.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Callable

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "streamlit_scanner"))
import data_providers as dp  # noqa: E402  (shared with the screener)

log = logging.getLogger("screener.backtest")
IST = "Asia/Kolkata"


# --------------------------------------------------------------------------- #
# Data
# --------------------------------------------------------------------------- #
_CACHE: dict[tuple[str, str, str, int], tuple[float, pd.DataFrame]] = {}
_CACHE_LOCK = threading.Lock()
INTRADAY_TTL = 15 * 60


def _drop_incomplete(df: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """Remove the still-forming candle (bar start + interval in the future)."""
    now = pd.Timestamp.now(tz=IST)
    return df[df.index + pd.Timedelta(minutes=minutes) <= now]


def fetch_candles(tickers: list[str], interval: str = "15m", days: int = 59,
                  source: str = "yahoo") -> dict[str, pd.DataFrame]:
    """Completed candles for any timeframe (5m ... 1mo) from Yahoo or TradingView, cached 15 min.
    Intraday: tz-aware IST index. Daily/weekly/monthly: dates (period start)."""
    days = min(days, dp.max_history_days(source, interval))
    out, need = {}, []
    now = time.time()
    with _CACHE_LOCK:
        for t in tickers:
            hit = _CACHE.get((source, t, interval, days))
            if hit and now - hit[0] < INTRADAY_TTL:
                out[t] = hit[1]
            else:
                need.append(t)
    if need:
        fetched = dp.download_bars(need, interval, days, source=source)
        m = dp.tf_minutes(interval)
        for t, df in fetched.items():
            df = _drop_incomplete(df, m) if m else dp.complete_bars(df, interval)
            if df.empty:
                continue
            out[t] = df
            with _CACHE_LOCK:
                _CACHE[(source, t, interval, days)] = (now, df)
    return out


fetch_intraday = fetch_candles   # backwards-compatible name


def interval_of(s: "BacktestStrategy", params: dict) -> str:
    """The candle timeframe chosen for a run (falls back to the strategy's default)."""
    tf = params.get("timeframe") or s.interval
    return tf if tf in dp.TIMEFRAMES else s.interval


def live_days(s: "BacktestStrategy", tf: str) -> int:
    """Calendar days of history the live engine needs for indicators on this timeframe."""
    if dp.is_intraday(tf):
        return s.live_history_days
    return {"1d": 120, "1wk": 420, "1mo": 1300}[tf]


TIMEFRAME_OPTIONS = [[k, v["label"]] for k, v in dp.TIMEFRAMES.items()]


# --------------------------------------------------------------------------- #
# Model
# --------------------------------------------------------------------------- #
@dataclass
class Param:
    key: str
    label: str
    kind: str  # float | int | bool | choice
    default: float | int | bool
    min: float | int | None = None
    max: float | int | None = None
    step: float | int | None = None
    help: str | None = None
    group: str = ""          # UI section heading
    options: list | None = None   # for kind == "choice": [[value, label], ...]


@dataclass
class Trade:
    ticker: str
    signal_time: str
    entry: float
    stop: float
    target: float
    exit_time: str
    exit: float
    reason: str          # target | stop | target (gap) | stop (gap) | end of day
    bars_held: int
    risk: float
    gross_r: float = 0.0
    net_r: float = 0.0
    qty: int = 0              # pre-set by risk-sized strategies; otherwise capital // entry
    pnl: float = 0.0
    side: str = "long"        # long | short
    entry_time: str = ""      # when the position was opened (signal_time if entry is at the signal close)


@dataclass
class BacktestStrategy:
    key: str
    name: str
    description: str
    rules: list[str]
    interval: str
    max_days: int
    simulate: Callable[[pd.DataFrame, dict, str], list[Trade]]
    params: list[Param] = field(default_factory=list)
    # live trading: given completed candles, return an entry signal for the LAST candle (or None)
    live_signal: Callable[[pd.DataFrame, dict], dict | None] | None = None
    last_entry_bar: tuple[int, int] = (15, 0)   # no new entries on candles starting after this (IST)
    # optional extras used by the live engine
    live_opposite: Callable[[pd.DataFrame, dict, int], bool] | None = None   # opposite signal on last candle?
    size: Callable[[float, float, dict], int] | None = None                 # qty from (entry, risk, params)
    square_off: tuple[int, int] | None = None                                # intraday exit time (IST)
    live_history_days: int = 3                                               # candles needed for indicators


# --------------------------------------------------------------------------- #
# Strategy: 15-minute breakout (long only, 1:1 R:R)
# --------------------------------------------------------------------------- #
def simulate_breakout(df: pd.DataFrame, p: dict, ticker: str) -> list[Trade]:
    """
    On each completed candle i: if close[i] > high[i-1] -> buy at close[i],
    stop = low[i], target = close[i] + rr * (close[i] - low[i]).
    One position at a time. Exits are checked on candles after the entry candle:
      - candle opens beyond stop/target (gap)  -> exit at the open
      - candle touches both stop and target   -> stop (conservative) unless stop_first is off
      - otherwise whichever level is touched
      - intraday mode: square off at the close of the session's last candle
    """
    o, h, l, c = (df[k].to_numpy(float) for k in ("Open", "High", "Low", "Close"))
    ts = df.index
    day = np.array([t.date() for t in ts])
    tf = p.get("timeframe", "15m")
    bar_min = dp.tf_minutes(tf) or 0
    mod = np.array([t.hour * 60 + t.minute for t in ts])
    sq_min = LS_SQUARE_OFF[0] * 60 + LS_SQUARE_OFF[1]
    # the final candle only counts as a session close if the session actually ended (>= 15:15 bar)
    final_is_close = len(ts) > 0 and (ts[-1].hour, ts[-1].minute) >= (15, 15)
    last_of_day = np.r_[day[1:] != day[:-1], final_is_close]
    rr = float(p["rr"])
    eod = bool(p["square_off_eod"]) and dp.is_intraday(p.get("timeframe", "15m"))   # daily+: hold across bars
    stop_first = bool(p["stop_first"])
    min_risk = float(p["min_risk_pct"]) / 100

    trades: list[Trade] = []
    i, n = 1, len(df)
    while i < n:
        same_session = day[i] == day[i - 1]
        breakout = c[i] > h[i - 1] and (same_session or not eod)
        risk = c[i] - l[i]
        too_late = eod and mod[i] + bar_min >= sq_min           # signal candle ends at/after the 15:15 square-off
        if not breakout or risk <= 0 or risk < c[i] * min_risk or (eod and last_of_day[i]) or too_late:
            i += 1
            continue

        entry, stop, target = c[i], l[i], c[i] + rr * risk
        exit_px, reason, j = None, None, i + 1
        while j < n:
            if eod and day[j] == day[i] and mod[j] >= sq_min:     # intraday square-off at 15:15 (open of that candle)
                exit_px, reason = o[j], "end of day"
                break
            if eod and day[j] != day[i]:                          # data gap: no 15:15 candle that day
                exit_px, reason, j = c[j - 1], "end of day", j - 1
                break
            if not eod or day[j] == day[i]:
                if o[j] <= stop:
                    exit_px, reason = o[j], "stop (gap)"
                elif o[j] >= target:
                    exit_px, reason = o[j], "target (gap)"
                else:
                    hit_stop, hit_tgt = l[j] <= stop, h[j] >= target
                    if hit_stop and hit_tgt:
                        exit_px, reason = (stop, "stop") if stop_first else (target, "target")
                    elif hit_stop:
                        exit_px, reason = stop, "stop"
                    elif hit_tgt:
                        exit_px, reason = target, "target"
                if exit_px is None and eod and last_of_day[j]:
                    exit_px, reason = c[j], "end of day"
            if exit_px is not None:
                break
            j += 1
        if exit_px is None:  # still open at the end of the data -> not a completed trade
            break

        trades.append(Trade(
            ticker=ticker, signal_time=ts[i].strftime("%Y-%m-%d %H:%M"),
            entry=round(float(entry), 4), stop=round(float(stop), 4), target=round(float(target), 4),
            exit_time=ts[j].strftime("%Y-%m-%d %H:%M"), exit=round(float(exit_px), 4),
            reason=reason, bars_held=int(j - i), risk=round(float(risk), 6),
            gross_r=round(float((exit_px - entry) / risk), 4),   # from unrounded prices
        ))
        i = max(j, i + 1)  # flat again after the exit candle closes; it can itself be a new signal
    return trades


def live_breakout_signal(df: pd.DataFrame, p: dict) -> dict | None:
    """Same rule as simulate_breakout, evaluated on the most recent completed candle only."""
    if len(df) < 2:
        return None
    prev, cur = df.iloc[-2], df.iloc[-1]
    t_prev, t_cur = df.index[-2], df.index[-1]
    if bool(p.get("square_off_eod", True)) and dp.is_intraday(p.get("timeframe", "15m")):
        if t_prev.date() != t_cur.date():
            return None                                   # first candle of the day has no same-session previous
        tf_min = dp.tf_minutes(p.get("timeframe", "15m")) or 15
        if t_cur.hour * 60 + t_cur.minute + tf_min >= LS_SQUARE_OFF[0] * 60 + LS_SQUARE_OFF[1]:
            return None                                   # candle ends at/after the 15:15 square-off
    close, high_prev, low = float(cur["Close"]), float(prev["High"]), float(cur["Low"])
    risk = close - low
    if not (close > high_prev) or risk <= 0 or risk < close * float(p.get("min_risk_pct", 0)) / 100:
        return None
    rr = float(p.get("rr", 1.0))
    return {"bar_time": t_cur.strftime("%Y-%m-%d %H:%M"), "entry": round(close, 2), "stop": round(low, 2),
            "target": round(close + rr * risk, 2), "risk": round(risk, 4), "prev_high": round(high_prev, 2)}


# --------------------------------------------------------------------------- #
# Strategy: 15-minute breakout, long & short, with filters and risk rules
# --------------------------------------------------------------------------- #
OPEN_MIN, CLOSE_MIN = 9 * 60 + 15, 15 * 60 + 30
LS_SQUARE_OFF = (15, 15)


@dataclass
class _Ctx:
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray
    c: np.ndarray
    v: np.ndarray
    ts: pd.DatetimeIndex
    day: np.ndarray          # date ordinal per candle
    mod: np.ndarray          # minute of day (candle start)
    vwap: np.ndarray
    atr: np.ndarray          # ATR(14) of the candles BEFORE this one
    vol_avg: np.ndarray      # 20-candle average volume BEFORE this one
    adv: np.ndarray          # average daily volume of the previous (up to) 5 sessions
    intraday: bool = True
    minutes: int = 15        # candle length for intraday timeframes


def ls_context(df: pd.DataFrame, tf: str = "15m") -> _Ctx:
    o, h, l, c = (df[k].to_numpy(float) for k in ("Open", "High", "Low", "Close"))
    v = df["Volume"].fillna(0).to_numpy(float) if "Volume" in df else np.zeros(len(df))
    ts = df.index
    day = np.array([t.toordinal() for t in ts.date])
    mod = np.array([t.hour * 60 + t.minute for t in ts])
    g = pd.Series(day)
    tp = (h + l + c) / 3
    intraday = dp.is_intraday(tf)
    if intraday:   # session VWAP, resets each day
        cum_pv = pd.Series(tp * v).groupby(g).cumsum().to_numpy()
        cum_v = pd.Series(v).groupby(g).cumsum().to_numpy()
    else:          # daily+: 20-bar volume-weighted average price
        cum_pv = pd.Series(tp * v).rolling(20, min_periods=10).sum().to_numpy()
        cum_v = pd.Series(v).rolling(20, min_periods=10).sum().to_numpy()
    with np.errstate(invalid="ignore", divide="ignore"):
        vwap = np.where(cum_v > 0, cum_pv / cum_v, np.nan)
    prev_c = np.r_[np.nan, c[:-1]]
    tr = np.nanmax(np.vstack([h - l, np.abs(h - prev_c), np.abs(l - prev_c)]), axis=0)
    atr = pd.Series(tr).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean().shift(1).to_numpy()
    vol_avg = pd.Series(v).rolling(20, min_periods=10).mean().shift(1).to_numpy()
    if intraday:
        daily = pd.Series(v).groupby(g).sum()
        prev5 = daily.shift(1).rolling(5, min_periods=1).mean()
        adv = g.map(prev5).to_numpy(float)
    else:          # bar volume / trading days per bar, averaged over the previous 5 bars
        adv = (pd.Series(v) / dp.TIMEFRAMES[tf]["days_per_bar"]).shift(1).rolling(5, min_periods=1).mean().to_numpy()
    return _Ctx(o, h, l, c, v, ts, day, mod, vwap, atr, vol_avg, adv, intraday, dp.tf_minutes(tf) or 0)


def ls_signal(x: _Ctx, j: int, p: dict, check_window: bool = True, any_side: bool = False) -> int:
    """+1 (long), -1 (short) or 0 for the candle j that just closed. Entry would be at the next candle's open.

    check_window=False and any_side=True are used for "opposite signal" exits, which ignore the entry
    time window and the long/short switches but still require the price/volume filters."""
    if j < 1 or (x.intraday and x.day[j] != x.day[j - 1]):
        return 0                                                 # intraday: first candle of the day has no same-day previous
    c, h, l = x.c[j], x.h[j], x.l[j]
    rng = h - l
    if rng <= 0 or c <= 0 or rng < c * float(p.get("min_range_pct", 0)) / 100:
        return 0
    mx = float(p.get("max_range_atr", 0))
    if mx > 0 and np.isfinite(x.atr[j]) and rng > mx * x.atr[j]:
        return 0
    if p.get("use_volume", True) and np.isfinite(x.vol_avg[j]) and x.vol_avg[j] > 0 \
            and x.v[j] <= float(p.get("vol_mult", 1.0)) * x.vol_avg[j]:
        return 0
    if c < float(p.get("min_price", 0)):
        return 0
    mdv = float(p.get("min_daily_vol_lakh", 0)) * 1e5
    if mdv > 0 and np.isfinite(x.adv[j]) and x.adv[j] < mdv:
        return 0
    buf = c * float(p.get("buffer_pct", 0)) / 100
    vw = x.vwap[j]
    use_vwap = bool(p.get("use_vwap", True)) and np.isfinite(vw)
    side = 0
    if (any_side or p.get("allow_long", True)) and c > x.h[j - 1] + buf and (not use_vwap or c > vw):
        side = 1
    elif ((any_side or (p.get("allow_short", True) and x.intraday))      # cash-market shorts are intraday-only
          and c < x.l[j - 1] - buf and (not use_vwap or c < vw)):
        side = -1
    if side and check_window and x.intraday:
        entry_min = x.mod[j] + x.minutes                             # next candle's open
        first_ok = OPEN_MIN + x.minutes * int(p.get("skip_first_candles", 2))
        last_ok = CLOSE_MIN - int(p.get("entry_cutoff_min", 45))
        if not (first_ok <= entry_min <= last_ok):
            return 0
    return side


def ls_size(entry: float, risk: float, p: dict) -> int:
    """Risk a fixed % of capital per trade, capped at a max % of capital in one position."""
    if entry <= 0 or risk <= 0:
        return 0
    cap = float(p.get("capital", 500000))
    q_risk = math.floor(cap * float(p.get("risk_pct", 0.5)) / 100 / risk)
    q_cap = math.floor(cap * float(p.get("max_position_pct", 20)) / 100 / entry)
    return max(0, min(q_risk, q_cap))


def simulate_breakout_ls(df: pd.DataFrame, p: dict, ticker: str) -> list[Trade]:
    """
    Signal on the close of candle j  -> enter at the open of candle j+1.
      long : close > prev high + buffer (and above VWAP)   stop = signal candle low
      short: close < prev low  - buffer (and below VWAP)   stop = signal candle high
      target = entry ± rr × risk;  at +1R the stop moves to entry (breakeven) if enabled
    Exits, in order on each candle: square-off time (at the open), opposite signal (at the open after
    it closed), gap through stop/target (at the open), stop/target touched intrabar (stop first if both).
    Per-stock limits: max trades per day, stop after N consecutive losses, cool-down after a stop-out.
    """
    x = ls_context(df, p.get("timeframe", "15m"))
    n = len(df)
    intraday = x.intraday
    rr = float(p.get("rr", 1.5))
    stop_first = bool(p.get("stop_first", True))
    sq = LS_SQUARE_OFF[0] * 60 + LS_SQUARE_OFF[1] if intraday else 10**6     # daily+: no square-off
    # per-day limits are intraday risk controls; on daily+ bars they don't apply
    max_trades = (int(p.get("max_trades_per_stock", 3)) or 10**6) if intraday else 10**6
    max_consec = (int(p.get("max_consec_losses", 2)) or 10**6) if intraday else 10**6
    cooldown = int(p.get("cooldown_candles", 1))

    trades: list[Trade] = []
    pos: dict | None = None
    pending_entry: tuple | None = None
    pending_exit = False
    cur_day, n_today, consec, cool_until = None, 0, 0, -1

    def close_pos(j_exit: int, px: float, reason: str, t_exit=None):
        nonlocal pos, consec, cool_until
        s = pos["side"]
        gross = s * (px - pos["entry"]) / pos["risk"]
        trades.append(Trade(
            ticker=ticker, signal_time=x.ts[pos["sig_j"]].strftime("%Y-%m-%d %H:%M"),
            entry_time=x.ts[pos["j"]].strftime("%Y-%m-%d %H:%M"),
            entry=round(float(pos["entry"]), 4), stop=round(float(pos["init_stop"]), 4),
            target=round(float(pos["target"]), 4),
            exit_time=(t_exit or x.ts[j_exit]).strftime("%Y-%m-%d %H:%M"), exit=round(float(px), 4),
            reason=reason, bars_held=int(j_exit - pos["j"]), risk=round(float(pos["risk"]), 6),
            gross_r=round(float(gross), 4), qty=int(pos["qty"]), side="long" if s > 0 else "short"))
        consec = consec + 1 if gross < -1e-9 else 0
        if reason.startswith("stop") or reason.startswith("breakeven"):
            cool_until = j_exit + cooldown
        pos = None

    for j in range(n):
        if intraday and x.day[j] != cur_day:
            if pos is not None:                         # session ended without a square-off candle (data gap)
                close_pos(j - 1, x.c[j - 1], "end of day")
            cur_day, n_today, consec, pending_exit = x.day[j], 0, 0, False
            pending_entry = None if pending_entry and x.day[pending_entry[2]] != x.day[j] else pending_entry

        # 1) entry at this candle's open
        if pending_entry is not None and pos is None:
            side, stop, sig_j = pending_entry
            pending_entry = None
            if x.mod[j] < sq:
                entry = x.o[j]
                risk = side * (entry - stop)
                qty = ls_size(entry, risk, p) if risk > 0 else 0
                if qty >= 1:
                    pos = {"side": side, "entry": entry, "stop": stop, "init_stop": stop, "risk": risk,
                           "target": entry + side * rr * risk, "j": j, "sig_j": sig_j, "qty": qty, "be": False}
                    n_today += 1

        # 2) manage an open position during this candle
        if pos is not None:
            s, st, tg = pos["side"], pos["stop"], pos["target"]
            stop_reason = "breakeven stop" if pos["be"] else "stop"
            if pending_exit:
                pending_exit = False
                close_pos(j, x.o[j], "opposite signal")
            elif x.mod[j] >= sq:
                close_pos(j, x.o[j], "end of day")
            elif s * (x.o[j] - st) <= 0:
                close_pos(j, x.o[j], stop_reason + " (gap)")
            elif s * (x.o[j] - tg) >= 0:
                close_pos(j, x.o[j], "target (gap)")
            else:
                hit_stop = x.l[j] <= st if s > 0 else x.h[j] >= st
                hit_tgt = x.h[j] >= tg if s > 0 else x.l[j] <= tg
                if hit_stop and (not hit_tgt or stop_first):
                    close_pos(j, st, stop_reason)
                elif hit_tgt:
                    close_pos(j, tg, "target")
                else:
                    if p.get("breakeven", True) and not pos["be"]:
                        trig = pos["entry"] + s * pos["risk"]
                        if (x.h[j] >= trig) if s > 0 else (x.l[j] <= trig):
                            pos["stop"], pos["be"] = pos["entry"], True
                    if p.get("exit_on_opposite", True) and ls_signal(x, j, p, check_window=False, any_side=True) == -s:
                        pending_exit = True

        # 3) new signal on this candle's close
        if pos is None and pending_entry is None and j + 1 < n and (not intraday or x.day[j + 1] == x.day[j]):
            if n_today < max_trades and consec < max_consec and j >= cool_until:
                sig = ls_signal(x, j, p)
                if sig:
                    pending_entry = (sig, x.l[j] if sig > 0 else x.h[j], j)
    return trades


def live_breakout_ls_signal(df: pd.DataFrame, p: dict) -> dict | None:
    """Entry signal on the most recent completed candle (live trading). Entry is at market right away,
    which is the next candle's open - the same price the backtest assumes."""
    if len(df) < 2:
        return None
    x = ls_context(df, p.get("timeframe", "15m"))
    j = len(df) - 1
    side = ls_signal(x, j, p)
    if not side:
        return None
    stop = x.l[j] if side > 0 else x.h[j]
    close = x.c[j]
    risk = side * (close - stop)
    # full precision (rounding here would shift the target vs the backtest); round only for display
    return {"bar_time": x.ts[j].strftime("%Y-%m-%d %H:%M"), "side": side, "entry": float(close),
            "stop": float(stop), "risk": float(risk), "rr": float(p.get("rr", 1.5)),
            "prev_high": round(float(x.h[j - 1]), 2), "prev_low": round(float(x.l[j - 1]), 2),
            "vwap": round(float(x.vwap[j]), 2) if np.isfinite(x.vwap[j]) else None}


def live_breakout_ls_opposite(df: pd.DataFrame, p: dict, side: int) -> bool:
    if len(df) < 2:
        return False
    x = ls_context(df, p.get("timeframe", "15m"))
    return ls_signal(x, len(df) - 1, p, check_window=False, any_side=True) == -side


LS_PARAMS = [
    Param("timeframe", "Candle timeframe", "choice", "15m", options=TIMEFRAME_OPTIONS,
          help="Intraday (5m-1h): squared off each day. Daily/weekly/monthly: positions are held across bars "
               "(delivery), long only, no time-of-day rules", group="Data & costs"),
    Param("days", "History (calendar days)", "int", 59, 5, 7300, 1,
          "Capped per source/timeframe: Yahoo ~59 days for 5-30m, ~2 years for 1h, decades for daily+; "
          "TradingView ~5,000 bars", group="Data & costs"),
    Param("cost_pct", "Costs per side (%)", "float", 0.03, 0.0, 0.2, 0.01,
          "Brokerage + STT + exchange + GST + stamp duty, each side", group="Data & costs"),
    Param("allow_long", "Take long (buy) trades", "bool", True, group="Entry"),
    Param("allow_short", "Take short (sell) trades", "bool", True, group="Entry"),
    Param("buffer_pct", "Break buffer (% of price)", "float", 0.05, 0.0, 0.5, 0.01,
          "Close must clear the previous high/low by this much", group="Entry"),
    Param("skip_first_candles", "Skip first N candles", "int", 2, 0, 8, 1,
          "2 = no entries before 09:45", group="Entry"),
    Param("entry_cutoff_min", "No entries in last N minutes", "int", 45, 15, 180, 15,
          "45 = no new entries after 14:45", group="Entry"),
    Param("use_vwap", "VWAP trend filter", "bool", True, "Longs only above VWAP, shorts only below", group="Filters"),
    Param("use_volume", "Volume filter", "bool", True, "Breakout candle volume above its 20-candle average", group="Filters"),
    Param("vol_mult", "Volume vs 20-candle avg (×)", "float", 1.0, 0.5, 3.0, 0.1, group="Filters"),
    Param("min_range_pct", "Min candle range (% of price)", "float", 0.3, 0.0, 1.5, 0.05,
          "Skip small candles whose target can't cover costs", group="Filters"),
    Param("max_range_atr", "Max candle range (× ATR14, 0 = off)", "float", 1.5, 0.0, 4.0, 0.1,
          "Skip exhaustion candles with very wide stops", group="Filters"),
    Param("min_price", "Min price (₹)", "float", 100, 0, 5000, 10, group="Filters"),
    Param("min_daily_vol_lakh", "Min avg daily volume (lakh shares)", "float", 5, 0, 100, 0.5,
          "Average of the previous 5 sessions", group="Filters"),
    Param("rr", "Reward : risk", "float", 1.5, 0.5, 4.0, 0.25, group="Exits"),
    Param("breakeven", "Move stop to entry at +1R", "bool", True, group="Exits"),
    Param("exit_on_opposite", "Exit on an opposite signal", "bool", True,
          "A valid short signal closes a long (and vice-versa) - no reversal", group="Exits"),
    Param("stop_first", "If a candle hits both stop and target, assume stop", "bool", True, group="Exits"),
    Param("capital", "Account capital (₹)", "int", 500000, 10000, 100000000, 10000, group="Risk"),
    Param("risk_pct", "Risk per trade (% of capital)", "float", 0.5, 0.1, 3.0, 0.1,
          "Quantity = capital × risk% ÷ (entry − stop)", group="Risk"),
    Param("max_position_pct", "Max position size (% of capital)", "float", 20, 5, 100, 5, group="Risk"),
    Param("max_trades_per_stock", "Max trades per stock per day", "int", 3, 1, 10, 1, group="Risk"),
    Param("max_consec_losses", "Stop a stock after N losses in a row", "int", 2, 1, 10, 1, group="Risk"),
    Param("cooldown_candles", "Wait N candles after a stop-out", "int", 1, 0, 6, 1, group="Risk"),
    Param("daily_loss_pct", "Daily loss limit (% of capital, 0 = off)", "float", 1.5, 0.0, 10.0, 0.25,
          "No new entries for the rest of the day once realized losses reach this", group="Risk"),
]


BACKTESTS: dict[str, BacktestStrategy] = {s.key: s for s in [
    BacktestStrategy(
        key="breakout_15m",
        name="Candle breakout (long, 1:1)",
        description="Buy when a completed candle (15-minute by default) closes above the previous candle's high. "
                    "Stop at the breakout candle's low, target at 1× the risk above entry.",
        rules=[
            "Signal: the just-completed candle closes above the high of the candle before it",
            "Entry: the breakout candle's close",
            "Stop loss: the breakout candle's low · Risk = entry − stop",
            "Target: entry + R:R × risk (1:1 by default)",
            "Long only, one open position per stock",
            "Timeframe: 15 min by default; daily/weekly/monthly hold positions across bars (delivery)",
        ],
        interval="15m", max_days=180, simulate=simulate_breakout, live_signal=live_breakout_signal,
        square_off=LS_SQUARE_OFF,
        params=[
            Param("timeframe", "Candle timeframe", "choice", "15m", options=TIMEFRAME_OPTIONS,
                  help="Intraday (5m-1h) or daily/weekly/monthly. On daily+ positions are held across bars"),
            Param("days", "History (calendar days)", "int", 59, 5, 7300, 1,
                  "Capped per source/timeframe: Yahoo ~59 days for 5-30m, ~2 years for 1h, decades for daily+; "
                  "TradingView ~5,000 bars"),
            Param("rr", "Reward : risk", "float", 1.0, 0.5, 3.0, 0.25),
            Param("square_off_eod", "Square off at end of day (intraday)", "bool", True,
                  help="Close open positions at the last candle of the session; signals must be within one session"),
            Param("stop_first", "If a candle hits both stop and target, assume stop", "bool", True,
                  help="Conservative: 15-min candles can't tell which level was hit first"),
            Param("cost_pct", "Costs per side (%)", "float", 0.03, 0.0, 0.2, 0.01,
                  help="Brokerage + STT + exchange + GST + stamp duty, as % of trade value, each side"),
            Param("min_risk_pct", "Min risk (% of price)", "float", 0.0, 0.0, 1.0, 0.05,
                  help="Skip breakouts whose stop is too tight (tiny risk means costs eat the trade)"),
            Param("capital", "Capital per trade (₹)", "int", 100000, 10000, 10000000, 10000),
        ],
    ),
    BacktestStrategy(
        key="breakout_ls_15m",
        name="Candle breakout (long & short)",
        description="Trade breaks of the previous candle's high (buy) or low (sell short) on your chosen timeframe, "
                    "filtered by VWAP trend, volume and candle size, with risk-based sizing, a breakeven "
                    "stop, opposite-signal exits, daily limits and a 15:15 square-off.",
        rules=[
            "Long: candle closes above the previous candle's high + buffer, above VWAP · Short: closes below the previous low − buffer, below VWAP",
            "Filters: volume above its 20-candle average · range ≥ min % of price and ≤ 1.5 × ATR · liquid stocks only",
            "Entry: market at the next candle's open, between 09:45 and 14:45 only",
            "Stop: signal candle's low (long) / high (short) · Target: entry ± 1.5 × risk · stop to entry at +1R",
            "Exit early on a valid opposite signal (no reversal) · square off everything at 15:15",
            "Size: risk 0.5% of capital per trade, max 20% of capital per position",
            "Limits: 3 trades/stock/day, stop a stock after 2 losses in a row, wait 1 candle after a stop-out, "
            "halt new entries at −1.5% on the day",
            "Daily/weekly/monthly: long only, held across bars (no square-off or entry window), VWAP = 20-bar VWMA, "
            "per-day limits off",
        ],
        interval="15m", max_days=180, simulate=simulate_breakout_ls, live_signal=live_breakout_ls_signal,
        live_opposite=live_breakout_ls_opposite, size=ls_size, square_off=LS_SQUARE_OFF,
        last_entry_bar=(15, 0), live_history_days=7, params=LS_PARAMS,
    ),
]}


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #
def _apply_costs(trades: list[Trade], cost_pct: float, capital: float) -> None:
    c = cost_pct / 100
    for t in trades:
        sign = 1 if t.side == "long" else -1
        cost_per_share = c * (t.entry + t.exit)
        t.net_r = round(t.gross_r - cost_per_share / t.risk, 4)
        if not t.qty:
            t.qty = int(capital // t.entry) if t.entry > 0 else 0
        t.pnl = round(t.qty * (sign * (t.exit - t.entry) - cost_per_share), 2)
        if not t.entry_time:
            t.entry_time = t.signal_time


def apply_daily_loss_limit(trades: list[Trade], limit_pct: float, capital: float) -> tuple[list[Trade], int]:
    """Account-wide: once the day's realized P&L (trades already closed before a new entry) reaches
    -limit% of capital, drop that day's later entries across all stocks. Returns (kept, n_dropped).
    Approximation: per-stock state (e.g. consecutive-loss counts) isn't re-simulated after a drop."""
    if limit_pct <= 0:
        return trades, 0
    limit = capital * limit_pct / 100
    kept, dropped = [], 0
    by_day: dict[str, list[Trade]] = {}
    for t in sorted(trades, key=lambda x: (x.entry_time, x.ticker)):
        day = t.entry_time[:10]
        accepted = by_day.setdefault(day, [])
        realized = sum(a.pnl for a in accepted if a.exit_time <= t.entry_time)
        if realized <= -limit:
            dropped += 1
            continue
        accepted.append(t)
        kept.append(t)
    return kept, dropped


def summarize(trades: list[Trade], capital: float) -> dict:
    n = len(trades)
    if n == 0:
        return {"trades": 0}
    r = np.array([t.net_r for t in trades])
    g = np.array([t.gross_r for t in trades])
    wins, losses = r[r > 0], r[r <= 0]
    gross_win, gross_loss = wins.sum(), -losses.sum()
    pf = float(gross_win / gross_loss) if gross_loss > 0 else math.inf
    sd = float(r.std(ddof=1)) if n > 1 else 0.0
    t_stat = float(r.mean() / (sd / math.sqrt(n))) if sd > 0 else 0.0
    avg_win = float(wins.mean()) if len(wins) else 0.0
    avg_loss = float(-losses.mean()) if len(losses) else 0.0
    breakeven = avg_loss / (avg_win + avg_loss) if (avg_win + avg_loss) > 0 else None

    order = np.argsort([t.exit_time for t in trades], kind="stable")
    equity = np.cumsum(r[order])
    peak = np.maximum.accumulate(np.r_[0, equity])[1:]
    max_dd = float((peak - equity).max()) if n else 0.0

    reasons = pd.Series([t.reason for t in trades]).value_counts().to_dict()
    pnl = float(sum(t.pnl for t in trades))
    by_side = {}
    for side in ("long", "short"):
        rs = np.array([t.net_r for t in trades if t.side == side])
        if len(rs):
            by_side[side] = {"trades": int(len(rs)), "win_rate": round(float((rs > 0).mean() * 100), 2),
                             "expectancy_r": round(float(rs.mean()), 4), "total_r": round(float(rs.sum()), 2),
                             "net_pnl": round(float(sum(t.pnl for t in trades if t.side == side)), 2)}
    return {
        "by_side": by_side,
        "trades": n,
        "win_rate": round(len(wins) / n * 100, 2),
        "breakeven_win_rate": round(breakeven * 100, 2) if breakeven is not None else None,
        "avg_win_r": round(avg_win, 3), "avg_loss_r": round(avg_loss, 3),
        "expectancy_r": round(float(r.mean()), 4),
        "gross_expectancy_r": round(float(g.mean()), 4),
        "total_r": round(float(r.sum()), 2),
        "profit_factor": round(pf, 3) if math.isfinite(pf) else None,
        "t_stat": round(t_stat, 2),
        "max_drawdown_r": round(max_dd, 2),
        "net_pnl": round(pnl, 2),
        "avg_return_pct": round(pnl / n / capital * 100, 4),
        "exit_reasons": reasons,
        "avg_bars_held": round(float(np.mean([t.bars_held for t in trades])), 1),
    }


def verdict(s: dict, min_trades: int = 30) -> dict:
    """Plain-language call on whether the strategy works on this sample."""
    n = s.get("trades", 0)
    if n == 0:
        return {"level": "none", "label": "No trades", "reason": "The strategy never triggered in this period."}
    exp, pf, t = s["expectancy_r"], s["profit_factor"], s["t_stat"]
    pf_txt = "∞" if pf is None else f"{pf:.2f}"
    stats = f"expectancy {exp:+.3f}R per trade after costs, profit factor {pf_txt}, t-stat {t:.2f} over {n} trades"
    if n < min_trades:
        return {"level": "inconclusive", "label": "Inconclusive — too few trades",
                "reason": f"Only {n} trades (need at least {min_trades} to judge). {stats[0].upper() + stats[1:]}."}
    if exp > 0 and (pf is None or pf >= 1.1) and t >= 2:
        return {"level": "success", "label": "Successful",
                "reason": f"Positive and statistically significant edge: {stats}."}
    if exp > 0:
        return {"level": "marginal", "label": "Marginal — not proven",
                "reason": f"Slightly profitable, but the edge is too small or noisy to rely on: {stats}. "
                          "A t-stat of 2+ and profit factor of 1.1+ would be needed."}
    gross = s["gross_expectancy_r"]
    cost_note = (" It is positive before costs, so trading costs are what make it lose."
                 if gross > 0 else "")
    return {"level": "fail", "label": "Not successful",
            "reason": f"Loses money after costs: {stats}.{cost_note}"}


def run_backtest(strategy_key: str, tickers: list[str], params: dict, source: str = "yahoo",
                 data_fn: Callable[..., dict[str, pd.DataFrame]] | None = None) -> dict:
    s = BACKTESTS[strategy_key]
    tf = interval_of(s, params)
    requested = int(params.get("days", s.max_days))
    days = min(requested, dp.max_history_days(source, tf))
    data = (data_fn or fetch_candles)(tickers, interval=tf, days=days, source=source)
    capital = float(params.get("capital", 100000))

    all_trades: list[Trade] = []
    for t in tickers:
        df = data.get(t)
        if df is None or len(df) < 3:
            continue
        trades = s.simulate(df, params, t)
        _apply_costs(trades, float(params.get("cost_pct", 0)), capital)
        all_trades.extend(trades)
    all_trades, n_dropped = apply_daily_loss_limit(all_trades, float(params.get("daily_loss_pct", 0)), capital)

    per_stock = []
    for t in tickers:
        df = data.get(t)
        if df is None or len(df) < 3:
            per_stock.append({"ticker": t, "symbol": t.rsplit(".", 1)[0], "trades": 0, "no_data": True})
            continue
        trades = [x for x in all_trades if x.ticker == t]
        summ = summarize(trades, capital)
        per_stock.append({"ticker": t, "symbol": t.rsplit(".", 1)[0], "candles": len(df),
                          "from": df.index[0].strftime("%Y-%m-%d"), "to": df.index[-1].strftime("%Y-%m-%d"),
                          **summ, "verdict": verdict(summ)})

    overall = summarize(all_trades, capital)
    all_trades.sort(key=lambda x: x.exit_time)
    equity, running = [], 0.0
    for tr in all_trades:
        running += tr.net_r
        equity.append({"time": tr.exit_time, "r": round(running, 3)})

    loaded = [d for d in data.values() if d is not None and len(d)]
    return {
        "strategy": {"key": s.key, "name": s.name, "description": s.description, "rules": s.rules},
        "params": params,
        "period": {"from": min(d.index[0] for d in loaded).strftime("%Y-%m-%d") if loaded else None,
                   "to": max(d.index[-1] for d in loaded).strftime("%Y-%m-%d") if loaded else None,
                   "interval": tf, "interval_label": dp.TIMEFRAMES[tf]["label"], "intraday": dp.is_intraday(tf),
                   "source": source, "source_name": dp.PROVIDERS[source]["name"],
                   "days": days, "days_capped": days < requested},
        "overall": {**overall, "skipped_daily_loss": n_dropped}, "verdict": verdict(overall),
        "per_stock": per_stock, "equity": equity,
        "trades": [asdict(t) for t in all_trades],
        "missing": [t for t in tickers if t not in data],
    }


def candles_json(df: pd.DataFrame) -> dict:
    return {"time": [t.strftime("%Y-%m-%d %H:%M") for t in df.index],
            **{k.lower(): [round(float(v), 2) for v in df[k]] for k in ("Open", "High", "Low", "Close")}}


# --------------------------------------------------------------------------- #
# CLI:  python backtest_engine.py TCS INFY RELIANCE [--days 59] [--cost 0.03] [--rr 1]
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Run the 15-minute breakout backtest from the command line")
    ap.add_argument("tickers", nargs="+", help="NSE symbols, e.g. TCS INFY (.NS is added automatically)")
    ap.add_argument("--strategy", default="breakout_15m", choices=list(BACKTESTS))
    ap.add_argument("--days", type=int, default=59)
    ap.add_argument("--source", choices=list(dp.PROVIDERS), default="yahoo")
    ap.add_argument("--rr", type=float, default=None, help="reward:risk (default: the strategy's own)")
    ap.add_argument("--cost", type=float, default=0.03, help="cost %% per side")
    ap.add_argument("--overnight", action="store_true", help="allow holding overnight (no EOD square-off)")
    ap.add_argument("--target-first", action="store_true", help="if a candle hits stop and target, assume target")
    a = ap.parse_args()
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)

    syms = [t.upper() if "." in t else t.upper() + ".NS" for t in a.tickers]
    prm = {p.key: p.default for p in BACKTESTS[a.strategy].params}
    prm.update(days=a.days, cost_pct=a.cost, stop_first=not a.target_first)
    if a.rr is not None:
        prm["rr"] = a.rr
    if "square_off_eod" in prm:
        prm["square_off_eod"] = not a.overnight
    res = run_backtest(a.strategy, syms, prm, source=a.source)

    o, v = res["overall"], res["verdict"]
    print(f"\n{res['strategy']['name']}  |  {res['period']['source_name']}  |  "
          f"{res['period']['from']} -> {res['period']['to']}  |  {len(syms)} stocks")
    print("-" * 78)
    print(f"{'Symbol':<14}{'Trades':>7}{'Win %':>8}{'Exp R':>9}{'Total R':>9}{'PF':>7}  Verdict")
    for p in res["per_stock"]:
        if not p.get("trades"):
            print(f"{p['symbol']:<14}{'0':>7}  {'no data' if p.get('no_data') else 'no signals'}")
            continue
        pf = "inf" if p["profit_factor"] is None else f"{p['profit_factor']:.2f}"
        print(f"{p['symbol']:<14}{p['trades']:>7}{p['win_rate']:>8.1f}{p['expectancy_r']:>+9.3f}"
              f"{p['total_r']:>+9.1f}{pf:>7}  {p['verdict']['label']}")
    print("-" * 78)
    if o.get("trades"):
        print(f"ALL: {o['trades']} trades, win rate {o['win_rate']}% (break-even {o['breakeven_win_rate']}%), "
              f"expectancy {o['expectancy_r']:+.3f}R, total {o['total_r']:+.1f}R, net P&L Rs {o['net_pnl']:,.0f}")
    print(f"\nVERDICT: {v['label'].upper()}\n{v['reason']}\n")
