"""Pure helpers for scripts/discover_epics.py - kept separate from the
interactive input()/print() flow so the filtering/formatting logic is
unit-testable."""

from __future__ import annotations

import pandas as pd

CANDIDATE_COLUMNS = ["epic", "instrumentName", "instrumentType", "expiry", "bid", "offer", "marketStatus"]


def search_multiple_terms(search_fn, terms: list[str]) -> pd.DataFrame:
    """Runs search_fn(term) for each term, concatenates results, and dedupes on epic."""
    frames = [search_fn(term) for term in terms]
    frames = [f for f in frames if f is not None and len(f) > 0]
    if not frames:
        return pd.DataFrame(columns=CANDIDATE_COLUMNS)
    combined = pd.concat(frames, ignore_index=True)
    return combined.drop_duplicates(subset="epic").reset_index(drop=True)


def format_candidates_table(df: pd.DataFrame) -> str:
    if len(df) == 0:
        return "(no candidates found)"
    columns = [c for c in CANDIDATE_COLUMNS if c in df.columns]
    display = df[columns].copy()
    display.insert(0, "row", range(len(display)))
    return display.to_string(index=False)
