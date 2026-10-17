"""oil-pair dashboard: every pair's position, spread and running profitability.

    ./.venv/bin/streamlit run dashboard/app.py

Kept running by launchd (scripts/install_dashboard.sh) at http://localhost:8501.
Read-only: it never places or closes a trade. See dashboard/figures.py for
where each figure comes from.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "dashboard"))

import figures  # noqa: E402
from oil_pair.settings import load_pair_config  # noqa: E402
from oil_pair.state_store import load_state  # noqa: E402
from oil_pair.strategy_logic import PairSide  # noqa: E402

UK = "Europe/London"
IG_REFRESH_SECONDS = 300      # IG is one request budget all the pairs share
PAGE_REFRESH_SECONDS = 30
TOTAL = "All pairs"

st.set_page_config(page_title="oil-pair", page_icon="📈", layout="wide")


@st.cache_resource
def _env():
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    return True


@st.cache_data(ttl=IG_REFRESH_SECONDS, show_spinner="Reading IG...")
def ig_figures() -> tuple[list, dict, pd.Timestamp]:
    """Closed trades since 5 Oct and open positions, from IG."""
    _env()
    import pair_status
    import pnl_spreadsheet
    pairs = pnl_spreadsheet.load_pairs()
    trades = [t for t in pnl_spreadsheet.closed_trades(pairs) if t.opened >= pnl_spreadsheet.TRADES_FROM]
    statuses, _ = pair_status.gather(dry_run=True)
    return trades, {s.name: s for s in statuses}, pd.Timestamp.now(tz="UTC")


@st.cache_data(ttl=PAGE_REFRESH_SECONDS - 5)
def local_figures(pair: str, epics: tuple[str, str]):
    log = figures.read_log(ROOT / "logs" / pair / "oil_pair.log", pd.Timestamp.now(tz="UTC"))
    mids = figures.read_mids(ROOT / "price_log" / pair / "ticks.csv", list(epics))
    return log, mids, figures.is_running(pair)


def money(v: float | None) -> str:
    return "" if v is None or pd.isna(v) else f"{'-' if v < 0 else ''}£{abs(v):,.2f}"


def ago(ts: pd.Timestamp | None, now: pd.Timestamp) -> str:
    if ts is None or pd.isna(ts):
        return "never"
    minutes = (now - ts).total_seconds() / 60
    return f"{minutes:.0f} min ago" if minutes < 120 else f"{minutes / 60:.0f} h ago"


@st.fragment(run_every=PAGE_REFRESH_SECONDS)
def page() -> None:
    now = pd.Timestamp.now(tz="UTC")
    try:
        trades, statuses, ig_at = ig_figures()
    except Exception as exc:  # noqa: BLE001 - show the page with what's local
        st.error(f"Couldn't read IG just now ({exc}) - showing local figures only.")
        trades, statuses, ig_at = [], {}, None

    import pnl_spreadsheet
    start = pnl_spreadsheet.TRADES_FROM.tz_convert("UTC")
    pairs = {p.parent.name: load_pair_config(p) for p in sorted((ROOT / "config").glob("*/pair_config.toml"))}

    rows, curves, views = [], {}, {}
    for name, cfg in pairs.items():
        epics = (cfg.instrument_a.epic, cfg.instrument_b.epic)
        names = {cfg.instrument_a.name: epics[0], cfg.instrument_b.name: epics[1]}
        log, mids, running = local_figures(name, epics)
        mine = [t for t in trades if t.pair == name]
        status = statuses.get(name)
        open_legs = [figures.OpenLeg(leg.epic, leg.size * (1 if leg.direction == "BUY" else -1), leg.open_level, leg.opened)
                     for leg in (status.legs if status else ())]
        curves[name] = figures.running_pnl(mine, open_legs, mids, names, start, now)
        views[name] = (cfg, log, mine, status)
        state = load_state(ROOT / "state" / name / "run_state.json")
        paired = [t for t in mine if len(t.markets) == 2]
        banked = sum(t.net for t in mine)
        open_pnl = status.net if status else 0.0
        z = log.spread["z"].iloc[-1] if len(log.spread) else None
        s = cfg.strategy
        if status:
            position = f"{status.side} since {status.opened.tz_convert(UK):%d %b %H:%M}"
        else:
            position = "flat" if state is None or state.side is PairSide.FLAT else state.side.value
        rows.append({
            "Pair": name, "Account": (status.account if status else (state.account if state else None) or "demo").upper(),
            "Position": position, "Spread now (std)": z,
            "Entry / take profit / stop": f"±{s.entry_stds:g} / ±{s.limit_stds:g} / ±{s.stop_stds:g}",
            "Open P&L after costs": open_pnl if status else None, "Banked since 5 Oct": banked,
            "Total": banked + open_pnl, "Closed trades": len(paired),
            "Win rate": (sum(t.net > 0 for t in paired) / len(paired)) if paired else None,
            "Process": "running" if running else "STOPPED",
            "Last log line": ago(log.last_line_at, now), "Errors (24h)": len(log.recent_errors),
        })
    table = pd.DataFrame(rows)

    # --- headline ---
    account = "DEMO" if set(table["Account"]) == {"DEMO"} else "/".join(sorted(set(table["Account"])))
    st.title(f"oil-pair · {account}")
    st.caption(
        f"Page updated {now.tz_convert(UK):%a %d %b %H:%M:%S} UK · IG figures from "
        f"{ig_at.tz_convert(UK):%H:%M:%S} (every {IG_REFRESH_SECONDS // 60} min)" if ig_at is not None else
        f"Page updated {now.tz_convert(UK):%H:%M:%S} UK"
    )
    banked, open_pnl = table["Banked since 5 Oct"].sum(), table["Open P&L after costs"].fillna(0).sum()
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Banked since 5 Oct", money(banked))
    c2.metric("Open P&L after costs", money(open_pnl))
    c3.metric("Total", money(banked + open_pnl))
    c4.metric("Open pairs", f"{table['Open P&L after costs'].notna().sum()} of {len(table)}")
    stopped = table[table["Process"] == "STOPPED"]["Pair"].tolist()
    if stopped:
        st.warning(f"Not running: {', '.join(stopped)}")

    # --- pairs ---
    st.dataframe(
        table, hide_index=True, width="stretch",
        column_config={
            "Spread now (std)": st.column_config.NumberColumn(format="%+.2f"),
            "Open P&L after costs": st.column_config.NumberColumn(format="£%.2f"),
            "Banked since 5 Oct": st.column_config.NumberColumn(format="£%.2f"),
            "Total": st.column_config.NumberColumn(format="£%.2f"),
            "Win rate": st.column_config.NumberColumn(format="percent"),
        },
    )

    # --- running profitability ---
    st.subheader("Running profitability since 5 Oct")
    fig = go.Figure()
    total = sum(curves.values())
    for name, curve in curves.items():
        if curve.abs().sum() > 0:
            fig.add_scatter(x=curve.index, y=curve.values, name=name, mode="lines", line_shape="hv")
    fig.add_scatter(x=total.index, y=total.values, name=TOTAL, mode="lines", line=dict(color="black", width=3))
    fig.update_layout(height=420, yaxis_title="£ after costs (open trades at mid)", hovermode="x unified",
                      margin=dict(l=10, r=10, t=10, b=10), legend=dict(orientation="h"))
    st.plotly_chart(fig, width="stretch")

    # --- one pair in detail ---
    st.subheader("Pair detail")
    open_first = [r["Pair"] for r in rows if r["Open P&L after costs"] is not None] + [r["Pair"] for r in rows]
    left, right = st.columns([2, 1])
    choice = left.selectbox("Pair", list(dict.fromkeys(open_first)), label_visibility="collapsed")
    days = right.slider("Days of spread history", 1, 30, 7)
    cfg, log, mine, status = views[choice]
    s = cfg.strategy
    spread = log.spread[log.spread.index >= now - pd.Timedelta(days=days)]

    zf = go.Figure()
    zf.add_scatter(x=spread.index, y=spread["z"], name="spread (std)", mode="lines", line=dict(color="#1f77b4"))
    for level, label, colour in ((s.entry_stds, "entry", "orange"), (s.limit_stds, "take profit", "green"),
                                 (s.stop_stds, "stop", "red")):
        for sign in ((1, -1) if level else (1,)):
            zf.add_hline(y=sign * level, line=dict(color=colour, dash="dash", width=1),
                         annotation_text=label if sign > 0 else None, annotation_position="top left")
    shown_from = spread.index.min() if len(spread) else now
    for t in mine:
        for at, label, symbol in ((t.opened, "opened", "triangle-up"), (t.closed, f"closed {money(t.net)}", "x")):
            if len(spread) and at >= shown_from:
                near = spread["z"].reindex([at], method="nearest")
                zf.add_scatter(x=[at], y=near.values, mode="markers", marker=dict(symbol=symbol, size=11),
                               name=f"{t.side} {label}", showlegend=False, hovertext=label)
    if status is not None and len(spread) and status.opened >= spread.index.min():
        near = spread["z"].reindex([status.opened], method="nearest")
        zf.add_scatter(x=[status.opened], y=near.values, mode="markers", showlegend=False,
                       marker=dict(symbol="triangle-up", size=13, color="black"), hovertext="open now")
    zf.update_layout(height=380, yaxis_title="spread, std from zero", margin=dict(l=10, r=10, t=10, b=10),
                     showlegend=False)
    st.plotly_chart(zf, width="stretch")

    a, b = st.columns(2)
    with a:
        st.caption(f"{choice}: running profitability")
        curve = curves[choice]
        pf = go.Figure(go.Scatter(x=curve.index, y=curve.values, mode="lines", line_shape="hv",
                                  line=dict(color="black")))
        pf.update_layout(height=260, margin=dict(l=10, r=10, t=10, b=10), yaxis_title="£")
        st.plotly_chart(pf, width="stretch")
    with b:
        if status is not None:
            st.caption(f"{choice}: open now")
            st.write(f"**{status.side}** since {status.opened.tz_convert(UK):%a %d %b %H:%M} UK")
            for leg in status.legs:
                st.write(f"{leg.direction} £{leg.size:.2f}/pt {leg.name}: opened {leg.open_level:g}, now {leg.mid:g}")
            fin = f"financing {'received' if status.financing >= 0 else 'paid'} {money(abs(status.financing))}"
            st.write(f"Gross {money(status.gross)} · costs {money(status.costs)} (bid/offer "
                     f"{money(status.spread_cost)}, {fin}) · **after costs {money(status.net)}**")
        if log.recent_errors:
            with st.expander(f"{len(log.recent_errors)} error(s) in the last 24 hours"):
                st.code("\n".join(log.recent_errors), language=None)

    if mine:
        st.caption(f"{choice}: closed trades since 5 Oct")
        st.dataframe(pd.DataFrame([{
            "Opened (UK)": t.opened.tz_convert(UK).strftime("%d %b %H:%M"),
            "Closed (UK)": t.closed.tz_convert(UK).strftime("%d %b %H:%M"),
            "Side": t.side, "Exit": t.exit, "Realised (IG)": t.realised, "Financing": t.financing,
            "After costs": t.net, "Note": t.note,
        } for t in sorted(mine, key=lambda t: t.closed, reverse=True)]), hide_index=True, width="stretch",
            column_config={k: st.column_config.NumberColumn(format="£%.2f")
                           for k in ("Realised (IG)", "Financing", "After costs")})

    st.caption("Banked figures are IG's own (realised P&L plus financing and admin fees). Open trades are "
               "marked at mid prices from the pairs' 15-second price logs, so the line is an estimate until "
               "IG books the close. Read-only: this page never trades.")


page()
