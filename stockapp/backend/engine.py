"""
Core backtest engine: fetches price data, runs the vectorized backtest,
computes performance metrics, and renders a chart to a base64 PNG string
(so the frontend can display it with a plain <img> tag - no chart JS lib needed).
"""

import base64
import io

import matplotlib
matplotlib.use("Agg")  # headless rendering, no display needed
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import yfinance as yf


def fetch_price_data(ticker: str, start: str, end: str | None = None) -> pd.DataFrame:
    df = yf.download(ticker, start=start, end=end, auto_adjust=True, progress=False)

    if df.empty:
        raise ValueError(
            f"No data returned for '{ticker}'. Check the symbol and suffix "
            f"(.NS for NSE, .BO for BSE)."
        )

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    return df[["Open", "High", "Low", "Close", "Volume"]].dropna()


def run_backtest(df: pd.DataFrame, initial_capital: float = 100_000.0,
                  commission_bps: float = 5.0) -> dict:
    data = df.copy()
    data["daily_return"] = data["Close"].pct_change().fillna(0)
    data["strategy_return"] = data["position"] * data["daily_return"]

    trade_change = data["position"].diff().abs().fillna(0)
    cost = trade_change * (commission_bps / 10_000.0)
    data["strategy_return_net"] = data["strategy_return"] - cost

    data["equity"] = initial_capital * (1 + data["strategy_return_net"]).cumprod()
    data["benchmark_equity"] = initial_capital * (1 + data["daily_return"]).cumprod()

    trades = int(trade_change.sum() / 2)

    metrics = _compute_metrics(data["strategy_return_net"], data["equity"])
    bench_metrics = _compute_metrics(data["daily_return"], data["benchmark_equity"])
    metrics["benchmark_CAGR_pct"] = bench_metrics["CAGR_pct"]
    metrics["benchmark_max_drawdown_pct"] = bench_metrics["max_drawdown_pct"]
    metrics["trades"] = trades

    return {"data": data, "metrics": metrics}


def _compute_metrics(returns: pd.Series, equity: pd.Series, periods_per_year: int = 252) -> dict:
    total_days = len(returns)
    if total_days == 0 or equity.iloc[0] == 0:
        return {"total_return_pct": 0, "CAGR_pct": 0, "sharpe": 0, "max_drawdown_pct": 0}

    total_return = equity.iloc[-1] / equity.iloc[0] - 1
    years = total_days / periods_per_year
    cagr = (equity.iloc[-1] / equity.iloc[0]) ** (1 / years) - 1 if years > 0 else 0

    ann_vol = returns.std() * np.sqrt(periods_per_year)
    sharpe = (returns.mean() * periods_per_year) / ann_vol if ann_vol > 0 else 0

    running_max = equity.cummax()
    drawdown = (equity - running_max) / running_max
    max_dd = drawdown.min()

    return {
        "total_return_pct": round(float(total_return) * 100, 2),
        "CAGR_pct": round(float(cagr) * 100, 2),
        "sharpe": round(float(sharpe), 2),
        "max_drawdown_pct": round(float(max_dd) * 100, 2),
    }


def decide_action(data: pd.DataFrame) -> str:
    """Translate the latest signal into a plain-English decision."""
    last_signal = data["signal"].iloc[-1]
    prev_signal = data["signal"].iloc[-2] if len(data) > 1 else last_signal

    if last_signal == 1 and prev_signal == 0:
        return "BUY"
    if last_signal == 0 and prev_signal == 1:
        return "SELL"
    if last_signal == 1:
        return "HOLD"
    return "STAY OUT"


def render_chart(data: pd.DataFrame, ticker: str, strategy_label: str,
                  indicator_cols: list[str]) -> str:
    """Render price+indicators and equity curve, return as base64-encoded PNG."""
    fig, axes = plt.subplots(2, 1, figsize=(10, 7), sharex=True,
                              gridspec_kw={"height_ratios": [2, 1]})

    axes[0].plot(data.index, data["Close"], label="Close", color="#4C9AFF", linewidth=1.2)
    palette = ["#F2994A", "#27AE60", "#BB6BD9"]
    for i, col in enumerate(indicator_cols):
        if col in data.columns:
            axes[0].plot(data.index, data[col], label=col, color=palette[i % len(palette)], linewidth=1)
    axes[0].set_title(f"{ticker} — {strategy_label}", fontsize=12, fontweight="bold")
    axes[0].legend(loc="upper left", fontsize=8)
    axes[0].grid(alpha=0.25)

    axes[1].plot(data.index, data["equity"], label="Strategy", color="#27AE60", linewidth=1.4)
    axes[1].plot(data.index, data["benchmark_equity"], label="Buy & Hold", color="#888888",
                 linestyle="--", linewidth=1.2)
    axes[1].set_title("Equity Curve", fontsize=11)
    axes[1].legend(loc="upper left", fontsize=8)
    axes[1].grid(alpha=0.25)

    plt.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=140)
    plt.close(fig)
    buf.seek(0)
    return base64.b64encode(buf.read()).decode("utf-8")
