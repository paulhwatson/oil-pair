"""IG's overnight charges on a position: daily financing and the admin fee.

IG books these per market, not per deal - "Daily Financing Adjustment -
Commodities Basis for 1 day Oil - Brent Crude" and "Daily Admin Fee -
Commodities Interest for 1 day Oil - Brent Crude" - on whatever was held in
that market at 22:00 UK, an hour or two later. A position's part of each
charge is its share of the stake held across that cutoff: positions still
open and deals since closed (the transaction history's DEAL rows). So where
two pairs hold the same market (Brent) each gets its part, and a deal opened
and closed within the day - like a refused entry's unwind - held nothing at
the cutoff and takes no share.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import pandas as pd

UK = "Europe/London"
OVERNIGHT = re.compile(r"^Daily (?:Financing Adjustment|Admin Fee)\b.*? for \d+ day (.+)$")
# A closed deal in the history is this position itself if it opened this close to it.
SAME_DEAL_WITHIN = pd.Timedelta(minutes=2)


@dataclass(frozen=True)
class Holding:
    name: str              # IG's instrument name, as the transaction history gives it
    size: float            # £ per point
    opened: pd.Timestamp   # UTC


def holdings(positions: pd.DataFrame) -> list[Holding]:
    """Every open position in IG's list (fetch_open_positions) as a Holding."""
    return [
        Holding(str(r.instrumentName), abs(float(r["size"])), utc(r.createdDateUTC))
        for _, r in positions.iterrows()
    ]


def financing_cutoff(charged: pd.Timestamp) -> pd.Timestamp:
    """The cutoff a charge belongs to: the last 22:00 UK at or before it was booked."""
    local = charged.tz_convert(UK)
    cutoff = local.normalize() + pd.Timedelta(hours=22)
    if cutoff > local:
        cutoff -= pd.Timedelta(days=1)
    return cutoff.tz_convert("UTC")


def utc(value) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def _amount(value) -> float:
    return float(re.sub(r"[^0-9.\-]", "", str(value)) or 0)


def _charges(transactions: pd.DataFrame, holding: Holding) -> list[tuple[pd.Timestamp, float]]:
    """(cutoff, amount) for each overnight charge in this holding's market since it opened."""
    out = []
    for _, row in transactions.iterrows():
        match = OVERNIGHT.search(str(row.instrumentName))
        if not match or match.group(1).strip() != holding.name:
            continue
        cutoff = financing_cutoff(utc(row.dateUtc))
        if holding.opened < cutoff:
            out.append((cutoff, _amount(row.profitAndLoss)))
    return out


def overnight_share(transactions: pd.DataFrame, holding: Holding, others: list[Holding]) -> float:
    """This holding's part of IG's overnight charges since it opened: + received, - paid.

    `others` are the other positions open in the account. If this holding has
    just been closed, its own DEAL row in the history is recognised (same
    market, size and opening time) and not counted a second time."""
    if transactions.empty:
        return 0.0
    deals = transactions[(transactions.transactionType == "DEAL") & (transactions.instrumentName == holding.name)]
    closed = []
    own_found = False
    for _, d in deals.iterrows():
        opened, size = utc(d.openDateUtc), abs(_amount(d["size"]))
        if not own_found and abs(opened - holding.opened) <= SAME_DEAL_WITHIN and abs(size - holding.size) < 1e-9:
            own_found = True
            continue
        closed.append((opened, utc(d.dateUtc), size))
    total = 0.0
    for cutoff, amount in _charges(transactions, holding):
        held = holding.size
        held += sum(o.size for o in others if o.name == holding.name and o.opened < cutoff)
        held += sum(size for opened, shut, size in closed if opened < cutoff < shut)
        total += amount * holding.size / held
    return total


def unbooked(transactions: pd.DataFrame, holding: Holding, now: pd.Timestamp) -> bool:
    """True if the holding was held at the last 22:00 UK cutoff but IG hasn't
    booked that night's charges yet - they arrive an hour or two later, so a
    figure worked out in between is missing them."""
    cutoff = financing_cutoff(now)
    if holding.opened >= cutoff:
        return False
    return all(c != cutoff for c, _ in _charges(transactions, holding))


def fetch_transactions(client, since: pd.Timestamp) -> pd.DataFrame:
    """IG's transaction history since `since` (v2 /history/transactions)."""
    response = client.service._req(
        "read", "/history/transactions",
        {"type": "ALL", "from": utc(since).strftime("%Y-%m-%dT%H:%M:%S"), "pageSize": 1000}, None, "2",
    )
    response.raise_for_status()
    return pd.DataFrame(response.json().get("transactions", []))
