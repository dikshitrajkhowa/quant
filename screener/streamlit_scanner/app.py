"""
Streamlit UI for the NSE multi-strategy stock screener.

Launch with:  python main.py      (or: streamlit run app.py)
Strategies live in strategies.py - add one there and it appears in the dropdown.
"""

from __future__ import annotations

import time
from datetime import date

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

import data_providers as dp
import near_52w_high as core
import strategies as strat

st.set_page_config(page_title="NSE Strategy Screener", page_icon="📈", layout="wide")

# Palette (validated reference categorical slots + neutral ink)
BLUE = "#2a78d6"      # price series / matches
UP = "#1baf7a"        # candle up
DOWN = "#e34948"      # candle down
NEUTRAL = "#8a8984"   # reference lines
OTHERS = "rgba(138,137,132,0.45)"  # non-matching stocks in breadth chart
BAND = "rgba(42,120,214,0.10)"     # threshold zone
OVERLAY_COLORS = {"20 DMA": "#eb6834", "50 DMA": "#4a3aa7", "150 DMA": "#e87ba4", "200 DMA": "#eda100",
                  "BB upper": NEUTRAL, "BB mid": NEUTRAL, "BB lower": NEUTRAL}

UNIVERSE_LABELS = {
    "all": "All NSE equities (EQ series)",
    "nifty50": "Nifty 50",
    "nifty100": "Nifty 100",
    "nifty500": "Nifty 500",
}
HISTORY_PERIOD = "2y"  # 2 years so 200-DMA strategies have enough history; 52W metrics use the last 252 sessions
PRICE_COLS = {"Current Price", "52W High", "52W Low", "Prior 52W High"}


def metric(container, *args, **kwargs):
    """st.metric that drops `delta_arrow` on older Streamlit versions."""
    try:
        container.metric(*args, **kwargs)
    except TypeError:
        kwargs.pop("delta_arrow", None)
        container.metric(*args, **kwargs)


# --------------------------------------------------------------------------- #
# Data (cached)
# --------------------------------------------------------------------------- #
@st.cache_data(ttl=3600, show_spinner=False)
def load_tickers(universe: str) -> list[str]:
    return core.get_nse_tickers(universe)


@st.cache_data(ttl=3600, show_spinner=False)
def load_history(tickers: tuple[str, ...], period: str, batch_size: int, source: str = "yahoo") -> dict[str, pd.DataFrame]:
    bar = st.progress(0.0, text="Downloading price history…")

    def cb(done: int, total: int) -> None:
        bar.progress(done / total, text=f"Downloading price history… batch {done}/{total}")

    data = dp.download_daily(list(tickers), period=period, source=source, batch_size=batch_size, progress_callback=cb)
    bar.empty()
    return data


@st.cache_data(ttl=3600, show_spinner="Running strategy…", max_entries=50)
def evaluate(data_token: str, strategy_key: str, params: tuple, min_hist: int, _history: dict) -> pd.DataFrame:
    """Cached per (dataset, strategy, params). `_history` is not hashed - `data_token` identifies it."""
    return strat.run_strategy(_history, strat.STRATEGIES[strategy_key], dict(params), min_hist)


# --------------------------------------------------------------------------- #
# Charts
# --------------------------------------------------------------------------- #
def add_levels(fig, levels, row=None, col=None, label=True):
    for lv in levels:
        if "y0" in lv:
            lo, hi = sorted((lv["y0"], lv["y"]))
            fig.add_hrect(y0=lo, y1=hi, fillcolor=BAND, line_width=0, layer="below", row=row, col=col)
        kw = dict(annotation_text=f"{lv['label']} ₹{lv['y']:,.2f}", annotation_position="top left") if label else {}
        fig.add_hline(y=lv["y"], line=dict(color=NEUTRAL, dash="dash", width=1.5 if label else 1),
                      row=row, col=col, **kw)


def ranking_bar(top: pd.DataFrame, s: strat.Strategy) -> go.Figure:
    df = top.iloc[::-1]  # rank 1 at top
    vals = df[s.score_col]
    fig = go.Figure(go.Bar(
        x=vals, y=df["Symbol"], orientation="h",
        marker=dict(color=BLUE, cornerradius=4),
        customdata=np.stack([df["Current Price"], df["% From 52W High"]], axis=-1),
        hovertemplate=(f"<b>%{{y}}</b><br>{s.score_col}: %{{x}}"
                       "<br>Price ₹%{customdata[0]:,.2f}<br>%{customdata[1]:.2f}% below 52W high<extra></extra>"),
        text=[s.score_fmt.format(v) if pd.notna(v) else "" for v in vals],
        textposition="outside", cliponaxis=False,
    ))
    direction = "lower ranks higher" if s.ascending else "higher ranks higher"
    fig.update_layout(height=max(320, 26 * len(df) + 80), margin=dict(l=10, r=50, t=10, b=30),
                      xaxis_title=f"{s.score_col} ({direction})", yaxis_title=None, bargap=0.35)
    fig.update_xaxes(showgrid=True, zeroline=True)
    fig.update_yaxes(showgrid=False)
    return fig


def score_distribution(all_df: pd.DataFrame, s: strat.Strategy) -> go.Figure:
    col = s.score_col
    vals = all_df[col].astype(float)
    lo, hi = np.nanpercentile(vals, [1, 99]) if vals.notna().any() else (0, 1)
    clipped = vals.clip(lo, hi)
    size = (hi - lo) / 40 if hi > lo else 1
    bins = dict(start=lo, end=hi + size, size=size)
    fig = go.Figure()
    fig.add_trace(go.Histogram(x=clipped[~all_df["Passed"]], xbins=bins, name="Other stocks",
                               marker=dict(color=OTHERS), hovertemplate="%{x}<br>%{y} stocks<extra>Others</extra>"))
    fig.add_trace(go.Histogram(x=clipped[all_df["Passed"]], xbins=bins, name="Strategy matches",
                               marker=dict(color=BLUE), hovertemplate="%{x}<br>%{y} stocks<extra>Matches</extra>"))
    fig.update_layout(barmode="stack", bargap=0.06, height=360, margin=dict(l=10, r=10, t=30, b=30),
                      xaxis_title=f"{col} (1st–99th percentile shown)", yaxis_title="Number of stocks",
                      legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0))
    return fig


def high_distribution(all_df: pd.DataFrame) -> go.Figure:
    fig = go.Figure(go.Histogram(
        x=all_df["% From 52W High"].clip(upper=80), xbins=dict(start=0, end=80, size=2.5),
        marker=dict(color=BLUE), hovertemplate="%{x}% below high<br>%{y} stocks<extra></extra>"))
    fig.update_layout(height=360, margin=dict(l=10, r=10, t=30, b=30), bargap=0.06,
                      xaxis_title="% below 52-week high (80%+ grouped)", yaxis_title="Number of stocks")
    return fig


def small_multiples(top, history, days, s, params, cols=4) -> go.Figure:
    n = len(top)
    rows = int(np.ceil(n / cols))
    titles = [f"#{rank} {r['Symbol']} · {s.score_fmt.format(r[s.score_col])}" for rank, r in top.iterrows()]
    fig = make_subplots(rows=rows, cols=cols, subplot_titles=titles,
                        vertical_spacing=0.09 if rows < 4 else 0.05, horizontal_spacing=0.05)
    for i, (_, r) in enumerate(top.iterrows()):
        row, col = i // cols + 1, i % cols + 1
        full = history[r["Ticker"]]
        df = full.tail(days)
        fig.add_trace(go.Scatter(x=df.index, y=df["Close"], mode="lines", line=dict(color=BLUE, width=2),
                                 showlegend=False,
                                 hovertemplate=f"<b>{r['Symbol']}</b><br>%{{x|%d %b %Y}}<br>₹%{{y:,.2f}}<extra></extra>"),
                      row=row, col=col)
        for ov in s.overlays:  # thin indicator lines, legend once
            for name, ser in strat.overlay_series(full, ov).items():
                fig.add_trace(go.Scatter(x=df.index, y=ser.tail(days), mode="lines", name=name,
                                         line=dict(color=OVERLAY_COLORS.get(name, NEUTRAL), width=1,
                                                   dash="dot" if name.startswith("BB") else "solid"),
                                         showlegend=(i == 0), legendgroup=name, hoverinfo="skip"),
                              row=row, col=col)
        # shapes after traces: plotly skips shapes on subplots with no traces yet
        if s.levels:
            add_levels(fig, s.levels(r, params), row=row, col=col, label=False)
    fig.update_layout(height=230 * rows + 40, margin=dict(l=10, r=10, t=60, b=10), hovermode="x",
                      legend=dict(orientation="h", yanchor="bottom", y=1.0 + 30 / (230 * rows), x=0))
    fig.update_annotations(font_size=12)
    fig.update_xaxes(showgrid=False, tickformat="%b", nticks=4)
    fig.update_yaxes(nticks=4)
    return fig


def detail_chart(full: pd.DataFrame, row: pd.Series, days: int, s: strat.Strategy, params: dict) -> go.Figure:
    view = full.tail(days)
    extra = s.panel in ("rsi", "macd")
    heights = [0.6, 0.15, 0.25] if extra else [0.75, 0.25]
    fig = make_subplots(rows=len(heights), cols=1, shared_xaxes=True, row_heights=heights, vertical_spacing=0.03)

    fig.add_trace(go.Candlestick(x=view.index, open=view["Open"], high=view["High"], low=view["Low"],
                                 close=view["Close"], increasing_line_color=UP, decreasing_line_color=DOWN,
                                 name="OHLC"), row=1, col=1)
    for ov in s.overlays:
        for name, ser in strat.overlay_series(full, ov).items():
            fig.add_trace(go.Scatter(x=view.index, y=ser.tail(days), name=name,
                                     line=dict(color=OVERLAY_COLORS.get(name, NEUTRAL), width=1.5,
                                               dash="dot" if name.startswith("BB") else "solid")), row=1, col=1)
    if s.levels:
        add_levels(fig, s.levels(row, params), row=1, col=1)

    vol_colors = np.where(view["Close"] >= view["Open"], UP, DOWN)
    fig.add_trace(go.Bar(x=view.index, y=view["Volume"], marker_color=vol_colors, name="Volume", showlegend=False,
                         hovertemplate="%{x|%d %b %Y}<br>Vol %{y:,.0f}<extra></extra>"), row=2, col=1)
    fig.update_yaxes(title_text="Price (₹)", row=1, col=1)
    fig.update_yaxes(title_text="Volume", row=2, col=1)

    if s.panel == "rsi":
        r = strat.rsi(full["Close"]).tail(days)
        fig.add_trace(go.Scatter(x=view.index, y=r, name="RSI(14)", line=dict(color=BLUE, width=1.5)), row=3, col=1)
        fig.add_hrect(y0=30, y1=70, fillcolor=BAND, line_width=0, layer="below", row=3, col=1)
        for lvl in (30, 70):
            fig.add_hline(y=lvl, line=dict(color=NEUTRAL, dash="dash", width=1), row=3, col=1)
        fig.update_yaxes(title_text="RSI", range=[0, 100], row=3, col=1)
    elif s.panel == "macd":
        m = strat.macd(full["Close"]).tail(days)
        fig.add_trace(go.Bar(x=view.index, y=m["Hist"], name="Histogram",
                             marker_color=np.where(m["Hist"] >= 0, UP, DOWN)), row=3, col=1)
        fig.add_trace(go.Scatter(x=view.index, y=m["MACD"], name="MACD", line=dict(color=BLUE, width=1.5)), row=3, col=1)
        fig.add_trace(go.Scatter(x=view.index, y=m["Signal"], name="Signal", line=dict(color="#eb6834", width=1.5)),
                      row=3, col=1)
        fig.update_yaxes(title_text="MACD", row=3, col=1)

    fig.update_layout(height=640 if extra else 560, margin=dict(l=10, r=10, t=30, b=10),
                      xaxis_rangeslider_visible=False, hovermode="x unified",
                      legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0))
    fig.update_xaxes(rangebreaks=[dict(bounds=["sat", "mon"])])
    return fig


def column_config(df: pd.DataFrame, s: strat.Strategy, params: dict, chart_days: int) -> dict:
    cfg = {}
    for c in df.columns:
        if c in PRICE_COLS:
            cfg[c] = st.column_config.NumberColumn(format="₹%.2f")
        elif "%" in c:
            cfg[c] = st.column_config.NumberColumn(format="%.2f%%")
        elif "Ratio" in c:
            cfg[c] = st.column_config.NumberColumn(format="%.2f×")
        elif c == "Avg Vol (20D)":
            cfg[c] = st.column_config.NumberColumn(format="localized")
    if s.key == "near_52w_high":
        cfg["% From 52W High"] = st.column_config.ProgressColumn(
            "% From 52W High", format="%.2f%%", min_value=0.0, max_value=float(params["threshold"]))
    cfg["Trend"] = st.column_config.LineChartColumn(f"Last {chart_days}D", width="medium")
    return cfg


# --------------------------------------------------------------------------- #
# Sidebar
# --------------------------------------------------------------------------- #
with st.sidebar:
    st.header("Strategy")
    skey = st.selectbox("Screening strategy", list(strat.STRATEGIES),
                        format_func=lambda k: strat.STRATEGIES[k].name, key="strategy")
    S = strat.STRATEGIES[skey]
    st.caption(S.description)

    params = {}
    for p in S.params:
        wkey = f"{skey}.{p.key}"
        if p.kind == "bool":
            params[p.key] = st.toggle(p.label, value=p.default, help=p.help, key=wkey)
        elif p.kind == "int":
            params[p.key] = st.slider(p.label, int(p.min), int(p.max), int(p.default), int(p.step), help=p.help, key=wkey)
        else:
            params[p.key] = st.slider(p.label, float(p.min), float(p.max), float(p.default), float(p.step),
                                      help=p.help, key=wkey)

    st.header("Universe & display")
    PROVIDERS = dp.available()
    source = st.selectbox(
        "Data source", list(PROVIDERS),
        format_func=lambda k: PROVIDERS[k]["name"] + ("" if PROVIDERS[k]["available"] else " (not installed)"),
        help="\n\n".join(f"**{v['name']}** — {v['note']}" for v in PROVIDERS.values()))
    if not PROVIDERS[source]["available"]:
        st.caption(f"Install with: `{PROVIDERS[source]['install_hint']}`")
    elif source == "tradingview":
        st.caption("Unofficial API, one request per stock — use Nifty 500 or smaller for speed.")
    universe = st.selectbox("Universe", list(UNIVERSE_LABELS), format_func=UNIVERSE_LABELS.get, index=0)
    top_n = st.slider("Top N to display", 5, 50, 20, 5)

    with st.expander("Filters"):
        min_price = st.number_input("Min price (₹)", 0.0, value=0.0, step=10.0)
        min_vol = st.number_input("Min 20-day avg volume", 0, value=0, step=50_000)
        min_hist = st.number_input("Min trading days of history", 50, 500, 200, 10)

    with st.expander("Advanced"):
        chart_days = st.slider("Trading days in charts", 30, 500, 120, 10)
        limit = st.number_input("Scan only first N tickers (0 = all)", 0, value=0, step=50)
        batch_size = st.number_input("Tickers per download batch", 20, 500, 100, 20)

    run = st.button("Download data & run", type="primary", width="stretch")
    if st.button("Clear cached data", width="stretch"):
        st.cache_data.clear()
        for k in ("history", "data_token"):
            st.session_state.pop(k, None)
        st.toast("Cache cleared")

st.title(S.name)
st.caption("NSE strategy screener · switch strategies in the sidebar; data is downloaded once and reused"
           + (f" · data: {dp.PROVIDERS[st.session_state['source']]['name']}" if "source" in st.session_state else "") + ".")

# --------------------------------------------------------------------------- #
# Data load (only on button press; strategy/param changes re-run instantly)
# --------------------------------------------------------------------------- #
if run:
    try:
        with st.spinner("Fetching NSE stock list…"):
            tickers = load_tickers(universe)
        if limit:
            tickers = tickers[: int(limit)]
        history = load_history(tuple(tickers), HISTORY_PERIOD, int(batch_size), source)
    except Exception as exc:
        st.error(f"Data download failed: {exc}")
        st.stop()
    if not history:
        st.error(f"No price data downloaded — check your internet connection / {PROVIDERS[source]['name']} access.")
        st.stop()
    st.session_state.update(history=history, universe=universe, source=source, n_tickers=len(tickers),
                            data_token=f"{source}-{universe}-{len(tickers)}-{time.time():.0f}")

if "history" not in st.session_state:
    st.info("Pick a strategy and universe in the sidebar, then click **Download data & run**. "
            "Scanning all NSE stocks takes a few minutes the first time; after that you can switch strategies "
            "and parameters instantly.")
    st.stop()

if st.session_state.get("universe") != universe or st.session_state.get("source", "yahoo") != source:
    loaded_src = PROVIDERS[st.session_state.get("source", "yahoo")]["name"]
    st.warning(f"Showing **{UNIVERSE_LABELS[st.session_state['universe']]}** data from **{loaded_src}** — click "
               "**Download data & run** to load the new selection.")

history: dict[str, pd.DataFrame] = st.session_state["history"]
all_df = evaluate(st.session_state["data_token"], skey, tuple(sorted(params.items())), int(min_hist), history)
result = strat.rank_matches(all_df, S, min_price=min_price, min_avg_volume=min_vol)

# --------------------------------------------------------------------------- #
# KPI row
# --------------------------------------------------------------------------- #
last_date = max(df.index[-1] for df in history.values()).date()
new_highs = int((all_df["52W High Date"] == all_df["Last Date"]).sum()) if not all_df.empty else 0
k1, k2, k3, k4 = st.columns(4)
metric(k1, "Stocks evaluated", f"{len(all_df):,}",
       help=f"{st.session_state['n_tickers']:,} tickers loaded; stocks with too little history for this strategy "
            "are excluded")
metric(k2, "Strategy matches", f"{len(result):,}",
       f"{len(result) / max(len(all_df), 1):.1%} of evaluated", delta_color="off", delta_arrow="off")
metric(k3, "New 52W highs today", f"{new_highs:,}", help="Market breadth: stocks that set a 52-week high in the "
       "latest session")
metric(k4, "Data as of", f"{last_date:%d %b %Y}")

if result.empty:
    st.warning("No stocks match this strategy with the current parameters and filters.")
    if not all_df.empty:
        st.plotly_chart(score_distribution(all_df, S), theme="streamlit", width="stretch")
    st.stop()

top = result.head(top_n)
tab_rank, tab_charts, tab_detail, tab_dist = st.tabs(["Ranking", "Price charts", "Stock detail", "Market breadth"])

with tab_rank:
    left, right = st.columns([2, 3], gap="large")
    with left:
        st.subheader(f"Top {len(top)} by {S.score_col}")
        st.plotly_chart(ranking_bar(top, S), theme="streamlit", width="stretch")
    with right:
        st.subheader("Results")
        base_cols = ["Symbol", "Current Price", "% From 52W High", "52W High", "52W Low", "1M Return %",
                     "Avg Vol (20D)"]
        strategy_cols = [c for c in result.columns if c not in base_cols and c not in
                         ("Ticker", "Passed", "52W High Date", "% Above 52W Low", "Last Date")]
        if S.score_col in strategy_cols:
            strategy_cols.remove(S.score_col)
        order = ["Symbol", S.score_col, *strategy_cols, *[c for c in base_cols[1:] if c != S.score_col]]
        table = result[list(dict.fromkeys(order))].copy()
        table["Trend"] = result["Ticker"].map(lambda t: history[t]["Close"].tail(chart_days).round(2).tolist())
        st.dataframe(table, width="stretch", height=min(38 * len(table) + 40, 720),
                     column_config=column_config(table, S, params, chart_days))
        st.download_button(
            "Download results (CSV)", result.drop(columns=["Passed"]).to_csv().encode(),
            file_name=f"{skey}_{st.session_state['universe']}_{date.today():%Y%m%d}.csv", mime="text/csv")

with tab_charts:
    st.caption(f"Last {chart_days} trading days with this strategy's indicators. "
               + ("Dashed line / shaded band = strategy reference level." if S.levels else ""))
    st.plotly_chart(small_multiples(top, history, chart_days, S, params), theme="streamlit", width="stretch")

with tab_detail:
    pick = st.selectbox("Stock", top["Symbol"].tolist(),
                        format_func=lambda s: f"#{top.index[top['Symbol'] == s][0]}  {s}")
    row = top[top["Symbol"] == pick].iloc[0]
    c1, c2, c3, c4 = st.columns(4)
    metric(c1, "Price", f"₹{row['Current Price']:,.2f}", f"{row['1M Return %']:+.2f}% (1M)")
    metric(c2, S.score_col, S.score_fmt.format(row[S.score_col]))
    metric(c3, "52W High", f"₹{row['52W High']:,.2f}", f"{row['% From 52W High']:.2f}% below",
           delta_color="off", delta_arrow="off")
    metric(c4, "52W Low", f"₹{row['52W Low']:,.2f}", f"{row['% Above 52W Low']:.1f}% above",
           delta_color="off", delta_arrow="off")
    st.plotly_chart(detail_chart(history[row["Ticker"]], row, chart_days, S, params), theme="streamlit",
                    width="stretch")

with tab_dist:
    a, b = st.columns(2, gap="large")
    with a:
        st.subheader(f"{S.score_col} across the universe")
        st.caption("Blue = stocks that pass this strategy.")
        st.plotly_chart(score_distribution(all_df, S), theme="streamlit", width="stretch")
    with b:
        st.subheader("Distance from 52-week high")
        st.caption("Many stocks near their highs = broad strength; a thin left tail = narrow leadership.")
        st.plotly_chart(high_distribution(all_df), theme="streamlit", width="stretch")
