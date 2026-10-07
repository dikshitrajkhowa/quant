"""
Strategy definitions.

Each strategy function takes a price DataFrame (columns: Open, High, Low, Close, Volume)
and a params dict, and returns:
  - the DataFrame with extra indicator columns + a 'position' column (1 = long, 0 = flat)
  - a human-readable one-line explanation of the current (latest) signal

Add a new strategy by writing a function with this signature and registering it
in STRATEGY_REGISTRY at the bottom.
"""

import numpy as np
import pandas as pd


def sma_crossover(df: pd.DataFrame, params: dict):
    fast = int(params.get("fast", 20))
    slow = int(params.get("slow", 50))

    out = df.copy()
    out["sma_fast"] = out["Close"].rolling(fast).mean()
    out["sma_slow"] = out["Close"].rolling(slow).mean()
    out["signal"] = np.where(out["sma_fast"] > out["sma_slow"], 1, 0)
    out["position"] = out["signal"].shift(1).fillna(0)

    last = out.iloc[-1]
    explanation = (
        f"SMA({fast}) = {last['sma_fast']:.2f} is "
        f"{'above' if last['sma_fast'] > last['sma_slow'] else 'below'} "
        f"SMA({slow}) = {last['sma_slow']:.2f}"
    )
    return out, explanation


def rsi_mean_reversion(df: pd.DataFrame, params: dict):
    period = int(params.get("period", 14))
    oversold = float(params.get("oversold", 30))
    overbought = float(params.get("overbought", 70))

    out = df.copy()
    delta = out["Close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out["rsi"] = 100 - (100 / (1 + rs))
    out["rsi"] = out["rsi"].fillna(50)

    # Enter long when RSI dips below `oversold`, hold until RSI rises above `overbought`
    position = np.zeros(len(out))
    in_position = False
    rsi_vals = out["rsi"].values
    for i in range(len(out)):
        if not in_position and rsi_vals[i] < oversold:
            in_position = True
        elif in_position and rsi_vals[i] > overbought:
            in_position = False
        position[i] = 1 if in_position else 0

    out["signal"] = position
    out["position"] = out["signal"].shift(1).fillna(0)

    last = out.iloc[-1]
    explanation = (
        f"RSI({period}) = {last['rsi']:.1f} "
        f"(oversold < {oversold:.0f}, overbought > {overbought:.0f})"
    )
    return out, explanation


def macd_crossover(df: pd.DataFrame, params: dict):
    fast = int(params.get("fast", 12))
    slow = int(params.get("slow", 26))
    signal_span = int(params.get("signal", 9))

    out = df.copy()
    ema_fast = out["Close"].ewm(span=fast, adjust=False).mean()
    ema_slow = out["Close"].ewm(span=slow, adjust=False).mean()
    out["macd"] = ema_fast - ema_slow
    out["macd_signal"] = out["macd"].ewm(span=signal_span, adjust=False).mean()
    out["signal"] = np.where(out["macd"] > out["macd_signal"], 1, 0)
    out["position"] = out["signal"].shift(1).fillna(0)

    last = out.iloc[-1]
    explanation = (
        f"MACD = {last['macd']:.2f} is "
        f"{'above' if last['macd'] > last['macd_signal'] else 'below'} "
        f"Signal = {last['macd_signal']:.2f}"
    )
    return out, explanation


STRATEGY_REGISTRY = {
    "sma_crossover": {
        "label": "SMA Crossover",
        "description": "Long when a fast moving average is above a slow moving average.",
        "func": sma_crossover,
        "params": [
            {"name": "fast", "label": "Fast window", "type": "number", "default": 20},
            {"name": "slow", "label": "Slow window", "type": "number", "default": 50},
        ],
    },
    "rsi_mean_reversion": {
        "label": "RSI Mean Reversion",
        "description": "Buy when RSI signals oversold, exit when it signals overbought.",
        "func": rsi_mean_reversion,
        "params": [
            {"name": "period", "label": "RSI period", "type": "number", "default": 14},
            {"name": "oversold", "label": "Oversold level", "type": "number", "default": 30},
            {"name": "overbought", "label": "Overbought level", "type": "number", "default": 70},
        ],
    },
    "macd_crossover": {
        "label": "MACD Crossover",
        "description": "Long when the MACD line is above its signal line.",
        "func": macd_crossover,
        "params": [
            {"name": "fast", "label": "Fast EMA", "type": "number", "default": 12},
            {"name": "slow", "label": "Slow EMA", "type": "number", "default": 26},
            {"name": "signal", "label": "Signal EMA", "type": "number", "default": 9},
        ],
    },
}
