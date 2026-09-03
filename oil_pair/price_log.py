"""Local, append-only cache of streamed mid prices.

Builds the app's own historical dataset from live Lightstreamer ticks over
time, so the initial hedge-ratio fit (and, after a restart, subsequent
fits) don't depend on IG's historical-data REST endpoint - which is
rate-limited on a weekly, non-retryable basis (see
HistoricalDataAllowanceExceededError in ig_client.py).
"""

from __future__ import annotations

import csv
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PRICE_LOG_PATH = REPO_ROOT / "price_log" / "ticks.csv"

_FIELDNAMES = ["timestamp", "epic", "mid"]


def append_tick(epic: str, mid: float, timestamp: pd.Timestamp, path: Path = DEFAULT_PRICE_LOG_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not path.exists()
    with path.open("a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=_FIELDNAMES)
        if is_new:
            writer.writeheader()
        writer.writerow({"timestamp": timestamp.isoformat(), "epic": epic, "mid": mid})


def load_mid_series(epic: str, path: Path = DEFAULT_PRICE_LOG_PATH) -> pd.Series:
    """Returns an empty float Series if the log doesn't exist or has no rows
    for this epic - callers must handle "no cache yet" rather than treating
    that as an error."""
    if not path.exists():
        return pd.Series(dtype=float)

    df = pd.read_csv(path, parse_dates=["timestamp"])
    df = df[df["epic"] == epic]
    if df.empty:
        return pd.Series(dtype=float)

    series = pd.Series(df["mid"].to_numpy(dtype=float), index=pd.DatetimeIndex(df["timestamp"]))
    series.name = epic
    return series.sort_index()
