"""
Entry point for the FastAPI screener app.

    python main.py                  # http://localhost:8000
    python main.py --port 9000
    python main.py --reload         # auto-reload on code changes (dev)
    python main.py --no-browser

API docs: http://localhost:8000/docs
"""

from __future__ import annotations

import argparse
import logging
import threading
import webbrowser

import uvicorn


def main() -> None:
    p = argparse.ArgumentParser(description="NSE Strategy Screener (FastAPI)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--reload", action="store_true", help="auto-reload on code changes")
    p.add_argument("--no-browser", action="store_true", help="don't open a browser tab")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s %(name)s: %(message)s")
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)

    if not args.no_browser:
        url = f"http://{'localhost' if args.host in ('127.0.0.1', '0.0.0.0') else args.host}:{args.port}"
        threading.Timer(1.5, webbrowser.open, args=(url,)).start()

    uvicorn.run("server:app", host=args.host, port=args.port, reload=args.reload,
                app_dir=str(__import__("pathlib").Path(__file__).resolve().parent))


if __name__ == "__main__":
    main()
