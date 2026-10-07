"""
52-Week High Momentum Screener (NSE)
====================================

Finds NSE equities trading close to their 52-week high.

Steps
-----
1. Data collection : fetch the full NSE equity list (EQUITY_L.csv) and ~1 year
                     of daily OHLCV for each stock via yfinance.
2. Screening       : for each stock compute
                       - 52-week high   (max of daily High over the last year)
                       - current price  (latest Close)
                       - % below high   ((high - current) / high * 100)
                     and keep stocks within `--threshold` % (default 5) of the high.
3. Ranking         : sort by % below high, closest first.
4. Analysis        : print the top `--top` stocks (default 20), save a CSV, and
                     plot recent price action for each with its 52-week high.

Usage
-----
    python near_52w_high.py                          # all NSE EQ stocks, 5%, top 20
    python near_52w_high.py --threshold 3 --top 10
    python near_52w_high.py --universe nifty500 --min-price 50 --min-avg-volume 100000
    python near_52w_high.py --use-close              # 52w high from closing prices
    python near_52w_high.py --no-charts

Requires: pandas, numpy, requests, yfinance, matplotlib
"""

from __future__ import annotations

import argparse
import io
import logging
import sys
import time
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import requests
import yfinance as yf

log = logging.getLogger("52w_screener")

BASE_DIR = Path(__file__).resolve().parent
CACHE_DIR = BASE_DIR / ".cache"
OUTPUT_DIR = BASE_DIR / "output"

NSE_EQUITY_LIST_URL = "https://archives.nseindia.com/content/equities/EQUITY_L.csv"
NSE_INDEX_URLS = {
    "nifty50": "https://archives.nseindia.com/content/indices/ind_nifty50list.csv",
    "nifty100": "https://archives.nseindia.com/content/indices/ind_nifty100list.csv",
    "nifty500": "https://archives.nseindia.com/content/indices/ind_nifty500list.csv",
}
HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/csv,application/octet-stream,*/*",
}


# --------------------------------------------------------------------------- #
# 1. Data collection
# --------------------------------------------------------------------------- #
def _fetch_csv_cached(url: str, cache_name: str) -> pd.DataFrame:
    """Download a CSV from NSE, cached for the day. Falls back to latest cache."""
    CACHE_DIR.mkdir(exist_ok=True)
    today_file = CACHE_DIR / f"{cache_name}_{date.today():%Y%m%d}.csv"
    if today_file.exists():
        return pd.read_csv(today_file)

    try:
        resp = requests.get(url, headers=HTTP_HEADERS, timeout=30)
        resp.raise_for_status()
        df = pd.read_csv(io.StringIO(resp.text))
        df.to_csv(today_file, index=False)
        return df
    except Exception as exc:  # network / NSE blocking
        cached = sorted(CACHE_DIR.glob(f"{cache_name}_*.csv"))
        if cached:
            log.warning("Could not download %s (%s); using cache %s", url, exc, cached[-1].name)
            return pd.read_csv(cached[-1])
        raise RuntimeError(f"Failed to fetch {url} and no cache available") from exc


def get_nse_tickers(universe: str = "all") -> list[str]:
    """Return yfinance tickers (SYMBOL.NS) for the chosen universe."""
    if universe == "all":
        df = _fetch_csv_cached(NSE_EQUITY_LIST_URL, "EQUITY_L")
        df.columns = [c.strip().upper() for c in df.columns]
        df = df[df["SERIES"].str.strip() == "EQ"]  # regular equity segment only
    else:
        df = _fetch_csv_cached(NSE_INDEX_URLS[universe], f"ind_{universe}")
        df.columns = [c.strip().upper() for c in df.columns]

    symbols = df["SYMBOL"].astype(str).str.strip().unique()
    tickers = sorted(f"{s}.NS" for s in symbols if s)
    log.info("Universe '%s': %d tickers", universe, len(tickers))
    return tickers


def download_history(
    tickers: list[str],
    period: str = "1y",
    batch_size: int = 100,
    pause: float = 1.0,
    progress_callback=None,
) -> dict[str, pd.DataFrame]:
    """Download daily OHLCV in batches. Returns {ticker: DataFrame}.

    progress_callback(done_batches, total_batches) is called after each batch.
    """
    data: dict[str, pd.DataFrame] = {}
    batches = [tickers[i : i + batch_size] for i in range(0, len(tickers), batch_size)]

    for n, batch in enumerate(batches, 1):
        log.info("Downloading batch %d/%d (%d tickers)", n, len(batches), len(batch))
        try:
            raw = yf.download(
                batch,
                period=period,
                interval="1d",
                group_by="ticker",
                auto_adjust=True,
                threads=True,
                progress=False,
            )
        except Exception as exc:
            log.warning("Batch %d failed: %s", n, exc)
            if progress_callback:
                progress_callback(n, len(batches))
            continue

        if raw is None or raw.empty:
            if progress_callback:
                progress_callback(n, len(batches))
            continue

        for t in batch:
            try:
                if isinstance(raw.columns, pd.MultiIndex):
                    if t not in raw.columns.get_level_values(0):
                        continue
                    df = raw[t]
                else:  # single-ticker, flat columns (older yfinance)
                    df = raw
                df = df.dropna(subset=["Close"])
                if not df.empty:
                    data[t] = df
            except Exception:
                continue

        if progress_callback:
            progress_callback(n, len(batches))
        if n < len(batches):
            time.sleep(pause)  # be polite to Yahoo

    log.info("Got price history for %d / %d tickers", len(data), len(tickers))
    return data


# --------------------------------------------------------------------------- #
# 2 & 3. Screening and ranking
# --------------------------------------------------------------------------- #
def compute_metrics(
    history: dict[str, pd.DataFrame],
    use_close: bool = False,
    min_history_days: int = 200,
) -> pd.DataFrame:
    """52-week-high metrics for every stock with enough history (unfiltered)."""
    rows = []
    for ticker, df in history.items():
        if len(df) < min_history_days:
            continue  # skip recent listings: their "52w high" isn't a real 52w high

        high_series = df["Close"] if use_close else df["High"]
        high_52w = float(high_series.max())
        high_date = high_series.idxmax()
        current = float(df["Close"].iloc[-1])
        if not np.isfinite(high_52w) or high_52w <= 0 or not np.isfinite(current):
            continue

        pct_from_high = (high_52w - current) / high_52w * 100
        avg_vol_20 = float(df["Volume"].tail(20).mean()) if "Volume" in df else np.nan
        ret_1m = (current / float(df["Close"].iloc[-22]) - 1) * 100 if len(df) > 22 else np.nan
        low_52w = float(df["Low"].min()) if "Low" in df else float(df["Close"].min())

        rows.append(
            {
                "Ticker": ticker,
                "Symbol": ticker.removesuffix(".NS"),
                "Current Price": round(current, 2),
                "52W High": round(high_52w, 2),
                "52W High Date": pd.Timestamp(high_date).date(),
                "% From 52W High": round(pct_from_high, 2),
                "52W Low": round(low_52w, 2),
                "% Above 52W Low": round((current / low_52w - 1) * 100, 2) if low_52w > 0 else np.nan,
                "1M Return %": round(ret_1m, 2),
                "Avg Vol (20D)": int(avg_vol_20) if np.isfinite(avg_vol_20) else None,
                "Last Date": pd.Timestamp(df.index[-1]).date(),
            }
        )

    return pd.DataFrame(rows)


def screen_near_52w_high(
    history: dict[str, pd.DataFrame],
    threshold_pct: float = 5.0,
    use_close: bool = False,
    min_history_days: int = 200,
    min_price: float = 0.0,
    min_avg_volume: float = 0.0,
    metrics: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Return stocks within `threshold_pct` of their 52-week high, ranked closest first."""
    all_df = metrics if metrics is not None else compute_metrics(history, use_close, min_history_days)
    if all_df.empty:
        return all_df

    mask = all_df["% From 52W High"] <= threshold_pct
    if min_price > 0:
        mask &= all_df["Current Price"] >= min_price
    if min_avg_volume > 0:
        mask &= all_df["Avg Vol (20D)"].fillna(0) >= min_avg_volume

    result = (
        all_df[mask]
        .sort_values(["% From 52W High", "1M Return %"], ascending=[True, False])
        .reset_index(drop=True)
    )
    result.index = result.index + 1
    result.index.name = "Rank"
    log.info("%d of %d stocks are within %.1f%% of their 52W high", len(result), len(all_df), threshold_pct)
    return result


# --------------------------------------------------------------------------- #
# 4. Analysis / charts
# --------------------------------------------------------------------------- #
def plot_top_stocks(
    result: pd.DataFrame,
    history: dict[str, pd.DataFrame],
    chart_days: int = 90,
    threshold_pct: float = 5.0,
    save_path: Path | None = None,
    show: bool = True,
) -> None:
    import matplotlib

    if not show:
        matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates

    n = len(result)
    if n == 0:
        return
    cols = 4 if n > 6 else min(n, 3)
    rows = int(np.ceil(n / cols))
    fig, axes = plt.subplots(rows, cols, figsize=(4.6 * cols, 3.0 * rows), squeeze=False)

    for ax, (rank, r) in zip(axes.flat, result.iterrows()):
        df = history[r["Ticker"]].tail(chart_days)
        high = r["52W High"]
        ax.plot(df.index, df["Close"], color="#2a6fdb", lw=1.4)
        ax.axhline(high, color="#d1495b", ls="--", lw=1, label="52W high")
        ax.axhspan(high * (1 - threshold_pct / 100), high, color="#d1495b", alpha=0.08)
        ax.set_title(f"#{rank} {r['Symbol']}  ({r['% From 52W High']:.2f}% off high)", fontsize=9)
        ax.tick_params(labelsize=7)
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%d-%b"))
        ax.xaxis.set_major_locator(mdates.MonthLocator())
        ax.grid(alpha=0.25)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)

    for ax in list(axes.flat)[n:]:
        ax.axis("off")

    fig.suptitle(
        f"Top {n} NSE stocks near 52-week high — last {chart_days} trading days "
        f"(shaded = within {threshold_pct:g}% of high)",
        fontsize=12,
    )
    fig.tight_layout(rect=(0, 0, 1, 0.96))

    if save_path:
        fig.savefig(save_path, dpi=130)
        log.info("Chart saved to %s", save_path)
    if show:
        plt.show()
    plt.close(fig)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="NSE 52-week-high momentum screener")
    p.add_argument("--threshold", type=float, default=5.0, help="max %% below 52W high (default 5)")
    p.add_argument("--top", type=int, default=20, help="number of top stocks to display (default 20)")
    p.add_argument("--source", choices=["yahoo", "tradingview"], default="yahoo",
                   help="price data source (tradingview needs: pip install tvkit)")
    p.add_argument("--universe", choices=["all", *NSE_INDEX_URLS], default="all",
                   help="stock universe (default: all NSE EQ series)")
    p.add_argument("--period", default="1y", help="history window for yfinance (default 1y)")
    p.add_argument("--use-close", action="store_true", help="use closing prices for 52W high instead of intraday highs")
    p.add_argument("--min-history", type=int, default=200, help="min trading days of history (default 200)")
    p.add_argument("--min-price", type=float, default=0.0, help="min current price filter")
    p.add_argument("--min-avg-volume", type=float, default=0.0, help="min 20-day avg volume filter")
    p.add_argument("--chart-days", type=int, default=90, help="trading days shown in charts (default 90)")
    p.add_argument("--batch-size", type=int, default=100, help="tickers per yfinance request")
    p.add_argument("--limit", type=int, default=0, help="only scan the first N tickers (for quick tests)")
    p.add_argument("--no-charts", action="store_true", help="skip chart generation")
    p.add_argument("--no-show", action="store_true", help="save chart to file without opening a window")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> pd.DataFrame:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)

    # 1. Data collection
    tickers = get_nse_tickers(args.universe)
    if args.limit:
        tickers = tickers[: args.limit]
    if args.source == "yahoo":
        history = download_history(tickers, period=args.period, batch_size=args.batch_size)
    else:
        import data_providers as dp
        history = dp.download_daily(tickers, period=args.period, source=args.source)
    if not history:
        print("\nNo price data downloaded — check your internet connection / Yahoo Finance access.")
        return pd.DataFrame()

    # 2 & 3. Screen + rank
    result = screen_near_52w_high(
        history,
        threshold_pct=args.threshold,
        use_close=args.use_close,
        min_history_days=args.min_history,
        min_price=args.min_price,
        min_avg_volume=args.min_avg_volume,
    )
    if result.empty:
        print(f"\nNo stocks within {args.threshold}% of their 52-week high.")
        return result

    # 4. Analysis
    top = result.head(args.top)
    OUTPUT_DIR.mkdir(exist_ok=True)
    stamp = f"{date.today():%Y%m%d}"
    csv_path = OUTPUT_DIR / f"near_52w_high_{args.universe}_{stamp}.csv"
    result.to_csv(csv_path)

    with pd.option_context("display.max_columns", None, "display.width", 200):
        print(f"\nTop {len(top)} of {len(result)} stocks within {args.threshold}% of 52-week high\n")
        print(top.drop(columns=["Ticker"]).to_string())
    print(f"\nFull results saved to {csv_path}")

    if not args.no_charts:
        plot_top_stocks(
            top,
            history,
            chart_days=args.chart_days,
            threshold_pct=args.threshold,
            save_path=OUTPUT_DIR / f"near_52w_high_{args.universe}_{stamp}.png",
            show=not args.no_show,
        )
    return result


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
