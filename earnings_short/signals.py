"""Which earnings reports to short - pure functions, no IG or database calls.

A report's surprise is (actual EPS - consensus) / the share's close before the report. It is a miss
when that is at or below the `quantile` of the surprises of every S&P 500 report in the year before - a
threshold known at the time, unlike ranking against the rest of its own quarter.

A miss is worth shorting only when something backs it up (confirmations): revenue also among the year's
worst, the shares already well down on the market by the entry, or the company's previous report a miss
too. In 2016-2026 a miss with none of these didn't drift at all (-24 bp a trade against the week's other
reports), and one with any of them did (+41 bp); the three are nearly unrelated to each other.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

MIN_HISTORY = 200   # reports in the trailing year before the threshold is trusted


@dataclass(frozen=True)
class Candidate:
    symbol: str          # our US symbol (BRK.B style)
    report_date: pd.Timestamp
    eps_actual: float
    eps_estimated: float
    prev_close: float
    surprise: float      # (actual - estimate) / prev_close
    threshold: float
    prev_day: pd.Timestamp | None = None        # the session prev_close is from
    revenue_surprise: float | None = None       # (actual - consensus) / consensus
    revenue_threshold: float | None = None      # the past year's revenue_quantile of it
    previous_missed: bool | None = None         # the company's last report missed on EPS (None: none recent)


def surprises(reports: pd.DataFrame) -> pd.Series:
    """reports: eps_actual, eps_estimated, prev_close."""
    ok = (reports.prev_close > 0) & reports.eps_actual.notna() & reports.eps_estimated.notna()
    return ((reports.eps_actual - reports.eps_estimated) / reports.prev_close).where(ok)


def revenue_surprises(reports: pd.DataFrame) -> pd.Series:
    """reports: revenue_actual, revenue_estimated (NaN where either is missing)."""
    if "revenue_actual" not in reports or "revenue_estimated" not in reports:
        return pd.Series(np.nan, index=reports.index)
    ok = (reports.revenue_estimated > 0) & reports.revenue_actual.notna()
    return ((reports.revenue_actual - reports.revenue_estimated) / reports.revenue_estimated).where(ok)


def revenue_threshold(history: pd.DataFrame, quantile: float) -> float | None:
    """The revenue surprise at `quantile` of the trailing year's reports, or None with too little history."""
    s = revenue_surprises(history).dropna()
    if len(s) < MIN_HISTORY:
        return None
    return float(np.quantile(s, quantile))


def previous_missed(earlier: pd.DataFrame, symbol: str, report_date: pd.Timestamp, within_days: int) -> bool | None:
    """Whether the company's previous report missed on EPS - None if it had none within `within_days`."""
    mine = earlier[(earlier.symbol == symbol) & (earlier.report_date < report_date)]
    if mine.empty:
        return None
    last = mine.loc[mine.report_date.idxmax()]
    if (report_date - last.report_date).days > within_days:
        return None
    return bool(last.eps_actual < last.eps_estimated)


def threshold(history: pd.DataFrame, quantile: float) -> float | None:
    """The surprise at `quantile` of the trailing year's reports, or None with too little history."""
    s = surprises(history).dropna()
    if len(s) < MIN_HISTORY:
        return None
    return float(np.quantile(s, quantile))


def candidates(reports: pd.DataFrame, cut: float, revenue_cut: float | None = None,
               earlier: pd.DataFrame | None = None, repeat_within_days: int = 150) -> list[Candidate]:
    """Reports to act on today whose surprise is at or below the cut, worst first, with what may back
    each up: its revenue surprise and the cut for it, and whether `earlier` (past reports) shows the
    company's previous one missed too."""
    s = surprises(reports)
    picked = reports.assign(surprise=s, rev=revenue_surprises(reports))[s.notna() & (s <= cut)].sort_values("surprise")
    return [
        Candidate(symbol=r.symbol, report_date=pd.Timestamp(r.report_date), eps_actual=float(r.eps_actual),
                  eps_estimated=float(r.eps_estimated), prev_close=float(r.prev_close), surprise=float(r.surprise),
                  threshold=cut, prev_day=pd.Timestamp(r.prev_day) if pd.notna(getattr(r, "prev_day", None)) else None,
                  revenue_surprise=None if pd.isna(r.rev) else float(r.rev), revenue_threshold=revenue_cut,
                  previous_missed=None if earlier is None else
                  previous_missed(earlier, r.symbol, pd.Timestamp(r.report_date), repeat_within_days))
        for r in picked.itertuples()
    ]


def reaction(price_now: float | None, prev_close: float | None, market_now: float | None,
             market_prev_close: float | None) -> float | None:
    """The share's move since the close before its report less the market's, as a log return - None
    without all four prices, or if they can't be the same units (a move of over 2x)."""
    if not all(v is not None and v > 0 for v in (price_now, prev_close, market_now, market_prev_close)):
        return None
    share, market = np.log(price_now / prev_close), np.log(market_now / market_prev_close)
    if abs(share) > np.log(2) or abs(market) > np.log(2):
        return None
    return float(share - market)


def confirmations(c: Candidate, move: float | None, max_reaction: float) -> list[str]:
    """What backs up the miss: revenue among the year's worst, the shares down at least `max_reaction`
    (log) on the market, or the previous report a miss too."""
    out = []
    if c.revenue_surprise is not None and c.revenue_threshold is not None and c.revenue_surprise <= c.revenue_threshold:
        out.append(f"revenue {c.revenue_surprise:+.1%} on consensus")
    if move is not None and move <= max_reaction:
        out.append(f"shares {np.expm1(move):+.1%} on the market")
    if c.previous_missed:
        out.append("last report missed too")
    return out


def describe(c: Candidate, move: float | None) -> str:
    """The three confirmations as they stand, for a skipped miss."""
    rev = ("revenue n/a" if c.revenue_surprise is None else
           f"revenue {c.revenue_surprise:+.1%}" + (f" (cut {c.revenue_threshold:+.1%})" if c.revenue_threshold is not None else ""))
    shares = "move n/a" if move is None else f"shares {np.expm1(move):+.1%} on the market"
    last = {True: "last report missed", False: "last report beat or met", None: "no recent report"}[c.previous_missed]
    return f"{rev}, {shares}, {last}"


def stake(notional: float, mid: float, min_size: float, max_min_ratio: float) -> float | None:
    """£ per point for about `notional` of exposure at `mid` (IG's US shares are priced in cents, so
    exposure = stake x price in points), rounded to 2 dp and raised to IG's minimum - or None when the
    minimum alone would be more than `max_min_ratio` x the notional."""
    size = round(notional / mid, 2)
    if size >= min_size:
        return size
    if min_size * mid > max_min_ratio * notional:
        return None
    return min_size
