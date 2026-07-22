"""
FastAPI backend for the NSE/BSE strategy backtester.

Run with:
    uvicorn main:app --reload --port 8000

Then open http://localhost:8000 in a browser (the frontend is served
from the same app, no separate server needed).
"""

import sys
from datetime import date, timedelta
from pathlib import Path

# Make sure this file's own directory (backend/) is on sys.path, so
# `engine` and `strategies` import correctly no matter where uvicorn
# is launched from (e.g. `uvicorn backend.main:app` from the project root).
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from engine import fetch_price_data, run_backtest, decide_action, render_chart
from strategies import STRATEGY_REGISTRY

app = FastAPI(title="NSE/BSE Strategy Backtester")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# A curated starter list — feel free to add any symbol; suffix is added automatically.
STOCKS = [
    {"symbol": "RELIANCE", "name": "Reliance Industries"},
    {"symbol": "TCS", "name": "Tata Consultancy Services"},
    {"symbol": "INFY", "name": "Infosys"},
    {"symbol": "HDFCBANK", "name": "HDFC Bank"},
    {"symbol": "ICICIBANK", "name": "ICICI Bank"},
    {"symbol": "SBIN", "name": "State Bank of India"},
    {"symbol": "TATAMOTORS", "name": "Tata Motors"},
    {"symbol": "ITC", "name": "ITC Limited"},
    {"symbol": "HINDUNILVR", "name": "Hindustan Unilever"},
    {"symbol": "BAJFINANCE", "name": "Bajaj Finance"},
    {"symbol": "WIPRO", "name": "Wipro"},
    {"symbol": "MARUTI", "name": "Maruti Suzuki"},
    {"symbol": "ADANIENT", "name": "Adani Enterprises"},
    {"symbol": "SUNPHARMA", "name": "Sun Pharmaceutical"},
    {"symbol": "LT", "name": "Larsen & Toubro"},
]


class BacktestRequest(BaseModel):
    symbol: str = Field(..., description='Base symbol, e.g. "TCS" (without exchange suffix)')
    exchange: str = Field("NSE", description='"NSE" or "BSE"')
    strategy: str = Field(..., description="Key from /api/strategies")
    start: str = Field(default=None, description="YYYY-MM-DD, defaults to 3 years ago")
    end: str | None = None
    capital: float = 100_000.0
    commission_bps: float = 5.0
    params: dict = Field(default_factory=dict)


@app.get("/api/stocks")
def get_stocks():
    return STOCKS


@app.get("/api/strategies")
def get_strategies():
    return [
        {
            "key": key,
            "label": meta["label"],
            "description": meta["description"],
            "params": meta["params"],
        }
        for key, meta in STRATEGY_REGISTRY.items()
    ]


@app.post("/api/backtest")
def backtest(req: BacktestRequest):
    if req.strategy not in STRATEGY_REGISTRY:
        raise HTTPException(status_code=400, detail=f"Unknown strategy '{req.strategy}'")

    exchange_suffix = ".NS" if req.exchange.upper() == "NSE" else ".BO"
    ticker = f"{req.symbol.upper()}{exchange_suffix}"

    start = req.start or (date.today() - timedelta(days=3 * 365)).isoformat()

    try:
        price_df = fetch_price_data(ticker, start, req.end)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Data fetch failed: {e}")

    strategy_meta = STRATEGY_REGISTRY[req.strategy]
    signal_df, explanation = strategy_meta["func"](price_df, req.params)

    if signal_df["Close"].notna().sum() < max(
        [p["default"] for p in strategy_meta["params"] if "default" in p], default=1
    ):
        raise HTTPException(status_code=400, detail="Not enough price history for the chosen parameters.")

    result = run_backtest(signal_df, initial_capital=req.capital, commission_bps=req.commission_bps)
    data = result["data"]
    decision = decide_action(data)

    indicator_cols = [c for c in data.columns if c.startswith(("sma_", "rsi", "macd"))
                       and c not in ("rsi",)]  # rsi plotted separately if needed; keep price panel clean
    price_indicator_cols = [c for c in data.columns if c.startswith("sma_")]
    chart_b64 = render_chart(data, ticker, strategy_meta["label"], price_indicator_cols)

    return {
        "ticker": ticker,
        "strategy": strategy_meta["label"],
        "decision": decision,
        "explanation": explanation,
        "period": {"start": str(data.index[0].date()), "end": str(data.index[-1].date())},
        "metrics": result["metrics"],
        "chart_png_base64": chart_b64,
    }


# --- Serve the static frontend from the same app -----------------------------
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")

    @app.get("/")
    def index():
        return FileResponse(str(FRONTEND_DIR / "index.html"))
