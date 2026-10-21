"""Which earnings reports to short - pure functions, no IG or database calls.

A report's surprise is (actual EPS - consensus) / the share's close before the report. It is a miss
worth shorting when that is at or below the `quantile` of the surprises of every S&P 500 report in the
year before - a threshold known at the time, unlike ranking against the rest of its own quarter.
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


def surprises(reports: pd.DataFrame) -> pd.Series:
    """reports: eps_actual, eps_estimated, prev_close."""
    ok = (reports.prev_close > 0) & reports.eps_actual.notna() & reports.eps_estimated.notna()
    return ((reports.eps_actual - reports.eps_estimated) / reports.prev_close).where(ok)


def threshold(history: pd.DataFrame, quantile: float) -> float | None:
    """The surprise at `quantile` of the trailing year's reports, or None with too little history."""
    s = surprises(history).dropna()
    if len(s) < MIN_HISTORY:
        return None
    return float(np.quantile(s, quantile))


def candidates(reports: pd.DataFrame, cut: float) -> list[Candidate]:
    """Reports to act on today whose surprise is at or below the cut, worst first."""
    s = surprises(reports)
    picked = reports.assign(surprise=s)[s.notna() & (s <= cut)].sort_values("surprise")
    return [
        Candidate(symbol=r.symbol, report_date=pd.Timestamp(r.report_date), eps_actual=float(r.eps_actual),
                  eps_estimated=float(r.eps_estimated), prev_close=float(r.prev_close), surprise=float(r.surprise),
                  threshold=cut)
        for r in picked.itertuples()
    ]


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
