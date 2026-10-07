# Signal Desk — NSE/BSE Strategy Backtester

A small full-stack app: pick a stock, pick a strategy, hit **Run backtest**, and get
a BUY/SELL/HOLD decision, performance metrics, and a chart — all served by a single
FastAPI app.

```
stockapp/
├── backend/
│   ├── main.py            # FastAPI app + API routes
│   ├── engine.py          # data fetch, backtest math, chart rendering
│   ├── strategies.py      # strategy definitions (add your own here)
│   └── requirements.txt
└── frontend/
    ├── index.html
    ├── style.css
    └── app.js
```

## Setup

```bash
cd backend
pip install -r requirements.txt
```

## Run

```bash
uvicorn main:app --reload --port 8000
```

Open **http://localhost:8000** — the frontend is served by the same app, no
separate dev server needed.

> Note: this needs internet access to reach Yahoo Finance (`query1/query2.finance.yahoo.com`)
> at request time. If you're running this from a network-restricted sandbox, you'll
> see a "No data returned" error — it'll work fine on a normal machine/network.

## API

- `GET /api/stocks` — curated list of tickers for the dropdown
- `GET /api/strategies` — available strategies + their tunable parameters
- `POST /api/backtest` — run a backtest

  ```json
  {
    "symbol": "TCS",
    "exchange": "NSE",
    "strategy": "sma_crossover",
    "start": "2021-01-01",
    "capital": 100000,
    "commission_bps": 5,
    "params": { "fast": 20, "slow": 50 }
  }
  ```

  Returns the decision, an explanation of the current signal, performance metrics
  (CAGR, Sharpe, max drawdown, total return, trade count, benchmark comparison),
  and a base64-encoded PNG chart.

## Adding a new strategy

Add a function to `strategies.py` with the signature
`(df: pd.DataFrame, params: dict) -> (pd.DataFrame with a 'position' column, explanation: str)`,
then register it in `STRATEGY_REGISTRY`. It will automatically appear in the
frontend dropdown with its own parameter inputs.

## Notes / limitations

- Long-only strategies (no shorting or leverage)
- Transaction costs are a flat bps assumption, not real slippage
- The stock list is a curated starter set — any valid NSE/BSE symbol works if you
  add it to `STOCKS` in `main.py`, or wire the dropdown to a live symbol-search API
- This is an educational tool, not investment advice
