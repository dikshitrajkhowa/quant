"""
Strategy registry for the NSE stock screener.

Each strategy evaluates one stock's daily OHLCV DataFrame and returns:
    (extra_columns: dict, passed: bool)   or   None if there isn't enough data.

The app builds the dropdown, sidebar parameters, ranking, table and chart
overlays from the `Strategy` definitions in STRATEGIES - to add a new one,
write an evaluate function and register it at the bottom of this file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import numpy as np
import pandas as pd

YEAR = 252  # trading days in a year


# --------------------------------------------------------------------------- #
# Indicators
# --------------------------------------------------------------------------- #
def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    """Wilder's RSI."""
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = gain / loss.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(100)


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    line = ema(close, fast) - ema(close, slow)
    sig = line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return pd.DataFrame({"MACD": line, "Signal": sig, "Hist": line - sig})


def bollinger(close: pd.Series, n: int = 20, k: float = 2.0) -> pd.DataFrame:
    mid = sma(close, n)
    sd = close.rolling(n, min_periods=n).std()
    upper, lower = mid + k * sd, mid - k * sd
    return pd.DataFrame({"Mid": mid, "Upper": upper, "Lower": lower, "Bandwidth": (upper - lower) / mid * 100})


def pct_return(close: pd.Series, days: int) -> float:
    if len(close) <= days:
        return np.nan
    return (float(close.iloc[-1]) / float(close.iloc[-days - 1]) - 1) * 100


def days_since_cross_up(fast: pd.Series, slow: pd.Series, within: int) -> int | None:
    """Sessions since `fast` crossed above `slow` (0 = today), if it happened within `within` sessions
    and fast is still above slow."""
    above = (fast > slow).dropna()
    if len(above) < within + 2 or not above.iloc[-1]:
        return None
    tail = above.iloc[-(within + 2):].to_numpy()
    for i in range(len(tail) - 1, 0, -1):
        if tail[i] and not tail[i - 1]:
            return len(tail) - 1 - i
    return None


# --------------------------------------------------------------------------- #
# Base metrics shared by every strategy (52-week window = last 252 sessions)
# --------------------------------------------------------------------------- #
def base_metrics(df: pd.DataFrame) -> dict:
    yr = df.tail(YEAR)
    close = df["Close"]
    current = float(close.iloc[-1])
    high, low = float(yr["High"].max()), float(yr["Low"].min())
    return {
        "Current Price": round(current, 2),
        "52W High": round(high, 2),
        "52W High Date": pd.Timestamp(yr["High"].idxmax()).date(),
        "% From 52W High": round((high - current) / high * 100, 2) if high > 0 else np.nan,
        "52W Low": round(low, 2),
        "% Above 52W Low": round((current / low - 1) * 100, 2) if low > 0 else np.nan,
        "1M Return %": round(pct_return(close, 21), 2),
        "Avg Vol (20D)": int(df["Volume"].tail(20).mean()) if "Volume" in df else None,
        "Last Date": pd.Timestamp(df.index[-1]).date(),
    }


# --------------------------------------------------------------------------- #
# Strategy definition
# --------------------------------------------------------------------------- #
@dataclass
class Param:
    key: str
    label: str
    kind: str  # "float" | "int" | "bool"
    default: float | int | bool
    min: float | int | None = None
    max: float | int | None = None
    step: float | int | None = None
    help: str | None = None


@dataclass
class Strategy:
    key: str
    name: str
    description: str
    evaluate: Callable[[pd.DataFrame, dict], tuple[dict, bool] | None]
    score_col: str                 # column used for ranking
    ascending: bool                # True = lower score ranks first
    score_fmt: str = "{:.2f}"      # display format for bar labels
    params: list[Param] = field(default_factory=list)
    min_rows: int = 60             # history needed for the indicators
    overlays: list[str] = field(default_factory=lambda: ["sma50"])  # sma20/50/150/200, bb
    panel: str = "volume"          # lower chart panel: volume | rsi | macd
    levels: Callable[[pd.Series, dict], list[dict]] | None = None   # horizontal lines/bands on price charts
    postprocess: Callable[[pd.DataFrame, dict], pd.DataFrame] | None = None  # cross-sectional step


# --------------------------------------------------------------------------- #
# Strategy implementations
# --------------------------------------------------------------------------- #
# 1. Near 52-week high -------------------------------------------------------
def _near_high(df, p):
    yr = df.tail(YEAR)
    hs = yr["Close"] if p["use_close"] else yr["High"]
    high, cur = float(hs.max()), float(df["Close"].iloc[-1])
    pct = (high - cur) / high * 100
    return {"52W High": round(high, 2), "52W High Date": pd.Timestamp(hs.idxmax()).date(),
            "% From 52W High": round(pct, 2)}, pct <= p["threshold"]


def _near_high_levels(row, p):
    h = row["52W High"]
    return [{"y": h, "label": "52W high", "y0": h * (1 - p["threshold"] / 100)}]


# 2. 52-week high breakout with volume ---------------------------------------
def _breakout(df, p):
    n = int(p["lookback"])
    if len(df) < YEAR // 2:
        return None
    prior = df.iloc[:-n].tail(YEAR - n)
    prior_high = float(prior["High"].max())
    recent = df.tail(n)
    cur = float(df["Close"].iloc[-1])
    avg_vol = float(df["Volume"].iloc[:-n].tail(50).mean())
    vol_ratio = float(recent["Volume"].max()) / avg_vol if avg_vol > 0 else np.nan
    breakout_pct = (cur / prior_high - 1) * 100
    passed = cur > prior_high and vol_ratio >= p["vol_mult"]
    return {"Prior 52W High": round(prior_high, 2), "Breakout %": round(breakout_pct, 2),
            "Volume Ratio": round(vol_ratio, 2)}, passed


# 3. Golden cross ------------------------------------------------------------
def _golden_cross(df, p):
    c = df["Close"]
    s50, s200 = sma(c, 50), sma(c, 200)
    if np.isnan(s200.iloc[-1]):
        return None
    d = days_since_cross_up(s50, s200, int(p["within"]))
    spread = (s50.iloc[-1] / s200.iloc[-1] - 1) * 100
    return {"Days Since Cross": d if d is not None else np.nan, "50/200 Spread %": round(spread, 2)}, d is not None


# 4. RSI oversold pullback ---------------------------------------------------
def _rsi_oversold(df, p):
    c = df["Close"]
    r = float(rsi(c).iloc[-1])
    s200 = sma(c, 200).iloc[-1]
    in_uptrend = bool(c.iloc[-1] > s200) if not np.isnan(s200) else False
    passed = r <= p["rsi_max"] and (in_uptrend or not p["uptrend"])
    return {"RSI(14)": round(r, 1), "Above 200 DMA": in_uptrend}, passed


# 5. Minervini trend template ------------------------------------------------
def _trend_template(df, p):
    c = df["Close"]
    s50, s150, s200 = sma(c, 50), sma(c, 150), sma(c, 200)
    if len(s200.dropna()) < 22:
        return None
    cur = float(c.iloc[-1])
    yr = df.tail(YEAR)
    high, low = float(yr["High"].max()), float(yr["Low"].min())
    checks = [
        cur > s150.iloc[-1] and cur > s200.iloc[-1],
        s150.iloc[-1] > s200.iloc[-1],
        s200.iloc[-1] > s200.iloc[-22],                     # 200 DMA rising for ~1 month
        s50.iloc[-1] > s150.iloc[-1] and s50.iloc[-1] > s200.iloc[-1],
        cur > s50.iloc[-1],
        cur >= low * (1 + p["min_above_low"] / 100),
        cur >= high * (1 - p["max_from_high"] / 100),
    ]
    return {"6M Return %": round(pct_return(c, 126), 2), "Criteria Met": int(sum(checks))}, all(checks)


# 6. Volume spike ------------------------------------------------------------
def _volume_spike(df, p):
    vol = df["Volume"]
    avg = float(vol.iloc[:-1].tail(20).mean())
    if avg <= 0:
        return None
    ratio = float(vol.iloc[-1]) / avg
    chg = (float(df["Close"].iloc[-1]) / float(df["Close"].iloc[-2]) - 1) * 100
    return {"Volume Ratio": round(ratio, 2), "Day Change %": round(chg, 2)}, ratio >= p["vol_mult"] and chg >= p["min_change"]


# 7. Bollinger squeeze -------------------------------------------------------
def _bb_squeeze(df, p):
    bb = bollinger(df["Close"])
    bw = bb["Bandwidth"].dropna().tail(int(p["lookback"]))
    if len(bw) < int(p["lookback"]) // 2:
        return None
    pctile = float((bw <= bw.iloc[-1]).mean() * 100)
    return {"Bandwidth %": round(float(bw.iloc[-1]), 2), "Bandwidth Percentile": round(pctile, 1)}, pctile <= p["pct_max"]


# 8. Relative-strength momentum (cross-sectional) -----------------------------
def _momentum(df, p):
    c = df["Close"]
    r3, r6, r9, r12 = (pct_return(c, d) for d in (63, 126, 189, 252))
    if np.isnan(r3) or np.isnan(r6):
        return None
    parts = [(r3, 0.4), (r6, 0.2), (r9, 0.2), (r12, 0.2)]
    parts = [(r, w) for r, w in parts if not np.isnan(r)]
    score = sum(r * w for r, w in parts) / sum(w for _, w in parts)
    s50 = sma(c, 50).iloc[-1]
    above50 = bool(c.iloc[-1] > s50) if not np.isnan(s50) else False
    return {"RS Score": round(score, 2), "3M Return %": round(r3, 2), "6M Return %": round(r6, 2),
            "Above 50 DMA": above50}, (above50 or not p["above_50dma"])


def _momentum_post(all_df, p):
    all_df["RS Rank"] = (all_df["RS Score"].rank(pct=True) * 100).round(1)
    all_df["Passed"] = all_df["Passed"] & (all_df["RS Rank"] >= 100 - p["top_pct"])
    return all_df


# 9. MACD bullish crossover ---------------------------------------------------
def _macd_cross(df, p):
    m = macd(df["Close"])
    if m["Signal"].isna().iloc[-1]:
        return None
    d = days_since_cross_up(m["MACD"], m["Signal"], int(p["within"]))
    below_zero = bool(m["MACD"].iloc[-1] < 0)
    passed = d is not None and (below_zero or not p["below_zero"])
    return {"Days Since Cross": d if d is not None else np.nan, "MACD": round(float(m["MACD"].iloc[-1]), 2),
            "MACD Hist": round(float(m["Hist"].iloc[-1]), 2)}, passed


# 10. Near 52-week low --------------------------------------------------------
def _near_low(df, p):
    yr = df.tail(YEAR)
    low, cur = float(yr["Low"].min()), float(df["Close"].iloc[-1])
    pct = (cur / low - 1) * 100
    return {"% Above 52W Low": round(pct, 2), "52W Low Date": pd.Timestamp(yr["Low"].idxmin()).date(),
            "RSI(14)": round(float(rsi(df["Close"]).iloc[-1]), 1)}, pct <= p["threshold"]


def _near_low_levels(row, p):
    lo = row["52W Low"]
    return [{"y": lo, "label": "52W low", "y0": lo * (1 + p["threshold"] / 100)}]


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #
STRATEGIES: dict[str, Strategy] = {s.key: s for s in [
    Strategy(
        key="near_52w_high", name="Near 52-Week High",
        description="Momentum: stocks trading within X% of their 52-week high, closest first.",
        evaluate=_near_high, score_col="% From 52W High", ascending=True, score_fmt="{:.2f}%",
        params=[Param("threshold", "Max % below 52W high", "float", 5.0, 0.5, 20.0, 0.5),
                Param("use_close", "Use closing prices for 52W high", "bool", False,
                      help="Off = highest intraday High; On = highest Close")],
        overlays=["sma20", "sma50"], levels=_near_high_levels,
    ),
    Strategy(
        key="breakout_52w", name="52-Week High Breakout (volume)",
        description="Close above the prior 52-week high within the last N sessions, on volume at least "
                    "K× the 50-day average. Ranked by volume surge.",
        evaluate=_breakout, score_col="Volume Ratio", ascending=False, score_fmt="{:.2f}×",
        params=[Param("lookback", "Breakout within last N sessions", "int", 3, 1, 10, 1),
                Param("vol_mult", "Min volume vs 50D avg (×)", "float", 1.5, 1.0, 5.0, 0.1)],
        overlays=["sma20", "sma50"],
        levels=lambda r, p: [{"y": r["Prior 52W High"], "label": "Prior 52W high"}],
    ),
    Strategy(
        key="golden_cross", name="Golden Cross (50/200 DMA)",
        description="50-day moving average crossed above the 200-day within the last N sessions. "
                    "Most recent crosses first.",
        evaluate=_golden_cross, score_col="Days Since Cross", ascending=True, score_fmt="{:.0f}d",
        params=[Param("within", "Cross within last N sessions", "int", 10, 1, 60, 1)],
        min_rows=215, overlays=["sma50", "sma200"],
    ),
    Strategy(
        key="rsi_oversold", name="RSI Oversold Pullback",
        description="Mean reversion: RSI(14) at or below the level, optionally only in stocks still above "
                    "their 200 DMA (pullback in an uptrend). Most oversold first.",
        evaluate=_rsi_oversold, score_col="RSI(14)", ascending=True, score_fmt="{:.1f}",
        params=[Param("rsi_max", "Max RSI(14)", "float", 30.0, 10.0, 50.0, 1.0),
                Param("uptrend", "Only stocks above 200 DMA", "bool", True)],
        min_rows=60, overlays=["sma50", "sma200"], panel="rsi",
    ),
    Strategy(
        key="trend_template", name="Minervini Trend Template",
        description="Stage-2 uptrend: price > 50 > 150 > 200 DMA, 200 DMA rising, well off the 52W low and "
                    "near the 52W high. Ranked by 6-month return.",
        evaluate=_trend_template, score_col="6M Return %", ascending=False, score_fmt="{:.1f}%",
        params=[Param("min_above_low", "Min % above 52W low", "float", 30.0, 0.0, 200.0, 5.0),
                Param("max_from_high", "Max % below 52W high", "float", 25.0, 1.0, 50.0, 1.0)],
        min_rows=225, overlays=["sma50", "sma150", "sma200"],
    ),
    Strategy(
        key="volume_spike", name="Volume Spike + Price Up",
        description="Today's volume at least K× the 20-day average with a positive close - "
                    "signs of institutional buying. Ranked by volume surge.",
        evaluate=_volume_spike, score_col="Volume Ratio", ascending=False, score_fmt="{:.2f}×",
        params=[Param("vol_mult", "Min volume vs 20D avg (×)", "float", 2.5, 1.0, 10.0, 0.5),
                Param("min_change", "Min day change %", "float", 2.0, -5.0, 10.0, 0.5)],
        min_rows=25, overlays=["sma20", "sma50"],
    ),
    Strategy(
        key="bb_squeeze", name="Bollinger Band Squeeze",
        description="Volatility contraction: Bollinger bandwidth in the lowest X% of its recent range - "
                    "often precedes a large move. Tightest first.",
        evaluate=_bb_squeeze, score_col="Bandwidth Percentile", ascending=True, score_fmt="P{:.0f}",
        params=[Param("pct_max", "Max bandwidth percentile", "float", 10.0, 1.0, 50.0, 1.0),
                Param("lookback", "Lookback (sessions)", "int", 120, 40, 250, 10)],
        min_rows=80, overlays=["bb"],
    ),
    Strategy(
        key="momentum", name="Relative Strength Momentum",
        description="Top X% of the universe by weighted 3/6/9/12-month return (40/20/20/20). "
                    "Strongest first.",
        evaluate=_momentum, score_col="RS Score", ascending=False, score_fmt="{:.1f}",
        params=[Param("top_pct", "Top % of universe", "float", 10.0, 1.0, 50.0, 1.0),
                Param("above_50dma", "Must be above 50 DMA", "bool", True)],
        min_rows=130, overlays=["sma50", "sma200"], postprocess=_momentum_post,
    ),
    Strategy(
        key="macd_cross", name="MACD Bullish Crossover",
        description="MACD line crossed above its signal line within the last N sessions; optionally only "
                    "crosses below the zero line (early reversal). Most recent first.",
        evaluate=_macd_cross, score_col="Days Since Cross", ascending=True, score_fmt="{:.0f}d",
        params=[Param("within", "Cross within last N sessions", "int", 3, 1, 20, 1),
                Param("below_zero", "Only crosses below zero line", "bool", False)],
        min_rows=40, overlays=["sma50"], panel="macd",
    ),
    Strategy(
        key="near_52w_low", name="Near 52-Week Low",
        description="Contrarian / value watch-list: stocks within X% of their 52-week low. Closest first.",
        evaluate=_near_low, score_col="% Above 52W Low", ascending=True, score_fmt="{:.2f}%",
        params=[Param("threshold", "Max % above 52W low", "float", 5.0, 0.5, 20.0, 0.5)],
        overlays=["sma50", "sma200"], panel="rsi", levels=_near_low_levels,
    ),
]}


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #
def run_strategy(
    history: dict[str, pd.DataFrame],
    strategy: Strategy,
    params: dict,
    min_history_days: int = 200,
) -> pd.DataFrame:
    """Evaluate every stock. Returns all evaluated rows with a boolean `Passed` column."""
    rows = []
    need = max(int(min_history_days), strategy.min_rows)
    for ticker, df in history.items():
        if len(df) < need:
            continue
        try:
            out = strategy.evaluate(df, params)
        except Exception:
            continue
        if out is None:
            continue
        extra, passed = out
        row = {"Ticker": ticker, "Symbol": ticker.removesuffix(".NS"), **base_metrics(df), **extra,
               "Passed": bool(passed)}
        rows.append(row)

    all_df = pd.DataFrame(rows)
    if all_df.empty:
        return all_df
    if strategy.postprocess:
        all_df = strategy.postprocess(all_df, params)
    return all_df


def rank_matches(all_df: pd.DataFrame, strategy: Strategy, min_price: float = 0, min_avg_volume: float = 0) -> pd.DataFrame:
    if all_df.empty:
        return all_df
    m = all_df["Passed"].copy()
    if min_price > 0:
        m &= all_df["Current Price"] >= min_price
    if min_avg_volume > 0:
        m &= all_df["Avg Vol (20D)"].fillna(0) >= min_avg_volume
    res = all_df[m].sort_values(strategy.score_col, ascending=strategy.ascending, na_position="last")
    res = res.reset_index(drop=True)
    res.index = res.index + 1
    res.index.name = "Rank"
    return res


def overlay_series(df: pd.DataFrame, name: str) -> dict[str, pd.Series]:
    """Indicator lines for chart overlays."""
    c = df["Close"]
    if name.startswith("sma"):
        n = int(name[3:])
        return {f"{n} DMA": sma(c, n)}
    if name == "bb":
        bb = bollinger(c)
        return {"BB upper": bb["Upper"], "BB mid": bb["Mid"], "BB lower": bb["Lower"]}
    return {}
