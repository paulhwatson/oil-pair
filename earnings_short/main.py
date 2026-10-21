"""The daily run: close what is due, short new earnings misses, hedge each with the US 500.

    ./.venv/bin/python -m earnings_short.main             # demo; acts only 15:35-15:58 New York
    ./.venv/bin/python -m earnings_short.main --dry-run   # show what it would do, any time; no deals
    ./.venv/bin/python -m earnings_short.main --live      # the live account

launchd fires it a few times each weekday afternoon (scripts/install_earnings_short.sh); it acts once
per NYSE session, inside the window, and otherwise exits at once.

Order of a run: reconcile the book with IG's open positions (a deal no longer open was closed outside
the app); close every short whose exit session has come, with its hedge; then short the reports of the
previous session that missed, each with a US 500 long of the same exposure. Every deal is checked for
IG's acceptance, and the book is saved after each, so a crash never loses track of a position.

A close is reported after costs: IG's realised P&L on the share and the hedge (after bid/offer), plus
the short's funding - IG books one row a night for all US shares, short interest received and stock
borrowing paid, shared out among the book's shorts by exposure (oil_pair.costs) - and any dividend
adjustment. The quarterly US 500 hedge has no funding.
"""

from __future__ import annotations

import argparse
import logging
import time
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path

import pandas as pd

from earnings_short import book as book_store
from earnings_short import data, sessions, signals
from oil_pair import costs, instance_lock
from oil_pair.ig_client import IGClient, deal_accepted, describe_rejection
from oil_pair.logging_setup import configure_logging
from oil_pair.notify import Notifier, load_email_config, load_whatsapp_recipients
from oil_pair.settings import REPO_ROOT, load_credentials

log = logging.getLogger("earnings_short")
NY = "America/New_York"
CONFIG = REPO_ROOT / "config" / "earnings_short.toml"
STATE = REPO_ROOT / "state" / "earnings_short" / "book.json"
LOCK = REPO_ROOT / "state" / "earnings_short" / "earnings_short.lock"
LOGS = REPO_ROOT / "logs" / "earnings_short"
PAUSE_BETWEEN_DEALS = 2.0   # IG's per-minute request limit is shared with the pairs


@dataclass(frozen=True)
class Config:
    notional_per_trade: float
    max_min_stake_ratio: float
    max_new_per_day: int
    max_open: int
    quantile: float
    hold_sessions: int
    hedge_epics: list
    hedge_fallback_epic: str
    window_start: str
    window_end: str


def load_config(path: Path = CONFIG) -> Config:
    return Config(**tomllib.loads(path.read_text()))


def in_window(now_ny: pd.Timestamp, cfg: Config) -> bool:
    t = now_ny.strftime("%H:%M")
    return sessions.is_session(now_ny.tz_localize(None).normalize()) and cfg.window_start <= t <= cfg.window_end


def _profit(confirm: dict | None) -> float | None:
    try:
        return float(confirm["profit"]) if confirm and confirm.get("profit") is not None else None
    except (TypeError, ValueError):
        return None


def _level(confirm: dict | None) -> float | None:
    try:
        return float(confirm["level"]) if confirm and confirm.get("level") is not None else None
    except (TypeError, ValueError):
        return None


def _mid(market: dict) -> float | None:
    s = market["snapshot"]
    if s.get("bid") is None or s.get("offer") is None:
        return None
    return (float(s["bid"]) + float(s["offer"])) / 2


@dataclass(frozen=True)
class HedgeMarket:
    epic: str
    expiry: str                       # DEC-26, or DFB
    last_dealing: pd.Timestamp | None
    mid: float
    min_size: float


def hedge_markets(client: IGClient, cfg: Config) -> list[HedgeMarket]:
    """The tradeable US 500 contracts to hedge with: quarterly first, the daily-funded one last."""
    out = []
    for epic in [*cfg.hedge_epics, cfg.hedge_fallback_epic]:
        try:
            m = client.fetch_market(epic)
        except Exception as exc:  # noqa: BLE001 - a missing contract just isn't a choice
            log.warning("hedge contract %s unavailable: %s", epic, exc)
            continue
        mid = _mid(m)
        if m["snapshot"]["marketStatus"] != "TRADEABLE" or mid is None:
            continue
        last = (m["instrument"].get("expiryDetails") or {}).get("lastDealingDate")
        out.append(HedgeMarket(epic, m["instrument"].get("expiry") or "DFB",
                               pd.Timestamp(last, tz="UTC") if last else None, mid,
                               float(m["dealingRules"]["minDealSize"]["value"])))
    return out


def pick_hedge(markets: list[HedgeMarket], exit_session: pd.Timestamp) -> HedgeMarket | None:
    """The first contract still dealing a day after the short's exit (the daily-funded one always is)."""
    for h in markets:
        if h.last_dealing is None or h.last_dealing.tz_convert(None) > exit_session + pd.Timedelta(days=1):
            return h
    return None


def reconcile(book: book_store.Book, client: IGClient, now: pd.Timestamp, lines: list[str]) -> pd.DataFrame:
    """Drop what IG no longer holds, fill in IG's names; IG's open positions, for close_due."""
    open_now = client.fetch_open_positions()
    open_ids = set(open_now.get("dealId", pd.Series(dtype=str)))
    names = dict(zip(open_now["dealId"], open_now["instrumentName"])) if "instrumentName" in open_now else {}
    for p in list(book.positions):
        if p.deal_id not in open_ids:
            lines.append(f"{p.symbol}: no longer open on IG - closed outside the app; dropped from the book")
            log.warning("%s (%s) not open on IG - dropping it from the book", p.symbol, p.deal_id)
            book.closed.append({"symbol": p.symbol, "name": p.name, "opened": p.opened, "exposure": p.exposure,
                                "closed": now.isoformat(), "outside_the_app": True})
            book.positions.remove(p)
        elif p.name is None and names.get(p.deal_id):
            p.name = str(names[p.deal_id])
    return open_now


@dataclass(frozen=True)
class Funding:
    funding: float      # + received, - paid: short interest less stock borrowing
    dividends: float    # dividend adjustments: - paid on a short
    unbooked: bool      # IG hasn't booked the last night's funding yet

    @property
    def total(self) -> float:
        return self.funding + self.dividends


def _share(symbol: str, name: str | None, exposure: float | None, opened: str,
           closed: str | None = None) -> costs.Holding:
    return costs.Holding(name or symbol, 0.0, costs.utc(opened), costs.US_SHARES, exposure or 0.0,
                         costs.utc(closed) if closed else None)


def funding(book: book_store.Book, p: book_store.Position, transactions: pd.DataFrame,
            outside: list[costs.Holding], closed_at: pd.Timestamp) -> Funding:
    """The short's funding and dividends while held, from IG's transaction history. Each night's US
    share funding is shared among the US shares held that night: the book's shorts, open and closed,
    and `outside` - any other US share open on the account."""
    me = replace(_share(p.symbol, p.name, p.exposure, p.opened), size=p.size)
    others = [_share(q.symbol, q.name, q.exposure, q.opened) for q in book.positions if q.deal_id != p.deal_id]
    others += [_share(c["symbol"], c.get("name"), c.get("exposure"), c["opened"], c["closed"])
               for c in book.closed if c.get("exposure") and c.get("closed")]
    others += outside
    total = costs.overnight_share(transactions, me, others, until=closed_at)
    dividends = costs.overnight_share(transactions, replace(me, group=None), others, until=closed_at)
    return Funding(total - dividends, dividends, costs.unbooked(transactions, me, closed_at))


def outside_holdings(book: book_store.Book, open_now: pd.DataFrame | None) -> list[costs.Holding]:
    """US shares open on the account that aren't the book's: they share each night's funding too."""
    if open_now is None or "dealId" not in open_now:
        return []
    ours = {q.deal_id for q in book.positions} | {q.hedge_deal_id for q in book.positions}
    return [h for h in costs.holdings(open_now[~open_now["dealId"].isin(ours)]) if h.group == costs.US_SHARES]


def _transactions(client: IGClient, since: pd.Timestamp) -> pd.DataFrame | None:
    try:
        return costs.fetch_transactions(client, since - pd.Timedelta(days=1))
    except Exception:  # noqa: BLE001 - the close is reported without funding rather than not at all
        log.warning("could not read IG's transaction history for the funding", exc_info=True)
        return None


def _costs_text(f: Funding | None, hedge_expiry: str | None) -> str:
    if f is None:
        return "funding n/a (IG's history unavailable)"
    text = f"funding {_pounds(f.funding)}"
    if f.dividends:
        text += f", dividends {_pounds(f.dividends)}"
    if f.unbooked:
        text += " (IG hasn't booked the last night's yet)"
    if hedge_expiry == "DFB":
        text += " (the daily-funded hedge's own funding not counted)"
    return text


def close_due(book: book_store.Book, client: IGClient, now: pd.Timestamp, lines: list[str], path: Path,
              open_now: pd.DataFrame | None = None) -> float:
    """Close every short whose exit session has come, with its hedge; the total after costs."""
    today = now.tz_convert(NY).tz_localize(None).normalize()
    due = [p for p in book.positions if pd.Timestamp(p.exit_session) <= today]
    if not due:
        return 0.0
    transactions = _transactions(client, min(costs.utc(p.opened) for p in due))
    outside = outside_holdings(book, open_now)
    total = 0.0
    for p in due:
        confirm = client.close_market_position(p.deal_id, "BUY", p.size, p.epic)
        if not deal_accepted(confirm):
            lines.append(f"{p.symbol}: close REFUSED ({describe_rejection(confirm)}) - still short, will retry")
            log.critical("close of %s refused: %s", p.symbol, describe_rejection(confirm))
            continue
        share_pnl = _profit(confirm)
        hedge_pnl = None
        if p.hedge_deal_id:
            h = client.close_market_position(p.hedge_deal_id, "SELL", p.hedge_size, p.hedge_epic)
            if deal_accepted(h):
                hedge_pnl = _profit(h)
            else:
                lines.append(f"{p.symbol}: hedge close REFUSED ({describe_rejection(h)}) - close the US 500 "
                             f"long {p.hedge_deal_id} by hand")
                log.critical("hedge close for %s refused: %s", p.symbol, describe_rejection(h))
        f = funding(book, p, transactions, outside, now) if transactions is not None else None
        gross = (share_pnl or 0.0) + (hedge_pnl or 0.0)
        net = gross + (f.total if f else 0.0)
        total += net
        held = f"{costs.utc(p.opened).tz_convert(NY):%d %b} to {today:%d %b}"
        lines.append(f"Closed {p.symbol} short ({held}): share {_pounds(share_pnl)}, hedge {_pounds(hedge_pnl)}, "
                     f"{_costs_text(f, p.hedge_expiry)}, net {_pounds(net)} " + ("after costs" if f else "before funding"))
        book.closed.append({"symbol": p.symbol, "name": p.name, "opened": p.opened, "closed": now.isoformat(),
                            "report_date": p.report_date, "surprise": p.surprise, "exposure": p.exposure,
                            "share_pnl": share_pnl, "hedge_pnl": hedge_pnl, "gross": gross,
                            "funding": f.funding if f else None, "dividends": f.dividends if f else None,
                            "net": net})
        book.positions.remove(p)
        book_store.save(book, path)
        time.sleep(PAUSE_BETWEEN_DEALS)
    return total


def _pounds(v: float | None) -> str:
    return "n/a" if v is None else f"{'+' if v >= 0 else '-'}£{abs(v):,.2f}"


def plan_entries(today: pd.Timestamp, cfg: Config, held: set[str]) -> tuple[list[signals.Candidate], float | None, int]:
    """Today's candidates: reports whose first session on or after their date was the previous session."""
    prev = sessions.previous_session(today)
    recent = data.reports(prev - pd.Timedelta(days=6), prev + pd.Timedelta(days=1))
    if len(recent):
        first = recent.report_date.map(sessions.first_session_on_or_after)
        recent = recent[first == prev]
    history = data.reports(today - pd.Timedelta(days=365), prev - pd.Timedelta(days=6))
    cut = signals.threshold(history, cfg.quantile)
    if cut is None:
        return [], None, len(recent)
    found = [c for c in signals.candidates(recent, cut) if c.symbol not in held]
    return found, cut, len(recent)


def open_entries(book: book_store.Book, client: IGClient, cands: list[signals.Candidate], cfg: Config,
                 now: pd.Timestamp, lines: list[str], path: Path, dry_run: bool) -> int:
    today = now.tz_convert(NY).tz_localize(None).normalize()
    room = min(cfg.max_new_per_day, cfg.max_open - len(book.positions))
    terms = data.ig_terms([c.symbol for c in cands]).set_index("symbol") if cands else pd.DataFrame()
    hedges = hedge_markets(client, cfg)
    exit_session = sessions.add_sessions(today, cfg.hold_sessions)
    hedge = pick_hedge(hedges, exit_session)
    opened = 0
    for c in cands:
        if opened >= room:
            lines.append(f"{c.symbol}: skipped - the book is full ({cfg.max_open} open, {cfg.max_new_per_day} a day)")
            continue
        if c.symbol not in terms.index:
            lines.append(f"{c.symbol}: skipped - not on IG's US share list")
            continue
        epic, unborrowable = terms.loc[c.symbol, "epic"], terms.loc[c.symbol, "unborrowable"]
        if unborrowable is True:
            lines.append(f"{c.symbol}: skipped - IG can't short it")
            continue
        market = client.fetch_market(epic)
        status, mid = market["snapshot"]["marketStatus"], _mid(market)
        if status != "TRADEABLE" or mid is None:
            lines.append(f"{c.symbol}: skipped - IG market {status}")
            continue
        size = signals.stake(cfg.notional_per_trade, mid, float(market["dealingRules"]["minDealSize"]["value"]),
                             cfg.max_min_stake_ratio)
        if size is None:
            lines.append(f"{c.symbol}: skipped - IG's minimum stake would be over "
                         f"£{cfg.max_min_stake_ratio * cfg.notional_per_trade:,.0f} at {mid / 100:,.2f}")
            continue
        exposure = size * mid
        what = (f"{c.symbol}: surprise {c.surprise * 1e4:+.1f} bp of price (cut {c.threshold * 1e4:+.1f}), "
                f"short £{size:.2f}/pt ≈ £{exposure:,.0f}")
        if dry_run:
            lines.append("WOULD SHORT " + what)
            opened += 1
            continue
        confirm = client.open_market_position(epic, "DFB", "SELL", size, "GBP")
        if not deal_accepted(confirm):
            lines.append(f"{c.symbol}: short REFUSED by IG ({describe_rejection(confirm)})")
            log.warning("short of %s refused: %s", c.symbol, describe_rejection(confirm))
            continue
        pos = book_store.Position(
            symbol=c.symbol, epic=epic, deal_id=confirm["dealId"], size=size, entry_level=_level(confirm),
            exposure=round(exposure, 2), opened=now.isoformat(),
            exit_session=f"{exit_session:%Y-%m-%d}",
            report_date=f"{c.report_date:%Y-%m-%d}", surprise=c.surprise, threshold=c.threshold,
            name=market["instrument"].get("name"))
        book.positions.append(pos)
        book_store.save(book, path)
        if hedge is None:
            lines.append(f"{c.symbol}: no US 500 contract open to hedge with - short is unhedged")
            log.critical("no hedge contract for %s", c.symbol)
        else:
            hedge_size = max(round(exposure / hedge.mid, 2), hedge.min_size)
            h = client.open_market_position(hedge.epic, hedge.expiry, "BUY", hedge_size, "GBP")
            if deal_accepted(h):
                pos.hedge_deal_id, pos.hedge_size, pos.hedge_level = h["dealId"], hedge_size, _level(h)
                pos.hedge_epic, pos.hedge_expiry = hedge.epic, hedge.expiry
            else:
                lines.append(f"{c.symbol}: hedge REFUSED ({describe_rejection(h)}) - short is unhedged")
                log.critical("hedge for %s refused: %s", c.symbol, describe_rejection(h))
        book_store.save(book, path)
        lines.append("Shorted " + what + (f", hedged with US 500 {pos.hedge_expiry} £{pos.hedge_size:.2f}/pt"
                                          if pos.hedge_deal_id else ""))
        opened += 1
        time.sleep(PAUSE_BETWEEN_DEALS)
    return opened


def run(live: bool = False, dry_run: bool = False, force: bool = False, now: pd.Timestamp | None = None) -> None:
    cfg = load_config()
    now = now if now is not None else pd.Timestamp.now(tz="UTC")
    now_ny = now.tz_convert(NY)
    today = now_ny.tz_localize(None).normalize()
    account = "live" if live else "demo"
    if not (dry_run or force or in_window(now_ny, cfg)):
        return
    book = book_store.load(STATE, account)
    if book.last_session == f"{today:%Y-%m-%d}" and not (dry_run or force):
        return

    lines: list[str] = []
    client = IGClient(load_credentials(live=live))
    client.login()
    if not dry_run:
        open_now = reconcile(book, client, now, lines)
        closed_pnl = close_due(book, client, now, lines, STATE, open_now)
    else:
        closed_pnl = 0.0
    held = {p.symbol for p in book.positions}
    cands, cut, n_reports = plan_entries(today, cfg, held)
    if cut is None:
        lines.append("No trades: too little history to set the miss threshold")
    opened = open_entries(book, client, cands, cfg, now, lines, STATE, dry_run) if cands else 0

    summary = (f"{n_reports} report(s) from {sessions.previous_session(today):%a %d %b}, {len(cands)} miss(es) "
               f"at or below {cut * 1e4:+.1f} bp of price" if cut is not None else f"{n_reports} report(s)")
    log.info("%s%s; opened %d; %d open", "DRY RUN: " if dry_run else "", summary, opened, len(book.positions))
    for line in lines:
        log.info("  %s", line)
    if dry_run:
        print(summary)
        print("\n".join(lines) or "nothing to do")
        return
    book.last_session = f"{today:%Y-%m-%d}"
    book_store.save(book, STATE)
    if lines:
        subject = f"Earnings shorts: {opened} opened, {sum(1 for l in lines if l.startswith('Closed'))} closed"
        if any(l.startswith("Closed") for l in lines):
            subject += f" ({_pounds(closed_pnl)})"
        body = "\n".join([summary + ".", ""] + lines + ["", f"{len(book.positions)} short(s) open."])
        Notifier("earnings_short", load_email_config(), load_whatsapp_recipients(), account=account)._deliver_all(subject, body)


def main() -> None:
    parser = argparse.ArgumentParser(description="Short S&P 500 earnings misses, hedged with the US 500.")
    parser.add_argument("--live", action="store_true", help="trade the live account (default: demo)")
    parser.add_argument("--dry-run", action="store_true", help="show what it would do now; place no deals")
    parser.add_argument("--force", action="store_true", help="act now, outside the window and even if it ran today")
    parser.add_argument("--as-of", help="with --dry-run: plan as at 15:45 New York on this date (YYYY-MM-DD)")
    args = parser.parse_args()
    if args.as_of and not args.dry_run:
        parser.error("--as-of only works with --dry-run")
    now = (pd.Timestamp(args.as_of).tz_localize(NY) + pd.Timedelta(hours=15, minutes=45)).tz_convert("UTC") if args.as_of else None
    configure_logging(log_dir=LOGS)
    try:
        instance_lock.acquire(LOCK)
    except instance_lock.AlreadyRunningError:
        log.info("another run is in progress - exiting")
        return
    try:
        run(live=args.live, dry_run=args.dry_run, force=args.force, now=now)
    except Exception:
        log.exception("the run failed")
        try:
            Notifier("earnings_short", load_email_config(), load_whatsapp_recipients(),
                     account="live" if args.live else "demo")._deliver_all(
                "Earnings shorts: the run FAILED", "See logs/earnings_short/oil_pair.log.")
        except Exception:
            log.exception("could not send the failure message")
        raise
    finally:
        instance_lock.release(LOCK)


if __name__ == "__main__":
    main()
