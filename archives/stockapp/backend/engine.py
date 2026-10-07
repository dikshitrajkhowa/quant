"""
Core backtest engine: fetches price data, runs the vectorized backtest,
computes performance metrics, and packages series data as plain JSON
(the frontend renders it with Chart.js so values show on hover).
"""

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


def build_chart_data(data: pd.DataFrame, indicator_cols: list[str]) -> dict:
    """
    Package everything the frontend needs to draw interactive, hoverable
    charts with Chart.js: date labels, price + indicator series, and the
    strategy vs. benchmark equity curves.
    """
    dates = [d.strftime("%Y-%m-%d") for d in data.index]

    def series(col):
        return [None if pd.isna(v) else round(float(v), 2) for v in data[col]]

    return {
        "dates": dates,
        "close": series("Close"),
        "indicators": {col: series(col) for col in indicator_cols if col in data.columns},
        "equity": series("equity"),
        "benchmark_equity": series("benchmark_equity"),
    }
