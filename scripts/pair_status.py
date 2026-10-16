#!/usr/bin/env python3
"""Status report for every open pair, sent by email and WhatsApp.

Per open pair: demo or live, side, when it opened, each leg's opening and
current price, and

  gross P&L   - the legs' move at mid prices since opening, before any cost
  costs       - bid/offer (half to open, half to close now; today's spread
                stands in for the one paid at opening) plus IG's overnight
                financing and admin fees charged or credited so far
  net P&L     - gross less costs: about what closing now would bank,
                after financing

Overnight charges are shared out between positions in the same market as in
oil_pair.costs, which the close emails use too.

Usage:
    ./.venv/bin/python scripts/pair_status.py            # build and send
    ./.venv/bin/python scripts/pair_status.py --dry-run  # print only

Sent weekdays at 18:00 by launchd: see scripts/install_pair_status.sh. Each
run logs to logs/status/.
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from oil_pair import costs  # noqa: E402
from oil_pair.ig_client import IGClient  # noqa: E402
from oil_pair.logging_setup import configure_logging  # noqa: E402
from oil_pair.notify import Notifier, load_email_config, load_whatsapp_recipients  # noqa: E402
from oil_pair.settings import REPO_ROOT, load_credentials  # noqa: E402
from oil_pair.state_store import load_state  # noqa: E402
from oil_pair.strategy_logic import PairSide  # noqa: E402

UK = "Europe/London"


@dataclass(frozen=True)
class Leg:
    name: str
    epic: str
    deal_id: str
    direction: str            # BUY or SELL, as held
    size: float               # £ per point
    open_level: float         # the fill
    opened: pd.Timestamp      # UTC
    bid: float
    offer: float

    @property
    def sign(self) -> int:
        return 1 if self.direction == "BUY" else -1

    @property
    def half_spread(self) -> float:
        return (self.offer - self.bid) / 2

    @property
    def mid(self) -> float:
        return (self.bid + self.offer) / 2

    @property
    def open_mid(self) -> float:
        """The mid when it opened: a buy fills half a spread above it, a sell below."""
        return self.open_level - self.sign * self.half_spread

    def gross(self) -> float:
        return self.sign * self.size * (self.mid - self.open_mid)

    def spread_cost(self) -> float:
        return 2 * self.half_spread * self.size


@dataclass(frozen=True)
class PairStatus:
    name: str
    account: str
    side: str
    opened: pd.Timestamp
    legs: tuple[Leg, ...]
    financing: float          # + received, - paid
    spread_std: str           # e.g. "-2.02 std (take profit 0.00, stop -5.50)" or ""
    unbooked: bool = False    # held at the last 22:00 UK, but IG hasn't booked that charge yet

    @property
    def gross(self) -> float:
        return sum(leg.gross() for leg in self.legs)

    @property
    def spread_cost(self) -> float:
        return sum(leg.spread_cost() for leg in self.legs)

    @property
    def costs(self) -> float:
        return self.spread_cost - self.financing

    @property
    def net(self) -> float:
        return self.gross - self.costs


def money(v: float) -> str:
    return f"{'+' if v >= 0 else '-'}£{abs(v):,.2f}"


def format_report(statuses: list[PairStatus], flat: list[str], now: pd.Timestamp) -> tuple[str, str]:
    total = sum(s.net for s in statuses)
    subject = f"Status: {len(statuses)} open pair(s), net {money(total)}" if statuses else "Status: no open pairs"
    lines = [f"As at {now.tz_convert(UK):%a %d %b %H:%M} UK", ""]
    for s in statuses:
        held = (now - s.opened).total_seconds() / 86400
        lines.append(f"{s.name} ({s.account.upper()}) - {s.side} since {s.opened.tz_convert(UK):%a %d %b %H:%M} ({held:.1f} days)")
        for leg in s.legs:
            lines.append(f"  {leg.direction} £{leg.size:.2f}/pt {leg.name}: opened {leg.open_level:g}, now {leg.mid:g}")
        fin = (f"financing received {money(s.financing)}" if s.financing >= 0
               else f"financing paid {money(-s.financing)[1:]}")
        if s.unbooked:
            fin += "; IG hasn't booked the latest 22:00 UK charge yet"
        lines += [
            f"  Gross P&L: {money(s.gross)}",
            f"  Costs: {money(s.costs)[1:] if s.costs >= 0 else '-' + money(-s.costs)[1:]} "
            f"(bid/offer {money(s.spread_cost)[1:]}, {fin})",
            f"  P&L after costs: {money(s.net)}",
        ]
        if s.spread_std:
            lines.append(f"  Spread: {s.spread_std}")
        lines.append("")
    if statuses:
        lines.append(f"Total net across open pairs: {money(total)}")
    if flat:
        lines.append(f"Flat: {', '.join(flat)}")
    return subject, "\n".join(lines).strip()


def spread_line(pair: str, side: PairSide, limit_stds: float, stop_stds: float) -> str:
    log = REPO_ROOT / "logs" / pair / "oil_pair.log"
    if not log.exists():
        return ""
    found = None
    with log.open() as f:
        for line in f:
            if "__main__: spread=" in line:
                found = line
    m = re.search(r"\(([-\d.]+) std\)", found or "")
    if not m:
        return ""
    sign = 1 if side is PairSide.SHORT else -1
    return f"{float(m.group(1)):+.2f} std (take profit {sign * limit_stds + 0.0:+.2f}, stop {sign * stop_stds + 0.0:+.2f})"


def gather(dry_run: bool) -> tuple[list[PairStatus], list[str]]:
    statuses, flat, clients, positions, transactions = [], [], {}, {}, {}
    for cfg_path in sorted((REPO_ROOT / "config").glob("*/pair_config.toml")):
        pair = cfg_path.parent.name
        state = load_state(REPO_ROOT / "state" / pair / "run_state.json")
        if state is None or state.side is PairSide.FLAT:
            flat.append(pair)
            continue
        account = state.account or "demo"
        if account not in clients:
            client = IGClient(load_credentials(live=account == "live"))
            client.login()
            clients[account] = client
            positions[account] = client.fetch_open_positions()
            transactions[account] = costs.fetch_transactions(client, pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=60))
        cfg = tomllib.loads(cfg_path.read_text())
        names = {cfg[k]["epic"]: cfg[k]["name"] for k in ("instrument_a", "instrument_b")}
        pos = positions[account]
        legs = []
        for tracked in (state.leg_a, state.leg_b):
            if tracked is None:
                continue
            row = pos[pos.dealId == tracked.deal_id]
            if row.empty:
                continue
            r = row.iloc[0]
            legs.append(Leg(names.get(r.epic, r.instrumentName), r.epic, r.dealId, r.direction, float(r["size"]),
                            float(r.level), pd.Timestamp(r.createdDateUTC, tz="UTC"), float(r.bid), float(r.offer)))
        statuses.append((pair, account, state, cfg, legs))
    # charges are shared out among every position open in the account, this app's or not
    out, now = [], pd.Timestamp.now(tz="UTC")
    for pair, account, state, cfg, legs in statuses:
        pos, fin, unbooked = positions[account], 0.0, False
        for leg in legs:
            held = costs.Holding(leg.name, leg.size, leg.opened)
            others = costs.holdings(pos[pos.dealId != leg.deal_id])
            fin += costs.overnight_share(transactions[account], held, others)
            unbooked = unbooked or costs.unbooked(transactions[account], held, now)
        opened = min(leg.opened for leg in legs) if legs else now
        strat = cfg.get("strategy", {})
        out.append(PairStatus(pair, account, state.side.value, opened, tuple(legs), fin,
                              spread_line(pair, state.side, strat.get("limit_stds", 0), strat.get("stop_stds", 0)),
                              unbooked))
    return out, flat


def main() -> None:
    parser = argparse.ArgumentParser(description="Status report for every open pair.")
    parser.add_argument("--dry-run", action="store_true", help="print instead of sending")
    args = parser.parse_args()
    from dotenv import load_dotenv
    load_dotenv(REPO_ROOT / ".env")
    if not args.dry_run:
        configure_logging(log_dir=REPO_ROOT / "logs" / "status")
    statuses, flat = gather(args.dry_run)
    subject, body = format_report(statuses, flat, pd.Timestamp.now(tz="UTC"))
    if args.dry_run:
        print(f"SUBJECT: {subject}\n\n{body}")
        return
    # the open pairs' account(s); none when everything is flat
    account = "+".join(sorted({s.account for s in statuses})) or None
    Notifier("status", load_email_config(), load_whatsapp_recipients(), account=account)._deliver_all(subject, body)
    print(f"sent: {subject}")


if __name__ == "__main__":
    main()
