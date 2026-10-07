# Dhan screener

NSE screener (Large / Mid / Small cap buckets) on DhanHQ data, with an "explain why" report.

## Setup

```
pip install -r requirements.txt
copy .env.example .env        # then fill in DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN
```

Dhan access tokens expire after 24 hours. Update the token in `.env`, or paste it in the UI
sidebar (it writes `.env` for you and shows how old the token is).

## UI

```
streamlit run app.py          # or double-click run_ui.bat
```

- Pick a saved configuration, tweak mode, cap bucket, capital, risk, stop, factor weights and filters.
- **Run** shows the report, the picks table and all scores; each run is also saved to `reports/`.
- **Save**, **Save as new** and **Delete** manage configurations in `configs.json`.
- The **Demo** configuration uses synthetic data and needs no token.

## CLI

```
python dhan_screener.py --list-configs
python dhan_screener.py --config "Swing - large caps"
python dhan_screener.py --config "Intraday - live" --top 5     # flags override the saved config
python dhan_screener.py --mode swing --cap mid --top 10         # no config, plain flags
python dhan_screener.py --demo
```

Price history is cached per day in `.dhan_cache/`; use `--refresh` (or the UI toggle) to re-download.
