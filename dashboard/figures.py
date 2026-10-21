"""What the dashboard shows, worked out from the pairs' own files and IG.

Local files, re-read every refresh (cheap):
  logs/<pair>/oil_pair.log   the spread in std once a minute, as the pair
                             saw it with its live model; recent errors
  price_log/<pair>/ticks.csv the 15-second mid prices, for marking a trade
                             to market while it is open
  state/<pair>/run_state.json  whether the pair is open

IG, every few minutes (it is one request budget all the pairs share):
  closed trades with their financing (oil_pair.ledger, as the spreadsheet)
  open positions with P&L after costs (scripts/pair_status.py)

The earnings-miss shorts (earnings_short/) come from their book
(state/earnings_short/book.json), IG's open positions and its transaction
history for their funding, and their log for the last run.

A pair's running profitability is its banked P&L after costs (IG's
figures, stepping up or down as each trade closes) plus the open trade
marked to market at mid prices - an estimate until IG books the close.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path

import pandas as pd

from oil_pair.ledger import Trade

UK = "Europe/London"
_SPREAD = re.compile(
    r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ INFO __main__: spread=(\S+) \((\S+) std\) .*?side=PairSide\.(\w+)"
)
_LEVEL = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ (ERROR|CRITICAL) ")
_ENTER = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ INFO __main__: ENTER PairSide\.(\w+)")
# The close message's subject, as notify logs it: "Closed SHORT pair (stop loss): -£175.17 after costs"
_CLOSED = re.compile(
    r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ INFO oil_pair\.notify: (?:emailed|sent WhatsApp) "
    r"'Closed (\w+) pair \(([^)]*)\): ([+-])£([\d,]+\.\d\d)( after costs| before financing)?'"
)


@dataclass(frozen=True)
class LoggedClose:
    """A close as the pair's own close message reported it - used until IG's
    transaction history, which can lag by hours or a weekend, records it."""

    opened: pd.Timestamp | None
    closed: pd.Timestamp
    side: str
    exit: str
    net: float               # after costs when after_costs, else before financing
    after_costs: bool


@dataclass(frozen=True)
class LogView:
    spread: pd.DataFrame          # index UTC time; columns z, side
    last_line_at: pd.Timestamp | None
    recent_errors: list[str]      # ERROR/CRITICAL lines from the last 24 hours
    closes: tuple = ()            # LoggedClose, oldest first


def read_log(path: Path, now: pd.Timestamp, rotated: int = 1) -> LogView:
    """The pair's spread history and health from its log (and `rotated` older files)."""
    files = [p for p in [path.with_name(f"{path.name}.{i}") for i in range(rotated, 0, -1)] + [path] if p.exists()]
    rows, errors, last, enters, closes = [], [], None, [], []
    since = (now - pd.Timedelta(hours=24)).tz_convert(UK).strftime("%Y-%m-%d %H:%M:%S")
    for f in files:
        with f.open(errors="replace") as handle:
            for line in handle:
                if m := _SPREAD.match(line):
                    rows.append((m.group(1), float(m.group(3)), m.group(4)))
                elif (m := _LEVEL.match(line)) and m.group(1) >= since:
                    errors.append(line.strip())
                elif m := _ENTER.match(line):
                    enters.append(m.group(1))
                elif m := _CLOSED.match(line):
                    amount = float(m.group(5).replace(",", "")) * (-1 if m.group(4) == "-" else 1)
                    opened = max((e for e in enters if e <= m.group(1)), default=None)
                    closes.append((opened, m.group(1), m.group(2), m.group(3), amount, m.group(6) == " after costs"))
                if line[:4].isdigit():
                    last = line[:19]
    spread = pd.DataFrame(rows, columns=["at", "z", "side"])
    spread["at"] = _uk(spread["at"])
    # the hour the clocks go back repeats, so its times can't be placed: dropped
    spread = spread.dropna(subset=["at"]).drop_duplicates("at", keep="last").set_index("at").sort_index()
    logged = {}
    for opened, closed, side, exit_, amount, after_costs in closes:   # email and WhatsApp both log it: once each
        at = _uk(pd.Series([closed]))[0]
        if pd.isna(at) or any(abs(at - k) <= pd.Timedelta(minutes=2) for k in logged):
            continue
        logged[at] = LoggedClose(_uk(pd.Series([opened]))[0] if opened else None, at, side, exit_, amount, after_costs)
    return LogView(spread, _uk(pd.Series([last]))[0] if last else None, errors[-20:], tuple(logged.values()))


def not_yet_in_ig(closes: tuple, trades: list, since: pd.Timestamp) -> list:
    """Logged closes of trades opened since `since` (as the IG trades shown are)
    that IG's history doesn't have yet: no IG trade closed within five minutes."""
    return [c for c in closes if (c.opened if c.opened is not None else c.closed) >= since
            and not any(abs(t.closed - c.closed) <= pd.Timedelta(minutes=5) for t in trades)]


def _uk(text: pd.Series) -> pd.Series:
    return pd.to_datetime(text).dt.tz_localize(UK, ambiguous="NaT", nonexistent="NaT").dt.tz_convert("UTC")


def read_mids(path: Path, epics: list[str], freq: str = "15min") -> pd.DataFrame:
    """The pair's mid prices, last in each `freq` interval, both legs carried forward."""
    if not path.exists():
        return pd.DataFrame(columns=epics)
    ticks = pd.read_csv(path)
    ticks["timestamp"] = pd.to_datetime(ticks["timestamp"], utc=True, format="ISO8601")
    wide = ticks.pivot_table(index="timestamp", columns="epic", values="mid", aggfunc="last")
    return wide.reindex(columns=epics).resample(freq).last().ffill()


@dataclass(frozen=True)
class OpenLeg:
    epic: str
    size: float           # signed: + bought, - sold
    open_level: float
    opened: pd.Timestamp


def running_pnl(
    trades: list[Trade], open_legs: list[OpenLeg], mids: pd.DataFrame, names: dict[str, str],
    start: pd.Timestamp, now: pd.Timestamp, freq: str = "15min", logged: list = (),
) -> pd.Series:
    """One pair's running profitability: banked after costs, plus whatever is
    open marked to market at mid. `names` maps IG instrument names to epics;
    `logged` are closes IG's history doesn't have yet, which step in at their
    logged P&L (their path while open isn't known here)."""
    grid = pd.date_range(start.floor(freq), now.floor(freq), freq=freq, tz="UTC")
    marks = mids.reindex(grid, method="ffill") if len(mids) else pd.DataFrame(index=grid)
    out = pd.Series(0.0, index=grid)
    for t in trades:
        out[out.index >= t.closed] += t.net
        held = (out.index >= t.opened) & (out.index < t.closed)
        for leg in t.legs:
            out[held] += _mark(marks, names.get(leg.name), leg.size, leg.open_level)[held]
    for leg in open_legs:
        held = out.index >= leg.opened
        out[held] += _mark(marks, leg.epic, leg.size, leg.open_level)[held]
    for c in logged:
        out[out.index >= c.closed] += c.net
    return out


def _mark(marks: pd.DataFrame, epic: str | None, size: float, open_level: float) -> pd.Series:
    if epic is None or epic not in marks:
        return pd.Series(0.0, index=marks.index)
    return (size * (marks[epic] - open_level)).fillna(0.0)


def is_running(pair: str) -> bool:
    found = subprocess.run(["pgrep", "-f", f"oil_pair.main {pair}$"], capture_output=True, text=True)
    return found.returncode == 0


# --- the earnings-miss shorts ---

_ES_RUN = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ INFO earnings_short: (\d+ report.*)$")


def last_short_run(path: Path) -> tuple[pd.Timestamp, str] | None:
    """(when, what) of the strategy's last run that got as far as the day's reports."""
    if not path.exists():
        return None
    found = None
    for line in path.read_text(errors="replace").splitlines():
        if m := _ES_RUN.match(line):
            found = m
    if found is None:
        return None
    return pd.Timestamp(found.group(1)).tz_localize(UK).tz_convert("UTC"), found.group(2)


def open_shorts(book, positions: pd.DataFrame, transactions: pd.DataFrame | None, now: pd.Timestamp) -> pd.DataFrame:
    """Each open short with its hedge, marked at IG's mid prices now, and its funding so far."""
    from earnings_short import main as es

    by_deal = positions.set_index("dealId") if len(positions) and "dealId" in positions else pd.DataFrame()
    outside = es.outside_holdings(book, positions)
    rows = []
    for p in book.positions:
        share = by_deal.loc[p.deal_id] if p.deal_id in by_deal.index else None
        hedge = by_deal.loc[p.hedge_deal_id] if p.hedge_deal_id in by_deal.index else None
        if share is not None and p.name is None:
            p = replace(p, name=str(share.instrumentName))
        mid = (float(share.bid) + float(share.offer)) / 2 if share is not None else None
        share_pnl = p.size * (p.entry_level - mid) if mid is not None and p.entry_level else None
        hedge_mid = (float(hedge.bid) + float(hedge.offer)) / 2 if hedge is not None else None
        hedge_pnl = p.hedge_size * (hedge_mid - p.hedge_level) if hedge_mid is not None and p.hedge_level else None
        f = es.funding(book, p, transactions, outside, now) if transactions is not None else None
        net = (share_pnl or 0.0) + (hedge_pnl or 0.0) + (f.total if f else 0.0)
        rows.append({
            "Share": p.symbol, "Opened (UK)": pd.Timestamp(p.opened).tz_convert(UK).strftime("%d %b %H:%M"),
            "Report": pd.Timestamp(p.report_date).strftime("%d %b"), "Surprise (bp)": p.surprise * 1e4,
            "Short": f"£{p.size:.2f}/pt at {p.entry_level / 100:,.2f}" if p.entry_level else f"£{p.size:.2f}/pt",
            "Exposure": p.exposure, "Now (mid)": mid / 100 if mid is not None else None,
            "IG spread now": (float(share.offer) - float(share.bid)) / mid if mid else None,
            "Share P&L": share_pnl,
            "Hedge": f"US 500 {p.hedge_expiry} £{p.hedge_size:.2f}/pt" if p.hedge_deal_id else "UNHEDGED",
            "Hedge P&L": hedge_pnl, "Funding": f.total if f else None, "Net": net,
            "Closes": pd.Timestamp(p.exit_session).strftime("%a %d %b"),
            "On IG": "yes" if share is not None and (hedge is not None or not p.hedge_deal_id) else "MISSING",
        })
    return pd.DataFrame(rows)


def closed_shorts(book) -> pd.DataFrame:
    """The book's closed shorts, newest first, as their close messages reported them."""
    rows = [{
        "Share": c["symbol"], "Opened (UK)": pd.Timestamp(c["opened"]).tz_convert(UK).strftime("%d %b"),
        "Closed (UK)": pd.Timestamp(c["closed"]).tz_convert(UK).strftime("%d %b %H:%M"),
        "Share P&L": c.get("share_pnl"), "Hedge P&L": c.get("hedge_pnl"),
        "Funding": (c.get("funding") or 0.0) + (c.get("dividends") or 0.0) if c.get("funding") is not None else None,
        "Net": c.get("net"), "Note": "closed outside the app" if c.get("outside_the_app") else "",
        "_at": pd.Timestamp(c["closed"]),
    } for c in book.closed if c.get("closed")]
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).sort_values("_at", ascending=False).drop(columns="_at")
