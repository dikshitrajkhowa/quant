"""
Entry point: launches the Streamlit UI for the NSE multi-strategy screener.

    python main.py                 # opens http://localhost:8501
    python main.py --port 8600     # custom port
    python main.py --headless      # don't auto-open a browser

For the command-line version without the UI, run:  python near_52w_high.py --help
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

APP = Path(__file__).resolve().parent / "app.py"


def main() -> int:
    p = argparse.ArgumentParser(description="Launch the NSE Strategy Screener UI")
    p.add_argument("--port", type=int, default=8501, help="port to serve on (default 8501)")
    p.add_argument("--headless", action="store_true", help="don't open a browser window")
    args, extra = p.parse_known_args()  # anything else is passed straight to streamlit

    cmd = [
        sys.executable, "-m", "streamlit", "run", str(APP),
        "--server.port", str(args.port),
        "--server.headless", str(args.headless).lower(),
        "--browser.gatherUsageStats", "false",
        *extra,
    ]
    try:
        return subprocess.call(cmd, cwd=APP.parent)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
