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

A pair's running profitability is its banked P&L after costs (IG's
figures, stepping up or down as each trade closes) plus the open trade
marked to market at mid prices - an estimate until IG books the close.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from oil_pair.ledger import Trade

UK = "Europe/London"
_SPREAD = re.compile(
    r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ INFO __main__: spread=(\S+) \((\S+) std\) .*?side=PairSide\.(\w+)"
)
_LEVEL = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ (ERROR|CRITICAL) ")


@dataclass(frozen=True)
class LogView:
    spread: pd.DataFrame          # index UTC time; columns z, side
    last_line_at: pd.Timestamp | None
    recent_errors: list[str]      # ERROR/CRITICAL lines from the last 24 hours


def read_log(path: Path, now: pd.Timestamp, rotated: int = 1) -> LogView:
    """The pair's spread history and health from its log (and `rotated` older files)."""
    files = [p for p in [path.with_name(f"{path.name}.{i}") for i in range(rotated, 0, -1)] + [path] if p.exists()]
    rows, errors, last = [], [], None
    since = (now - pd.Timedelta(hours=24)).tz_convert(UK).strftime("%Y-%m-%d %H:%M:%S")
    for f in files:
        with f.open(errors="replace") as handle:
            for line in handle:
                if m := _SPREAD.match(line):
                    rows.append((m.group(1), float(m.group(3)), m.group(4)))
                elif (m := _LEVEL.match(line)) and m.group(1) >= since:
                    errors.append(line.strip())
                if line[:4].isdigit():
                    last = line[:19]
    spread = pd.DataFrame(rows, columns=["at", "z", "side"])
    spread["at"] = _uk(spread["at"])
    # the hour the clocks go back repeats, so its times can't be placed: dropped
    spread = spread.dropna(subset=["at"]).drop_duplicates("at", keep="last").set_index("at").sort_index()
    return LogView(spread, _uk(pd.Series([last]))[0] if last else None, errors[-20:])


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
    start: pd.Timestamp, now: pd.Timestamp, freq: str = "15min",
) -> pd.Series:
    """One pair's running profitability: banked after costs, plus whatever is
    open marked to market at mid. `names` maps IG instrument names to epics."""
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
    return out


def _mark(marks: pd.DataFrame, epic: str | None, size: float, open_level: float) -> pd.Series:
    if epic is None or epic not in marks:
        return pd.Series(0.0, index=marks.index)
    return (size * (marks[epic] - open_level)).fillna(0.0)


def is_running(pair: str) -> bool:
    found = subprocess.run(["pgrep", "-f", f"oil_pair.main {pair}$"], capture_output=True, text=True)
    return found.returncode == 0
