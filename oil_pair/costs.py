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

Shares are different: IG books their funding once a day for all of a
market's shares together - "Short Interest for US/Can shares 20/10/26"
(received on shorts), "Stock Borrowing for US/Can shares 20/10/26" (paid on
shorts); UK shares' rows name no market ("Short Interest for 20/10/26"). A
share position's part of each is its share of the exposure (stake x price
at opening) held in that market's shares that night. A dividend adjustment
names the share, and is shared out like a commodity's charge.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import pandas as pd

UK = "Europe/London"
OVERNIGHT = re.compile(r"^Daily (?:Financing Adjustment|Admin Fee)\b.*? for \d+ day (.+)$")
SHARE_FUNDING = re.compile(r"^[A-Za-z ]+? for (?:(?P<group>[A-Za-z/]+ shares) )?(?P<day>\d\d/\d\d/\d\d)$")
DIVIDEND = re.compile(r"dividend", re.IGNORECASE)
UK_SHARES, US_SHARES = "UK shares", "US/Can shares"
# A closed deal in the history is this position itself if it opened this close to it.
SAME_DEAL_WITHIN = pd.Timedelta(minutes=2)
PAGE_SIZE = 500


@dataclass(frozen=True)
class Holding:
    name: str              # IG's instrument name, as the transaction history gives it
    size: float            # £ per point
    opened: pd.Timestamp   # UTC
    group: str | None = None            # a share's funding rows (share_group); None for anything else
    exposure: float = 0.0               # £, size x price at opening: a share's weight in its group's funding
    closed: pd.Timestamp | None = None  # UTC, for one of `others` since closed


def share_group(epic: str) -> str | None:
    """Which of IG's daily share-funding rows a market's are booked in, from its epic: UK shares
    (KA.D.BP.DAILY.IP), US and Canadian ones (UD.D.STLD.DAILY.IP), or None - not a share, or not one
    of those."""
    if re.match(r"^K[A-Z]\.D\.", epic):
        return UK_SHARES
    if re.match(r"^U[A-Z]\.D\.", epic):
        return US_SHARES
    return None


def holdings(positions: pd.DataFrame) -> list[Holding]:
    """Every open position in IG's list (fetch_open_positions) as a Holding."""
    out = []
    for _, r in positions.iterrows():
        size, group = abs(float(r["size"])), share_group(str(r.get("epic", "")))
        level = pd.to_numeric(r.get("level"), errors="coerce")
        exposure = size * float(level) if group and pd.notna(level) else 0.0
        out.append(Holding(str(r.instrumentName), size, utc(r.createdDateUTC), group, exposure))
    return out


def _held_at(h: Holding, cutoff: pd.Timestamp, until: pd.Timestamp | None = None) -> bool:
    until = until if until is not None else h.closed
    return h.opened < cutoff and (until is None or cutoff < until)


def _night(day: str) -> pd.Timestamp:
    """The cutoff of the night a share-funding row is for: 22:00 UK on its date (DD/MM/YY)."""
    return (pd.Timestamp(pd.to_datetime(day, format="%d/%m/%y")) + pd.Timedelta(hours=22)).tz_localize(UK).tz_convert("UTC")


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


def _charges(
    transactions: pd.DataFrame, holding: Holding, until: pd.Timestamp | None = None
) -> list[tuple[pd.Timestamp, float]]:
    """(cutoff, amount) for each overnight charge in this holding's market - and
    each dividend adjustment on it - while it was held: since it opened, and
    before `until` if it has closed."""
    out = []
    for _, row in transactions.iterrows():
        name = str(row.instrumentName)
        match = OVERNIGHT.search(name)
        if match:
            if match.group(1).strip() != holding.name:
                continue
        elif row.transactionType == "DEAL" or not DIVIDEND.search(name) or _bare(holding.name) not in name:
            continue
        cutoff = financing_cutoff(utc(row.dateUtc))
        if _held_at(holding, cutoff, until):
            out.append((cutoff, _amount(row.profitAndLoss)))
    return out


def _bare(name: str) -> str:
    """"Steel Dynamics Inc (24 Hours)" -> "Steel Dynamics Inc", as a dividend row may name it."""
    return re.sub(r"\s*\([^)]*\)$", "", name)


def _share_funding(
    transactions: pd.DataFrame, holding: Holding, others: list[Holding], until: pd.Timestamp | None = None
) -> list[tuple[pd.Timestamp, float]]:
    """(cutoff, this holding's part) of each of its share group's daily funding rows while it was held."""
    if holding.group is None or holding.exposure <= 0:
        return []
    out = []
    for _, row in transactions.iterrows():
        match = SHARE_FUNDING.match(str(row.instrumentName))
        if not match or row.transactionType == "DEAL" or (match.group("group") or UK_SHARES) != holding.group:
            continue
        cutoff = _night(match.group("day"))
        if not _held_at(holding, cutoff, until):
            continue
        held = holding.exposure + sum(o.exposure for o in others if o.group == holding.group and _held_at(o, cutoff))
        out.append((cutoff, _amount(row.profitAndLoss) * holding.exposure / held))
    return out


def overnight_share(
    transactions: pd.DataFrame, holding: Holding, others: list[Holding], until: pd.Timestamp | None = None
) -> float:
    """This holding's part of IG's overnight charges while it was held: + received, - paid.
    For a share, its funding and any dividend adjustment.

    `others` are the other positions in the account: open ones, and for a
    share's funding any others since closed (with `closed` set). `until` is when the
    holding closed, for one closed long ago; without it every charge since it
    opened counts. If this holding has been closed, its own DEAL row in the
    history is recognised (same market, size and opening time) and not
    counted a second time."""
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
    for cutoff, amount in _charges(transactions, holding, until):
        held = holding.size
        held += sum(o.size for o in others if o.name == holding.name and _held_at(o, cutoff))
        held += sum(size for opened, shut, size in closed if opened < cutoff < shut)
        total += amount * holding.size / held
    return total + sum(part for _, part in _share_funding(transactions, holding, others, until))


def unbooked(transactions: pd.DataFrame, holding: Holding, now: pd.Timestamp) -> bool:
    """True if the holding was held at the last 22:00 UK cutoff but IG hasn't
    booked that night's charges yet - they arrive an hour or two later, so a
    figure worked out in between is missing them."""
    cutoff = financing_cutoff(now)
    if holding.group is not None:
        # a share's funding is booked night by night; a weekend's may come in Friday's row
        while cutoff.tz_convert(UK).dayofweek >= 5:
            cutoff -= pd.Timedelta(days=1)
    if holding.opened >= cutoff:
        return False
    if holding.group is not None:
        return all(c != cutoff for c, _ in _share_funding(transactions, holding, []))
    return all(c != cutoff for c, _ in _charges(transactions, holding))


def fetch_transactions(client, since: pd.Timestamp) -> pd.DataFrame:
    """IG's transaction history since `since` (v2 /history/transactions), every page of it."""
    rows, page, pages = [], 1, 1
    while page <= pages:
        response = client.service._req(
            "read", "/history/transactions",
            {"type": "ALL", "from": utc(since).strftime("%Y-%m-%dT%H:%M:%S"), "pageSize": PAGE_SIZE,
             "pageNumber": page}, None, "2",
        )
        response.raise_for_status()
        body = response.json()
        rows += body.get("transactions", [])
        pages = int((body.get("metadata") or {}).get("pageData", {}).get("totalPages") or 1)
        page += 1
    return pd.DataFrame(rows)
