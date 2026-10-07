"""
Browser UI for the Dhan screener: manage the access token, edit / save / run configurations.

    pip install -r requirements.txt
    streamlit run app.py
"""
from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

import dhan_client as dc
import dhan_screener as ds

HERE = Path(__file__).resolve().parent
REPORTS = HERE / "reports"
NEW = "➕ New configuration"

st.set_page_config(page_title="Dhan Screener", page_icon="📈", layout="wide")

# ════════════════════════════ state helpers ═══════════════════════════════════
# Every form field has a session_state key "f_<name>"; weights are "w_<factor>" (in %),
# turnover floors are "t_<cap>". Presets are loaded into these keys via callbacks.


def _configs() -> dict[str, dict]:
    try:
        return ds.load_configs()
    except Exception as e:                                   # malformed JSON shouldn't kill the UI
        st.error(f"Could not read configs.json: {e}")
        return {}


def _put_mode_defaults(mode: str) -> None:
    base = ds.MODES[mode]
    for f in ds.FACTORS:
        st.session_state[f"w_{f}"] = round(base["weights"].get(f, 0) * 100, 1)
    for c in ds.CAPS:
        st.session_state[f"t_{c}"] = float(base["min_turnover_cr"][c])
    st.session_state.f_stop_atr = float(base["stop_atr"])


def load_into_form(preset: dict) -> None:
    cfg = ds.resolve_config(preset)
    s = st.session_state
    for k in ("mode", "cap", "live", "demo", "refresh", "exclude_asm_gsm"):
        s[f"f_{k}"] = cfg[k]
    s.f_top = int(cfg["top"])
    s.f_capital = float(cfg["capital"])
    s.f_risk_pct = float(cfg["risk_pct"])
    s.f_min_price = float(cfg["min_price"])
    s.f_stop_atr = float(cfg["stop_atr"])
    s.f_index_dir = cfg["index_dir"] or ""
    s.f_amfi = cfg["amfi"] or ""
    s.f_description = preset.get("description", "")
    for f in ds.FACTORS:
        s[f"w_{f}"] = round(cfg["weights"].get(f, 0) * 100, 1)
    for c in ds.CAPS:
        s[f"t_{c}"] = float(cfg["min_turnover_cr"][c])


def on_pick_config() -> None:
    name = st.session_state.picked
    if name == NEW:
        load_into_form({"mode": st.session_state.get("f_mode", "swing")})
        st.session_state.f_description = ""
    else:
        load_into_form(_configs().get(name, {}))


def on_mode_change() -> None:
    _put_mode_defaults(st.session_state.f_mode)


def form_config() -> dict:
    """The configuration currently in the form, in the same shape as configs.json entries."""
    s = st.session_state
    cfg = {
        "description": s.f_description.strip(),
        "mode": s.f_mode, "cap": s.f_cap, "top": int(s.f_top),
        "capital": float(s.f_capital), "risk_pct": float(s.f_risk_pct),
        "live": bool(s.f_live), "demo": bool(s.f_demo),
        "min_price": float(s.f_min_price), "exclude_asm_gsm": bool(s.f_exclude_asm_gsm),
        "stop_atr": float(s.f_stop_atr),
        "weights": {f: round(s[f"w_{f}"] / 100, 4) for f in ds.FACTORS if s[f"w_{f}"] > 0},
        "min_turnover_cr": {c: float(s[f"t_{c}"]) for c in ds.CAPS},
    }
    if s.f_index_dir.strip():
        cfg["index_dir"] = s.f_index_dir.strip()
    if s.f_amfi.strip():
        cfg["amfi"] = s.f_amfi.strip()
    return cfg


if "picked" not in st.session_state:
    saved = _configs()
    st.session_state.picked = next(iter(saved), NEW)
    load_into_form(saved.get(st.session_state.picked, {}))

# After "save as" / delete, switch the selection here, before the selectbox renders.
if "pending_pick" in st.session_state:
    st.session_state.picked = st.session_state.pop("pending_pick")
    on_pick_config()

# ════════════════════════════ sidebar: credentials ════════════════════════════

with st.sidebar:
    st.header("Dhan access")
    ts = dc.token_status()
    if not ts["env_exists"]:
        st.warning("No .env file yet. Save your client ID and token below to create it.")
    elif not ts["has_token"]:
        st.error("No access token in .env.")
    elif ts["likely_expired"]:
        st.error(f"Token saved {ts['age_hours']:.0f}h ago. Dhan tokens last 24h, so it has probably expired.")
    elif ts["age_hours"] is not None:
        left = dc.TOKEN_TTL_HOURS - ts["age_hours"]
        st.success(f"Token {ts['token_hint']} saved {ts['age_hours']:.1f}h ago (about {left:.0f}h left).")
    else:
        st.info(f"Token {ts['token_hint']} found in .env (save time unknown).")
    st.caption(f"Client ID: {ts['client_id'] or '—'}  \n`{ts['env_file']}`")

    with st.form("token_form", clear_on_submit=True):
        cid = st.text_input("Client ID", value=ts["client_id"])
        tok = st.text_input("New access token", type="password",
                            help="web.dhan.co → My Profile → Access DhanHQ APIs → generate token")
        if st.form_submit_button("Save to .env", width="stretch"):
            if not tok.strip():
                st.warning("Paste a token first.")
            else:
                dc.save_token(tok, cid or None)
                st.toast("Token saved to .env")
                st.rerun()

    if st.button("Test token", width="stretch", disabled=not ts["has_token"]):
        try:
            prof = dc.DhanClient().profile()
            st.success("Token works.")
            st.json({k: v for k, v in prof.items() if k in
                     ("dhanClientId", "tokenValidity", "activeSegment", "dataPlan", "dataValidity")} or prof)
        except Exception as e:
            st.error(str(e))

    st.divider()
    st.caption("Demo configurations run on synthetic data and need no token.")

# ════════════════════════════ main: configuration form ═══════════════════════

st.title("Dhan screener")

configs = _configs()
top_l, top_r = st.columns([3, 2])
with top_l:
    st.selectbox("Configuration", [*configs, NEW], key="picked", on_change=on_pick_config)
with top_r:
    st.text_input("Description", key="f_description", placeholder="What this configuration is for")

c1, c2, c3, c4 = st.columns(4)
with c1:
    st.radio("Mode", list(ds.MODES), key="f_mode", horizontal=True, on_change=on_mode_change,
             help="Switching mode resets weights, turnover floors and stop to that mode's defaults.")
    st.selectbox("Cap bucket", ["all", *ds.CAPS], key="f_cap",
                 format_func=lambda c: "All buckets" if c == "all" else ds.CAP_LABEL[c])
with c2:
    st.number_input("Top N per bucket", 1, 100, key="f_top")
    st.number_input("Capital (₹)", 1000.0, 1e9, step=10_000.0, key="f_capital", format="%.0f")
with c3:
    st.number_input("Risk per trade (%)", 0.05, 10.0, step=0.25, key="f_risk_pct")
    st.number_input("Stop (× ATR)", 0.1, 10.0, step=0.25, key="f_stop_atr")
with c4:
    st.toggle("Demo data (no token)", key="f_demo")
    st.toggle("Live quote overlay", key="f_live", disabled=st.session_state.f_demo,
              help="Adds gap %, today's change and live relative volume. Use during market hours.")
    st.toggle("Refresh history cache", key="f_refresh",
              help="Price history is cached per day. Turn on to re-download today's data.")

with st.expander("Factor weights", expanded=False):
    st.caption("Points each factor can add, as a share of 100. Weights are normalised, so they "
               "don't have to sum to 100. Set 0 to turn a factor off.")
    wcols = st.columns(3)
    for i, f in enumerate(ds.FACTORS):
        with wcols[i % 3]:
            st.number_input(ds.FACTOR_LABEL[f], 0.0, 100.0, step=5.0, key=f"w_{f}")
    total = sum(st.session_state[f"w_{f}"] for f in ds.FACTORS)
    if total <= 0:
        st.error("Give at least one factor a weight above zero.")
    else:
        mix = {ds.FACTOR_LABEL[f]: st.session_state[f"w_{f}"] / total * 100
               for f in ds.FACTORS if st.session_state[f"w_{f}"] > 0}
        st.caption("Effective mix: " + ", ".join(f"{k} {v:.0f}%" for k, v in mix.items()))

with st.expander("Filters and data sources", expanded=False):
    fc = st.columns(4)
    for i, c in enumerate(ds.CAPS):
        with fc[i]:
            st.number_input(f"Min turnover, {c} (₹ Cr/day)", 0.0, 10_000.0, step=5.0, key=f"t_{c}")
    with fc[3]:
        st.number_input("Min price (₹)", 0.0, 100_000.0, step=10.0, key="f_min_price")
    st.checkbox("Exclude stocks under ASM/GSM surveillance", key="f_exclude_asm_gsm")
    dcol1, dcol2 = st.columns(2)
    with dcol1:
        st.text_input("Index CSV folder (optional)", key="f_index_dir",
                      help="Folder with the three NSE index CSVs, if niftyindices.com is blocked.")
    with dcol2:
        st.text_input("AMFI category CSV (optional)", key="f_amfi",
                      help="symbol,category CSV to override the cap buckets.")

# ── actions ──────────────────────────────────────────────────────────────────
a1, a2, a3, a4 = st.columns([1.2, 1, 2, 1])
run_clicked = a1.button("▶ Run screener", type="primary", width="stretch", disabled=total <= 0)
is_saved = st.session_state.picked in configs

if a2.button("Save", width="stretch", disabled=not is_saved or total <= 0,
             help="Overwrite the selected configuration"):
    configs[st.session_state.picked] = form_config()
    ds.save_configs(configs)
    st.toast(f"Saved “{st.session_state.picked}”")

with a3.popover("Save as new…", width="stretch", disabled=total <= 0):
    new_name = st.text_input("Name", placeholder="e.g. Swing - midcap breakouts")
    if st.button("Save configuration", disabled=not new_name.strip()):
        name = new_name.strip()
        if name in configs:
            st.error("A configuration with that name exists. Pick it and use Save to overwrite.")
        else:
            configs[name] = form_config()
            ds.save_configs(configs)
            st.session_state.pending_pick = name
            st.rerun()

with a4.popover("Delete", width="stretch", disabled=not is_saved):
    st.write(f"Delete “{st.session_state.picked}”?")
    if st.button("Yes, delete", type="primary"):
        configs.pop(st.session_state.picked, None)
        ds.save_configs(configs)
        st.session_state.pending_pick = next(iter(configs), NEW)
        st.rerun()

# ════════════════════════════ run + results ═══════════════════════════════════

if run_clicked:
    name = st.session_state.picked if is_saved else None
    lines: list[str] = []
    with st.status("Running screener…", expanded=True) as status:
        def log(msg: str) -> None:
            lines.append(msg)
            status.write(msg)
        try:
            res = ds.run_screener(form_config(), log=log, name=name)
            REPORTS.mkdir(exist_ok=True)
            stem = f"{dt.datetime.now():%Y%m%d_%H%M}_{re.sub(r'[^A-Za-z0-9]+', '_', name or res['config']['mode']).strip('_')}"
            (REPORTS / f"{stem}.html").write_text(res["html"], encoding="utf-8")
            res["table"].to_csv(REPORTS / f"{stem}.csv")
            res.update(log=lines, stem=stem, name=name or "Unsaved configuration")
            st.session_state.result = res
            status.update(label=f"Done. Report saved to reports/{stem}.html", state="complete", expanded=False)
        except Exception as e:
            status.update(label="Run failed", state="error", expanded=True)
            st.error(str(e))
            if "401" in str(e) or "token" in str(e).lower():
                st.info("Paste a fresh access token in the sidebar and run again.")

res = st.session_state.get("result")
if res:
    st.divider()
    cfg, stats = res["config"], res["stats"]
    st.subheader(f"{res['name']} · {stats['date']}")
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Scanned", stats["scanned"])
    m2.metric("Passed filters", stats["passed"])
    m3.metric("Mode", cfg["mode"].title() + (" (demo)" if cfg["demo"] else ""))
    m4.metric("Picks shown", sum(len(d) for d in res["picks"].values()))

    tab_report, tab_table, tab_all, tab_log = st.tabs(["Report", "Picks table", "All scores", "Log"])
    with tab_report:
        n_rows = sum(len(d) for d in res["picks"].values())
        components.html(res["html"], height=min(4000, 420 + 190 * n_rows), scrolling=True)
    with tab_table:
        for cap, df in res["picks"].items():
            st.markdown(f"**{ds.CAP_LABEL[cap]}**")
            show = df.assign(**{k: df[k].map(" · ".join) for k in ("setup", "why", "watch")}) \
                     .drop(columns=["breakdown", "cap"])
            st.dataframe(show, width="stretch")
    with tab_all:
        st.dataframe(res["table"], width="stretch", height=520)
    with tab_log:
        st.code("\n".join(res["log"]) or "(no log)")

    d1, d2, _ = st.columns([1, 1, 3])
    d1.download_button("Download report (HTML)", res["html"], f"{res['stem']}.html", "text/html",
                       width="stretch")
    d2.download_button("Download scores (CSV)", res["table"].to_csv().encode("utf-8"), f"{res['stem']}.csv",
                       "text/csv", width="stretch")
