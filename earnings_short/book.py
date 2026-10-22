"""The strategy's own record of what it holds (state/earnings_short/book.json) - written after every
deal, so a crash between deals never loses track of a position."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class Position:
    symbol: str
    epic: str
    deal_id: str
    size: float              # £ per point, short
    entry_level: float | None
    exposure: float          # £, size x price at entry
    opened: str              # ISO UTC
    exit_session: str        # YYYY-MM-DD: close at the run on or after it
    report_date: str
    surprise: float
    threshold: float
    hedge_deal_id: str | None = None
    hedge_size: float | None = None
    hedge_level: float | None = None
    hedge_epic: str | None = None
    hedge_expiry: str | None = None      # DEC-26 for a quarterly contract, DFB for the daily-funded one
    name: str | None = None              # IG's instrument name, as its transaction history gives it
    entry_mid: float | None = None       # IG's mids when it opened, for the IB estimate (earnings_short/ib.py)
    hedge_entry_mid: float | None = None
    confirmations: list | None = None    # what backed up the miss (signals.confirmations)
    reaction: float | None = None        # its move on the market by the entry (log), if known


@dataclass
class Book:
    account: str
    positions: list[Position] = field(default_factory=list)
    closed: list[dict] = field(default_factory=list)
    last_session: str | None = None


def load(path: Path, account: str) -> Book:
    if not path.exists():
        return Book(account=account)
    raw = json.loads(path.read_text())
    book = Book(account=raw["account"], positions=[Position(**p) for p in raw.get("positions", [])],
                closed=raw.get("closed", []), last_session=raw.get("last_session"))
    if book.account != account:
        raise RuntimeError(f"{path} belongs to the {book.account} account, not {account} - refusing to mix them")
    return book


def save(book: Book, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(book), indent=2, default=str))
    tmp.replace(path)
