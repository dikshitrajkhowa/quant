"""
Live paper-trading engine.

Runs in a background thread inside the FastAPI server. Each *session* trades one stock with one
backtest strategy (same rules + parameters as the backtest) and sends orders to Megabull:

  every completed candle (e.g. 15 min)  -> fetch candles (Yahoo / TradingView), run strategy.live_signal()
  signal                                -> BUY market, qty = capital // price, product MIS
  in position                           -> exit at stop / target:
                                             * every ~15 s from Megabull's position LTP, when available
                                             * otherwise on each completed candle (low <= stop / high >= target)
  square-off time (15:20 IST)           -> SELL any open intraday position
State (sessions, positions, trades) is saved to app1/data/trading_state.json so a restart resumes.

Dry-run sessions run the same logic but don't send orders (fills are simulated at signal prices).
NSE holidays are not modelled: on a holiday no new candles arrive, so nothing happens.
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import pandas as pd

import backtest_engine as bte
import data_providers as dp
from megabull import MegabullClient, MegabullError, Order

log = logging.getLogger("trading")
IST = "Asia/Kolkata"
HERE = Path(__file__).resolve().parent
STATE_FILE = HERE / "data" / "trading_state.json"

OPEN, CLOSE = (9, 15), (15, 30)
ENV_SQUARE_OFF = tuple(int(x) for x in os.environ["TRADE_SQUAREOFF_TIME"].split(":")) if os.getenv("TRADE_SQUAREOFF_TIME") else None
CANDLE_DELAY = int(os.getenv("TRADE_CANDLE_DELAY_SEC", "20"))      # wait after a candle closes before fetching
CANDLE_MAX_WAIT = int(os.getenv("TRADE_CANDLE_MAX_WAIT_SEC", "240"))  # give up on a late candle after this
LTP_EVERY = int(os.getenv("TRADE_LTP_EVERY_SEC", "15"))
MAX_OPEN = int(os.getenv("TRADE_MAX_OPEN_POSITIONS", "10"))


def now_ist() -> datetime:
    return pd.Timestamp.now(tz=IST).to_pydatetime()


def hm(t: datetime) -> tuple[int, int]:
    return (t.hour, t.minute)


@dataclass
class Session:
    id: str
    ticker: str
    strategy: str
    params: dict
    source: str = "yahoo"
    capital: float = 100000.0
    dry_run: bool = False
    status: str = "waiting"          # waiting | in_position | done_for_day | stopped | error
    message: str = "Waiting for the next completed candle"
    created: str = ""
    origin: str = ""                 # backtest | screener | manual
    position: dict | None = None     # {qty, entry, stop, target, risk, entry_time, order_id, signal_bar}
    trades: list[dict] = field(default_factory=list)
    last_bar: str | None = None      # last candle evaluated
    day_stats: dict = field(default_factory=dict)
    last_close: float | None = None   # per-day counters: entries, consec, cool_until, squared
    last_check: str | None = None
    day: str | None = None

    @property
    def symbol(self) -> str:
        return self.ticker.rsplit(".", 1)[0]

    def public(self) -> dict:
        d = asdict(self)
        d["symbol"] = self.symbol
        d["realized_pnl"] = round(sum(t["pnl"] for t in self.trades), 2)
        d["today_trades"] = sum(1 for t in self.trades if t["exit_time"][:10] == (self.day or ""))
        sq = square_off_for(self)
        d["square_off"] = f"{sq[0]:02d}:{sq[1]:02d}" if sq else None
        tf = tf_of(self)
        d["timeframe"], d["timeframe_label"] = tf, dp.TIMEFRAMES[tf]["label"]
        return d


class TradingEngine:
    def __init__(self, client_factory: Callable[[], MegabullClient],
                 candles_fn: Callable[..., dict[str, pd.DataFrame]] | None = None,
                 clock: Callable[[], datetime] = now_ist, state_file: Path = STATE_FILE):
        self.client_factory = client_factory
        self.candles_fn = candles_fn or self._fetch_candles
        self.clock = clock
        self.state_file = state_file
        self.sessions: dict[str, Session] = {}
        self.log: deque[dict] = deque(maxlen=500)
        self.lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._group_bar: dict[tuple, str] = {}     # (source, interval) -> last processed candle start
        self._last_ltp = 0.0
        self._last_squareoff_day: str | None = None
        self._load()

    # ------------------------------------------------------------------ utils
    def _log(self, msg: str, level: str = "info", session: Session | None = None) -> None:
        entry = {"time": self.clock().strftime("%Y-%m-%d %H:%M:%S"), "level": level,
                 "session": session.id if session else None, "symbol": session.symbol if session else None,
                 "message": msg}
        self.log.append(entry)
        getattr(log, "warning" if level == "warn" else level if level in ("info", "error") else "info")(
            "%s%s", f"[{session.symbol}] " if session else "", msg)
        try:
            (HERE / "data").mkdir(exist_ok=True)
            with open(HERE / "data" / "trading.log", "a", encoding="utf-8") as f:
                f.write(f"{entry['time']} {level.upper():5} {entry['symbol'] or '-':12} {msg}\n")
        except OSError:
            pass

    def _save(self) -> None:
        try:
            self.state_file.parent.mkdir(exist_ok=True)
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps({"sessions": [asdict(s) for s in self.sessions.values()]}, indent=1, default=str),
                           encoding="utf-8")
            tmp.replace(self.state_file)
        except OSError as exc:
            log.error("Could not save trading state: %s", exc)

    def _load(self) -> None:
        if not self.state_file.exists():
            return
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            for d in data.get("sessions", []):
                s = Session(**d)
                self.sessions[s.id] = s
            if self.sessions:
                n_pos = sum(1 for s in self.sessions.values() if s.position)
                self._log(f"Restored {len(self.sessions)} session(s) from disk"
                          + (f", {n_pos} with open positions - check they match Megabull" if n_pos else ""))
        except Exception as exc:
            log.error("Could not load trading state: %s", exc)

    @staticmethod
    def _fetch_candles(tickers: list[str], tf: str, source: str, days: int = 3) -> dict[str, pd.DataFrame]:
        """Completed candles only (the forming intraday candle / today's, this week's, this month's bar dropped)."""
        data = dp.download_bars(tickers, tf, days, source=source, complete_only=True)
        m = dp.tf_minutes(tf)
        return {t: bte._drop_incomplete(df, m) for t, df in data.items()} if m else data

    # ------------------------------------------------------------------ public API
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="trading-engine", daemon=True)
        self._thread.start()

    def shutdown(self) -> None:
        self._stop.set()

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def create_sessions(self, tickers: list[str], strategy: str, params: dict, source: str, capital: float,
                        dry_run: bool, origin: str = "") -> list[Session]:
        if strategy not in bte.BACKTESTS or bte.BACKTESTS[strategy].live_signal is None:
            raise ValueError(f"Strategy '{strategy}' has no live-trading rules")
        out = []
        with self.lock:
            for t in tickers:
                dup = next((s for s in self.sessions.values() if s.ticker == t and s.status not in ("stopped",)), None)
                if dup:
                    self._log(f"Already trading {dup.symbol} (session {dup.id}) - skipped duplicate", "warn", dup)
                    continue
                s = Session(id=uuid.uuid4().hex[:8], ticker=t, strategy=strategy, params=params, source=source,
                            capital=float(capital), dry_run=dry_run, origin=origin,
                            created=self.clock().strftime("%Y-%m-%d %H:%M:%S"), day=self.clock().strftime("%Y-%m-%d"))
                self.sessions[s.id] = s
                out.append(s)
                sizing = (f"account ₹{capital:,.0f}, risking {params.get('risk_pct')}% per trade"
                          if bte.BACKTESTS[strategy].size else f"₹{capital:,.0f} per trade")
                self._log(f"Started {'DRY-RUN ' if dry_run else ''}session: {bte.BACKTESTS[strategy].name}, "
                          f"{sizing}, data from {dp.PROVIDERS[source]['name']}", session=s)
            self._save()
        return out

    def stop_session(self, sid: str, square_off: bool = True) -> Session:
        with self.lock:
            s = self.sessions[sid]
            if s.position and square_off:
                self._exit(s, "manual stop", None)
            s.status, s.message = "stopped", "Stopped by user" + ("" if not s.position else " (position left open)")
            self._log("Session stopped", session=s)
            self._save()
            return s

    def exit_now(self, sid: str) -> Session:
        with self.lock:
            s = self.sessions[sid]
            if s.position:
                self._exit(s, "manual exit", None)
                self._save()
            return s

    def remove_session(self, sid: str) -> None:
        with self.lock:
            s = self.sessions.get(sid)
            if s and s.position:
                raise ValueError("Close the position before removing the session")
            self.sessions.pop(sid, None)
            self._save()

    def stop_all(self, square_off: bool = True) -> int:
        n = 0
        with self.lock:
            for s in list(self.sessions.values()):
                if s.status != "stopped":
                    self.stop_session(s.id, square_off)
                    n += 1
        return n

    def status(self) -> dict:
        with self.lock:
            now = self.clock()
            return {"running": self.running, "market_open": self.market_open(now),
                    "now": now.strftime("%Y-%m-%d %H:%M:%S"), "square_off": ("%02d:%02d" % ENV_SQUARE_OFF) if ENV_SQUARE_OFF else "per strategy",
                    "sessions": [s.public() for s in sorted(self.sessions.values(), key=lambda x: x.created, reverse=True)],
                    "log": list(self.log)[-200:][::-1]}

    # ------------------------------------------------------------------ loop
    @staticmethod
    def market_open(now: datetime) -> bool:
        return now.weekday() < 5 and OPEN <= hm(now) < CLOSE

    def _run(self) -> None:
        self._log("Trading engine started")
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:  # never let the loop die
                log.exception("engine tick failed")
                self._log(f"Engine error: {exc}", "error")
            self._stop.wait(5)

    def tick(self) -> None:
        now = self.clock()
        today = now.strftime("%Y-%m-%d")
        with self.lock:
            active = [s for s in self.sessions.values() if s.status not in ("stopped", "error")]
            # new trading day: re-arm sessions that finished yesterday and reset per-day limits
            for s in active:
                if s.day != today:
                    s.day = today
                    keep = s.day_stats.get("cool_until") if not dp.is_intraday(tf_of(s)) else None
                    s.day_stats = {"cool_until": keep} if keep else {}      # bar cool-downs span days on daily+
                    if s.status == "done_for_day":
                        s.status, s.message = "waiting", "New day - waiting for the first signal"
            if now.weekday() >= 5 or not (OPEN <= hm(now) <= (CLOSE[0], CLOSE[1] + 10)):
                for s in active:
                    if s.status == "waiting":
                        s.message = "Market closed - waiting for the next session"
                return

            # 1) end-of-day square-off for intraday sessions (time depends on the strategy)
            changed = False
            for s in active:
                sq = square_off_for(s)
                if sq and s.params.get("square_off_eod", True) and hm(now) >= sq and s.day_stats.get("squared") != today:
                    if s.dry_run and s.position and not self._last_candle_in(s, now, sq):
                        continue        # dry run: price the exit at the candle that ends at the square-off time
                    s.day_stats["squared"] = today
                    if s.position:
                        self._exit(s, "end of day", None)
                    s.status, s.message = "done_for_day", f"Squared off at {sq[0]:02d}:{sq[1]:02d} - resumes next session"
                    changed = True
            if changed:
                self._save()

            # 2) fast exits / breakeven moves from Megabull LTP
            live_pos = [s for s in active if s.position and not s.dry_run]
            if live_pos and time.time() - self._last_ltp >= LTP_EVERY:
                self._last_ltp = time.time()
                self._check_ltp(live_pos, now)

            # 3) completed-candle processing, grouped by (data source, interval)
            groups: dict[tuple, list[Session]] = {}
            for s in active:
                if s.status in ("waiting", "in_position"):
                    groups.setdefault((s.source, tf_of(s)), []).append(s)
        for (source, tf), sess in groups.items():
            if dp.is_intraday(tf):
                self._process_group(now, source, tf, sess)
            else:
                self._process_daily_group(now, source, tf, sess)

    def _last_candle_in(self, s: Session, now: datetime, sq: tuple[int, int]) -> bool:
        minutes = dp.tf_minutes(tf_of(s)) or 15
        need = (now.replace(hour=sq[0], minute=sq[1], second=0, microsecond=0) - timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M")
        waited = (now - now.replace(hour=sq[0], minute=sq[1], second=0, microsecond=0)).total_seconds() > CANDLE_MAX_WAIT
        return bool(s.last_bar and s.last_bar >= need) or waited

    def _expected_bar(self, now: datetime, minutes: int) -> datetime | None:
        """Start time of the most recent candle that has fully closed (+ data delay)."""
        session_open = now.replace(hour=OPEN[0], minute=OPEN[1], second=0, microsecond=0)
        ref = now - timedelta(seconds=CANDLE_DELAY)
        elapsed = (ref - session_open).total_seconds() / 60
        if elapsed < minutes:
            return None
        k = int(elapsed // minutes) - 1
        start = session_open + timedelta(minutes=k * minutes)
        last_start = now.replace(hour=CLOSE[0], minute=CLOSE[1], second=0, microsecond=0) - timedelta(minutes=minutes)
        return min(start, last_start)

    def _process_daily_group(self, now: datetime, source: str, tf: str, sess: list[Session]) -> None:
        """Daily/weekly/monthly: once each trading morning (after the open + data delay), evaluate the last
        COMPLETED bar. Orders go out at market right away - the backtest's "next bar's open"."""
        open_t = now.replace(hour=OPEN[0], minute=OPEN[1], second=0, microsecond=0) + timedelta(seconds=CANDLE_DELAY)
        if now < open_t or hm(now) >= CLOSE:
            return
        key = (source, tf)
        today = now.strftime("%Y-%m-%d")
        if self._group_bar.get(key) == today:
            return
        tickers = sorted({s.ticker for s in sess})
        days = max(bte.live_days(bte.BACKTESTS[s.strategy], tf) for s in sess)
        try:
            data = self.candles_fn(tickers, tf, source, days)
        except Exception as exc:
            self._log(f"{dp.TIMEFRAMES[tf]['label']} bar download failed ({dp.PROVIDERS[source]['name']}): {exc}", "error")
            return
        self._group_bar[key] = today
        with self.lock:
            for s in sess:
                df = data.get(s.ticker)
                if df is None or df.empty:
                    self._log(f"No {dp.TIMEFRAMES[tf]['label']} bars from {dp.PROVIDERS[source]['name']}", "warn", s)
                    continue
                if df.index[-1].strftime("%Y-%m-%d %H:%M") == s.last_bar:
                    s.message = f"No new completed {dp.TIMEFRAMES[tf]['label'].replace('1 ', '')} bar yet" + (
                        "" if not s.position else " · holding position")
                    continue
                if s.status in ("waiting", "in_position"):
                    self._on_candle(s, df, now, stale=_is_stale(df.index[-1], tf, now))
            self._save()

    def _process_group(self, now: datetime, source: str, interval: str, sess: list[Session]) -> None:
        minutes = dp.tf_minutes(interval)
        bar = self._expected_bar(now, minutes)
        if bar is None:
            return
        key, bar_s = (source, interval), bar.strftime("%Y-%m-%d %H:%M")
        if self._group_bar.get(key) == bar_s:
            return
        tickers = sorted({s.ticker for s in sess})
        days = max(bte.live_days(bte.BACKTESTS[s.strategy], interval) for s in sess)
        try:
            data = self.candles_fn(tickers, interval, source, days)
        except Exception as exc:
            self._log(f"Candle download failed ({dp.PROVIDERS[source]['name']}): {exc}", "error")
            return
        latest = {t: df.index[-1].strftime("%Y-%m-%d %H:%M") for t, df in data.items() if len(df)}
        ready = [t for t in tickers if latest.get(t) == bar_s]
        late = (now - (bar + timedelta(minutes=minutes))).total_seconds() > CANDLE_MAX_WAIT
        if len(ready) < len(tickers) and not late:
            return  # data source hasn't published the candle yet - retry on the next tick
        self._group_bar[key] = bar_s
        if len(ready) < len(tickers):
            missing = [t.rsplit(".", 1)[0] for t in tickers if t not in ready]
            self._log(f"{bar_s} candle not available for {', '.join(missing)} after {CANDLE_MAX_WAIT}s - skipped", "warn")
        with self.lock:
            for s in sess:
                if s.ticker in ready and s.status in ("waiting", "in_position"):
                    self._on_candle(s, data[s.ticker], now)
            self._save()

    # ------------------------------------------------------------------ strategy
    def _on_candle(self, s: Session, df: pd.DataFrame, now: datetime, stale: bool = False) -> None:
        strat = bte.BACKTESTS[s.strategy]
        bar = df.iloc[-1]
        bar_t = df.index[-1]
        bar_s = bar_t.strftime("%Y-%m-%d %H:%M")
        s.last_bar, s.last_check = bar_s, now.strftime("%H:%M:%S")
        s.last_close = float(bar["Close"])
        if s.position:
            p = s.position
            if bar_s <= p["signal_bar"]:
                return
            side = p.get("side", 1)
            lo, hi, op = float(bar["Low"]), float(bar["High"]), float(bar["Open"])
            hit_stop = lo <= p["stop"] if side > 0 else hi >= p["stop"]
            hit_tgt = hi >= p["target"] if side > 0 else lo <= p["target"]
            stop_reason = "breakeven stop" if p.get("be") else "stop"
            entry_bar = bar_s[:10] == p["entry_time"][:10] if not dp.is_intraday(tf_of(s)) else False
            if not entry_bar and side * (op - p["stop"]) <= 0:          # opened through the stop
                self._exit(s, stop_reason + " (gap)", op, bar_t)
            elif not entry_bar and side * (op - p["target"]) >= 0:      # opened through the target
                self._exit(s, "target (gap)", op, bar_t)
            elif hit_stop and (not hit_tgt or s.params.get("stop_first", True)):
                self._exit(s, stop_reason, p["stop"], bar_t)
            elif hit_tgt:
                self._exit(s, "target", p["target"], bar_t)
            else:
                self._maybe_breakeven(s, hi if side > 0 else lo)
                if (strat.live_opposite and s.params.get("exit_on_opposite", False)
                        and strat.live_opposite(df, s.params, side)):
                    self._exit(s, "opposite signal", float(bar["Close"]), bar_t)
                    return                      # backtest: exit at the next open, no new entry on this candle
                if s.position:
                    p = s.position
                    s.message = (f"In {'long' if side > 0 else 'short'} · {bar_s[11:]} candle L {lo:.2f} / H {hi:.2f}"
                                 f" · stop ₹{p['stop']:.2f}{' (breakeven)' if p.get('be') else ''} · target ₹{p['target']:.2f}")
                    return
            if s.position or s.status != "waiting":
                return
        # ---- flat: look for a new entry on this candle ----
        if stale:
            s.message = (f"{bar_s[:10]} bar is from an earlier period - entries only on the first session after "
                         f"a bar completes (start sessions before the open)")
            return
        block = self._entry_block(s, bar_t)
        if block:
            s.message = f"{bar_s[11:]} candle checked - {block}"
            return
        sig = strat.live_signal(df, s.params)
        if not sig:
            s.message = f"{bar_s[11:]} candle checked - no signal (close {bar['Close']:.2f})"
            return
        self._enter(s, sig)

    def _entry_block(self, s: Session, bar_t) -> str | None:
        """Per-stock and account limits (same rules as the backtest). Returns a reason, or None if OK."""
        p, st = s.params, s.day_stats
        if st.get("cool_until") and bar_t.strftime("%Y-%m-%d %H:%M") < st["cool_until"]:
            return f"cooling down after a stop-out (until the {st['cool_until'][:16]} bar)"
        if not dp.is_intraday(tf_of(s)):
            return None                              # per-day limits are intraday-only (same as the backtest)
        mt = int(p.get("max_trades_per_stock", 0) or 0)
        if mt and st.get("entries", 0) >= mt:
            return f"daily limit of {mt} trades reached"
        mc = int(p.get("max_consec_losses", 0) or 0)
        if mc and st.get("consec", 0) >= mc:
            return f"stopped for today after {mc} losses in a row"
        if st.get("cool_until") and bar_t.strftime("%Y-%m-%d %H:%M") < st["cool_until"]:
            return f"cooling down after a stop-out (until the {st['cool_until'][11:]} candle)"
        dl = float(p.get("daily_loss_pct", 0) or 0)
        if dl > 0:
            today = self.clock().strftime("%Y-%m-%d")
            realized = sum(t["pnl"] for x in self.sessions.values() if x.dry_run == s.dry_run
                           for t in x.trades if t["exit_time"][:10] == today)
            if realized <= -float(p.get("capital", s.capital)) * dl / 100:
                if not st.get("loss_halt_logged"):
                    st["loss_halt_logged"] = True
                    self._log(f"Daily loss limit hit (realized ₹{realized:,.0f} ≤ −{dl}% of capital) - no new entries today", "warn", s)
                return "account daily loss limit reached"
        return None

    def _maybe_breakeven(self, s: Session, extreme: float) -> None:
        p = s.position
        if not p or p.get("be") or not s.params.get("breakeven", False):
            return
        side = p.get("side", 1)
        trigger = p["entry"] + side * p["risk"]
        if (extreme >= trigger) if side > 0 else (extreme <= trigger):
            p["stop"], p["be"] = p["entry"], True
            self._log(f"+1R reached (₹{trigger:.2f}) - stop moved to entry ₹{p['entry']:.2f}", session=s)

    def _enter(self, s: Session, sig: dict) -> None:
        strat = bte.BACKTESTS[s.strategy]
        side = int(sig.get("side", 1))
        open_now = sum(1 for x in self.sessions.values() if x.position)
        if open_now >= MAX_OPEN:
            self._log(f"Signal at {sig['bar_time'][11:]} skipped: {open_now} positions open (limit {MAX_OPEN})", "warn", s)
            return
        qty = strat.size(sig["entry"], sig["risk"], s.params) if strat.size else int(s.capital // sig["entry"])
        if qty < 1:
            self._log(f"Signal skipped: position size rounds to 0 shares at ₹{sig['entry']}", "warn", s)
            return
        verb = "BUY" if side > 0 else "SELL"
        fill, oid = sig["entry"], f"DRY-{uuid.uuid4().hex[:6]}"
        if not s.dry_run:
            try:
                res = self.client_factory().place_order(Order(s.ticker, verb, qty, product=product_of(s)))
            except MegabullError as exc:
                self._log(f"{verb} order failed: {exc}", "error", s)
                s.message = f"Last {verb} failed: {exc}"
                return
            oid = res["order_id"]
            if res.get("status") and "REJECT" in str(res["status"]).upper():
                self._log(f"{verb} rejected by Megabull: {res['response']}", "error", s)
                s.message = f"Last {verb} was rejected - see the log"
                return
            fill = self._fill_price(oid) or sig["entry"]
        fill = float(fill)
        stop = float(sig["stop"])
        if "rr" in sig:                                   # risk/target from the actual fill (like the backtest)
            risk = side * (fill - stop)
            target = fill + side * sig["rr"] * risk
        else:
            risk, target = float(sig["risk"]), float(sig["target"])
        s.day_stats["entries"] = s.day_stats.get("entries", 0) + 1
        s.position = {"side": side, "qty": qty, "entry": fill, "signal_entry": sig["entry"], "stop": stop,
                      "init_stop": stop, "target": target, "risk": risk, "be": False,
                      "entry_time": self.clock().strftime("%Y-%m-%d %H:%M:%S"), "order_id": oid,
                      "signal_bar": sig["bar_time"]}
        s.status = "in_position"
        word = "Bought" if side > 0 else "Sold short"
        s.message = f"{word} {qty} @ ₹{fill:.2f} · stop ₹{stop:.2f} · target ₹{target:.2f}"
        why = (f"close ₹{sig['entry']:.2f} > prev high ₹{sig.get('prev_high')}" if side > 0
               else f"close ₹{sig['entry']:.2f} < prev low ₹{sig.get('prev_low')}")
        self._log(f"{'[DRY RUN] ' if s.dry_run else ''}{verb} {qty} @ ₹{fill:.2f} ({why}"
                  f"{', VWAP ₹' + str(sig['vwap']) if sig.get('vwap') else ''}); stop ₹{stop:.2f}, target ₹{target:.2f}; order {oid}",
                  session=s)
        if risk <= 0:                                     # filled beyond the stop: get out immediately
            self._exit(s, "stop (gap)", None)

    def _exit(self, s: Session, reason: str, price_hint: float | None, bar_t=None) -> None:
        p = s.position
        if not p:
            return
        side = p.get("side", 1)
        verb = "SELL" if side > 0 else "BUY"
        fill, oid = price_hint or s.last_close or p["entry"], f"DRY-{uuid.uuid4().hex[:6]}"
        if not s.dry_run:
            try:
                res = self.client_factory().place_order(Order(s.ticker, verb, p["qty"], product=product_of(s)))
            except MegabullError as exc:
                self._log(f"{verb} to close ({reason}) failed: {exc} - position still open, will retry", "error", s)
                s.message = f"Exit failed ({reason}) - retrying"
                return
            oid = res["order_id"]
            fill = self._fill_price(oid) or price_hint or self._ltp(s) or p["entry"]
        fill = float(fill)
        pnl = side * (fill - p["entry"]) * p["qty"]
        r_mult = side * (fill - p["entry"]) / p["risk"] if p["risk"] else 0.0
        s.trades.append({"side": "long" if side > 0 else "short", "entry_time": p["entry_time"], "entry": round(p["entry"], 2),
                         "exit_time": self.clock().strftime("%Y-%m-%d %H:%M:%S"), "exit": round(fill, 2), "qty": p["qty"],
                         "stop": round(p.get("init_stop", p["stop"]), 2), "target": round(p["target"], 2), "reason": reason,
                         "pnl": round(pnl, 2), "r": round(r_mult, 3), "entry_order": p["order_id"], "exit_order": oid,
                         "dry_run": s.dry_run})
        # per-stock limits (mirrors the backtest)
        st = s.day_stats
        st["consec"] = st.get("consec", 0) + 1 if r_mult < -1e-9 else 0
        if reason.startswith(("stop", "breakeven")):
            cd = int(s.params.get("cooldown_candles", 0) or 0)
            if cd:
                tf = tf_of(s)
                ref = bar_t if bar_t is not None else _period_start(self.clock(), tf)
                st["cool_until"] = _add_bars(pd.Timestamp(ref), tf, cd).strftime("%Y-%m-%d %H:%M")
        s.position = None
        s.status = "waiting"
        s.message = f"Exited {'long' if side > 0 else 'short'} ({reason}) @ ₹{fill:.2f}: {'+' if pnl >= 0 else '−'}₹{abs(pnl):,.0f}"
        self._log(f"{'[DRY RUN] ' if s.dry_run else ''}{verb} {p['qty']} @ ₹{fill:.2f} to close {'long' if side > 0 else 'short'}"
                  f" ({reason}); P&L {'+' if pnl >= 0 else '−'}₹{abs(pnl):,.2f} ({r_mult:+.2f}R); order {oid}", session=s)

    def _fill_price(self, order_id: Any) -> float | None:
        if order_id is None:
            return None
        client = self.client_factory()
        for _ in range(3):
            try:
                f = client.order_fill(order_id)
                if f and f.get("avg_price"):
                    return float(f["avg_price"])
            except Exception:
                pass
            time.sleep(0.7)
        return None

    def _ltp(self, s: Session) -> float | None:
        try:
            return self.client_factory().ltp_map().get(s.symbol.upper())
        except Exception:
            return None

    def _check_ltp(self, sessions: list[Session], now: datetime | None = None) -> None:
        try:
            ltps = self.client_factory().ltp_map()
        except Exception as exc:
            log.debug("LTP check failed: %s", exc)
            return
        changed = False
        for s in sessions:
            ltp = ltps.get(s.symbol.upper())
            if ltp is None or not s.position:
                continue
            p = s.position
            side = p.get("side", 1)
            if side * (ltp - p["stop"]) <= 0:
                self._exit(s, "breakeven stop" if p.get("be") else "stop", ltp); changed = True
            elif side * (ltp - p["target"]) >= 0:
                self._exit(s, "target", ltp); changed = True
            else:
                self._maybe_breakeven(s, ltp)
                p = s.position
                s.message = (f"In {'long' if side > 0 else 'short'} · LTP ₹{ltp:.2f} (stop ₹{p['stop']:.2f}"
                             f"{' breakeven' if p.get('be') else ''}, target ₹{p['target']:.2f})")
        if changed:
            self._save()


def _candle_start(now: datetime, minutes: int) -> datetime:
    """Start of the candle currently in progress."""
    session_open = now.replace(hour=OPEN[0], minute=OPEN[1], second=0, microsecond=0)
    k = max(0, int((now - session_open).total_seconds() // (minutes * 60)))
    return session_open + timedelta(minutes=k * minutes)


def tf_of(s: "Session") -> str:
    return bte.interval_of(bte.BACKTESTS[s.strategy], s.params)


def product_of(s: "Session") -> str:
    """Intraday timeframes trade MIS; daily/weekly/monthly hold overnight, so CNC (delivery)."""
    return "MIS" if dp.is_intraday(tf_of(s)) else "CNC"


def _is_stale(bar_start: pd.Timestamp, tf: str, now: datetime) -> bool:
    """A daily/weekly/monthly signal is only actionable on the first session after its bar completed
    (that's the backtest's "next bar's open"); one extra business day is allowed for a market holiday."""
    end = _add_bars(pd.Timestamp(bar_start).normalize(), tf, 1) if tf != "1d" else pd.Timestamp(bar_start).normalize() + pd.Timedelta(days=1)
    first_session = end + pd.offsets.BDay(0)
    return pd.Timestamp(now.date()) > first_session + pd.offsets.BDay(1)


def _period_start(now: datetime, tf: str) -> pd.Timestamp:
    """Start of the bar currently forming (used for cool-downs after an LTP-triggered exit)."""
    m = dp.tf_minutes(tf)
    if m:
        return pd.Timestamp(_candle_start(now, m))
    d = pd.Timestamp(now.date())
    if tf == "1wk":
        return d - pd.Timedelta(days=d.weekday())
    if tf == "1mo":
        return d.replace(day=1)
    return d


def _add_bars(ts: pd.Timestamp, tf: str, n: int) -> pd.Timestamp:
    m = dp.tf_minutes(tf)
    if m:
        return ts + pd.Timedelta(minutes=m * n)
    if tf == "1d":
        return ts + pd.offsets.BDay(n)
    if tf == "1wk":
        return ts + pd.Timedelta(weeks=n)
    return ts + pd.DateOffset(months=n)


def square_off_for(s: "Session") -> tuple[int, int] | None:
    """None for daily+ (held across days). Else env TRADE_SQUAREOFF_TIME, the strategy's time, or 15:20."""
    if not dp.is_intraday(tf_of(s)):
        return None
    if ENV_SQUARE_OFF:
        return ENV_SQUARE_OFF
    return bte.BACKTESTS[s.strategy].square_off or (15, 20)
