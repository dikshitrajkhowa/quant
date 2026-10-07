"""
Thin DhanHQ v2 client for the *data* APIs a screener needs.

Only read-only endpoints are used, so no static IP is required (Dhan requires
static IP only for order placement / modification / cancellation).

Endpoints (docs: https://dhanhq.co/docs/v2/):
  GET  images.dhan.co/api-data/api-scrip-master-detailed.csv   instrument master
  POST /v2/charts/historical     daily OHLCV   (securityId, exchangeSegment, instrument, fromDate, toDate)
  POST /v2/charts/intraday       1/5/15/25/60-min OHLCV (max 90 days per call)
  POST /v2/marketfeed/quote      live snapshot, up to 1000 instruments/request, 1 request/sec

Credentials are read from a `.env` file next to this script (copy .env.example),
falling back to real environment variables:
  DHAN_CLIENT_ID=1000000001
  DHAN_ACCESS_TOKEN=eyJ...
"""
from __future__ import annotations

import datetime as dt
import os
import re
import threading
import time
from io import StringIO
from pathlib import Path

import pandas as pd
import requests

BASE = "https://api.dhan.co/v2"
SCRIP_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master-detailed.csv"
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
ENV_PATH = Path(__file__).resolve().parent / ".env"
TOKEN_TTL_HOURS = 24


class DhanError(RuntimeError):
    pass

# ── .env handling (no extra dependency) ──────────────────────────────────────

_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


def read_env(path: Path | str = ENV_PATH) -> dict[str, str]:
    """Parse KEY=VALUE lines. Supports comments, blank lines, quotes and `export`."""
    p = Path(path)
    if not p.exists():
        return {}
    out = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = _LINE.match(line)
        if not m:
            continue
        key, val = m.groups()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        else:
            val = val.split(" #", 1)[0].strip()      # inline comment on unquoted value
        out[key] = val
    return out


def load_env(path: Path | str = ENV_PATH) -> dict[str, str]:
    """Load .env into os.environ. The file wins over existing variables, so a token
    refreshed in .env takes effect without restarting the shell."""
    vals = read_env(path)
    os.environ.update(vals)
    return vals


def update_env(values: dict[str, str], path: Path | str = ENV_PATH) -> None:
    """Set keys in .env in place, keeping other lines and comments untouched."""
    p = Path(path)
    lines = p.read_text(encoding="utf-8").splitlines() if p.exists() else []
    pending = dict(values)
    for i, line in enumerate(lines):
        m = _LINE.match(line)
        if m and m.group(1) in pending:
            lines[i] = f"{m.group(1)}={pending.pop(m.group(1))}"
    lines += [f"{k}={v}" for k, v in pending.items()]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.environ.update(values)


def save_token(access_token: str, client_id: str | None = None, path: Path | str = ENV_PATH) -> None:
    """Store a fresh access token (and optionally client id) with its save time."""
    vals = {"DHAN_ACCESS_TOKEN": access_token.strip(),
            "DHAN_TOKEN_SAVED_AT": dt.datetime.now(IST).isoformat(timespec="seconds")}
    if client_id:
        vals["DHAN_CLIENT_ID"] = client_id.strip()
    update_env(vals, path)


def token_status(path: Path | str = ENV_PATH) -> dict:
    """What the UI shows: is a token configured, and roughly how old is it."""
    load_env(path)
    token = os.environ.get("DHAN_ACCESS_TOKEN", "")
    if token.startswith("paste-"):
        token = ""
    saved =os.environ.get("DHAN_TOKEN_SAVED_AT")
    age_h = None
    if saved:
        try:
            age_h = (dt.datetime.now(IST) - dt.datetime.fromisoformat(saved)).total_seconds() / 3600
        except ValueError:
            pass
    return {
        "env_file": str(Path(path)), "env_exists": Path(path).exists(),
        "client_id": os.environ.get("DHAN_CLIENT_ID", ""),
        "has_token": bool(token),
        "token_hint": f"…{token[-6:]}" if len(token) > 10 else ("set" if token else ""),
        "saved_at": saved, "age_hours": age_h,
        "likely_expired": age_h is not None and age_h >= TOKEN_TTL_HOURS,
    }


class RateLimiter:
    """Thread-safe 'at most N calls per second' limiter."""

    def __init__(self, per_second: float):
        self.interval = 1.0 / per_second
        self.lock = threading.Lock()
        self.next_slot = 0.0

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            if now < self.next_slot:
                time.sleep(self.next_slot - now)
            self.next_slot = max(now, self.next_slot) + self.interval


class DhanClient:
    def __init__(self, client_id: str | None = None, access_token: str | None = None,
                 history_rps: float = 4, timeout: int = 20, env_path: Path | str = ENV_PATH):
        load_env(env_path)
        self.client_id = client_id or os.environ.get("DHAN_CLIENT_ID")
        self.token = access_token or os.environ.get("DHAN_ACCESS_TOKEN")
        if self.token and self.token.startswith("paste-"):        # untouched .env.example value
            self.token = None
        if not (self.client_id and self.token):
            raise DhanError(f"DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN not set. Add them to {Path(env_path)} "
                            "(copy .env.example) or paste the token in the UI sidebar.")
        self.session = requests.Session()
        self.session.headers.update({
            "access-token": self.token,
            "client-id": self.client_id,
            "Content-Type": "application/json",
            "Accept": "application/json",
        })
        self.history_limiter = RateLimiter(history_rps)
        self.quote_limiter = RateLimiter(1)          # documented: 1 request/sec
        self.timeout = timeout

    # ── low level ────────────────────────────────────────────────────────────
    def _post(self, path: str, body: dict, limiter: RateLimiter, retries: int = 4) -> dict:
        last = None
        for attempt in range(retries):
            limiter.wait()
            r = self.session.post(BASE + path, json=body, timeout=self.timeout)
            if r.status_code == 401:
                raise DhanError("401 Unauthorized: access token is invalid or expired "
                                "(tokens are valid for 24h — generate a fresh one).")
            if r.status_code == 429 or r.status_code >= 500:
                last = f"HTTP {r.status_code}"
                time.sleep(2 ** attempt)
                continue
            try:
                data = r.json()
            except ValueError:
                raise DhanError(f"{path}: non-JSON response ({r.status_code}): {r.text[:200]}")
            err = data.get("errorCode") if isinstance(data, dict) else None
            if err == "DH-904":                       # rate limit exceeded
                last = err
                time.sleep(2 ** attempt)
                continue
            if r.status_code != 200 or err or data.get("status") == "failure":
                raise DhanError(f"{path}: {err or r.status_code} — "
                                f"{data.get('errorMessage') or data.get('remarks') or data}")
            return data
        raise DhanError(f"{path}: gave up after {retries} retries ({last})")

    def profile(self) -> dict:
        """GET /v2/profile: cheap call to check the token (returns tokenValidity, dataPlan, …)."""
        r = self.session.get(BASE + "/profile", timeout=self.timeout)
        if r.status_code == 401:
            raise DhanError("401 Unauthorized: access token is invalid or expired.")
        try:
            data = r.json()
        except ValueError:
            raise DhanError(f"/profile: non-JSON response ({r.status_code}): {r.text[:200]}")
        if r.status_code != 200 or (isinstance(data, dict) and data.get("errorCode")):
            raise DhanError(f"/profile: {data.get('errorCode') or r.status_code} — "
                            f"{data.get('errorMessage') or data}")
        return data

    # ── instruments ──────────────────────────────────────────────────────────
    @staticmethod
    def instrument_master() -> pd.DataFrame:
        r = requests.get(SCRIP_MASTER_URL, timeout=60)
        r.raise_for_status()
        df = pd.read_csv(StringIO(r.text), low_memory=False)
        df.columns = [c.strip().upper() for c in df.columns]
        return df

    @classmethod
    def nse_equities(cls, master: pd.DataFrame | None = None) -> pd.DataFrame:
        """NSE cash-market EQ series stocks: security_id, symbol, isin, asm_gsm."""
        m = cls.instrument_master() if master is None else master

        def col(*names):
            for n in names:
                if n in m.columns:
                    return m[n]
            return pd.Series([None] * len(m), index=m.index)

        df = pd.DataFrame({
            "security_id": col("SECURITY_ID", "SEM_SMST_SECURITY_ID").astype(str),
            "exch": col("EXCH_ID", "SEM_EXM_EXCH_ID"),
            "segment": col("SEGMENT", "SEM_SEGMENT"),
            "instrument": col("INSTRUMENT", "SEM_INSTRUMENT_NAME"),
            "series": col("SERIES", "SEM_SERIES"),
            "symbol": col("UNDERLYING_SYMBOL", "SEM_TRADING_SYMBOL", "SYMBOL_NAME"),
            "isin": col("ISIN"),
            "asm_gsm": col("ASM_GSM_FLAG").fillna("N"),
        })
        eq = df[(df.exch == "NSE") & (df.segment == "E") &
                (df.instrument == "EQUITY") & (df.series == "EQ")]
        return eq.drop(columns=["exch", "segment", "instrument", "series"]).reset_index(drop=True)

    # ── historical ───────────────────────────────────────────────────────────
    @staticmethod
    def _candles(data: dict, daily: bool) -> pd.DataFrame:
        if not data.get("timestamp"):
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        idx = pd.to_datetime(data["timestamp"], unit="s", utc=True).tz_convert(IST)
        if daily:
            idx = idx.normalize().tz_localize(None)
        df = pd.DataFrame({k: data[k] for k in ("open", "high", "low", "close", "volume")}, index=idx)
        return df[~df.index.duplicated(keep="last")].sort_index()

    def daily(self, security_id: str, days: int = 400) -> pd.DataFrame:
        to = dt.date.today() + dt.timedelta(days=1)          # toDate is non-inclusive
        body = {"securityId": str(security_id), "exchangeSegment": "NSE_EQ",
                "instrument": "EQUITY", "expiryCode": 0, "oi": False,
                "fromDate": (to - dt.timedelta(days=days)).isoformat(), "toDate": to.isoformat()}
        return self._candles(self._post("/charts/historical", body, self.history_limiter), daily=True)

    def intraday(self, security_id: str, interval: int = 5, days: int = 5) -> pd.DataFrame:
        if interval not in (1, 5, 15, 25, 60) or days > 90:
            raise ValueError("interval must be 1/5/15/25/60 and days <= 90")
        now = dt.datetime.now(IST)
        body = {"securityId": str(security_id), "exchangeSegment": "NSE_EQ",
                "instrument": "EQUITY", "interval": str(interval), "oi": False,
                "fromDate": (now - dt.timedelta(days=days)).strftime("%Y-%m-%d 09:15:00"),
                "toDate": now.strftime("%Y-%m-%d %H:%M:%S")}
        return self._candles(self._post("/charts/intraday", body, self.history_limiter), daily=False)

    # ── live snapshot ────────────────────────────────────────────────────────
    def quotes(self, security_ids: list[str]) -> pd.DataFrame:
        """Live quote snapshot for NSE_EQ ids (batched 1000/request)."""
        rows = []
        for i in range(0, len(security_ids), 1000):
            batch = [int(s) for s in security_ids[i:i + 1000]]
            data = self._post("/marketfeed/quote", {"NSE_EQ": batch}, self.quote_limiter)
            for sid, q in (data.get("data", {}).get("NSE_EQ") or {}).items():
                ohlc = q.get("ohlc") or {}
                ltp = q.get("last_price")
                net = q.get("net_change")
                prev_close = (ltp - net) if (ltp is not None and net is not None) else ohlc.get("close")
                rows.append({"security_id": str(sid), "ltp": ltp, "day_open": ohlc.get("open"),
                             "day_high": ohlc.get("high"), "day_low": ohlc.get("low"),
                             "prev_close": prev_close, "day_volume": q.get("volume")})
        return pd.DataFrame(rows).set_index("security_id") if rows else pd.DataFrame()
