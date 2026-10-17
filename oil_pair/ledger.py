"""Every pair trade on an IG account, rebuilt from IG's transaction history.

IG's history lists each closed deal - market, signed size, opening and
closing time and level, realised P&L - but not which pair it belonged to.
The legs of one pair trade open within seconds of each other, so deals
opened within LEG_WINDOW of each other, in different markets, are one trade.
(Two rows for one market with the same opening time and level are one
position closed in two parts.) Which pair the trade was comes from, in order:

  1. the pairs' own logs, which record every leg they open;
  2. the markets in the trade, when they are exactly one pair's two markets;
  3. for a lone leg - its partner was refused, or it was left unpaired - the
     only pair trading that market at the time.

Financing and admin fees come from the same history (oil_pair.costs), so a
trade's P&L after costs is what IG actually booked for it.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

import pandas as pd

from oil_pair import costs

UK = "Europe/London"
LEG_WINDOW = pd.Timedelta(seconds=60)
# A log line about a leg or an exit belongs to a deal this close to it in time.
LOG_MATCH_WITHIN = pd.Timedelta(minutes=2)

_LOG_TIME = r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ INFO "
_OPEN_LINE = re.compile(_LOG_TIME + r"oil_pair\.ig_client: opening market position: (?:BUY|SELL) (\S+) size=")
_EXIT_LINE = re.compile(_LOG_TIME + r"__main__: decision: Action\.EXIT_(TAKE_PROFIT|STOP_LOSS)")


@dataclass(frozen=True)
class PairMarkets:
    pair: str
    epics: dict[str, str] = field(hash=False)  # epic -> IG's instrument name, for both legs

    @property
    def names(self) -> frozenset[str]:
        return frozenset(self.epics.values())


@dataclass(frozen=True)
class ClosedLeg:
    name: str              # IG's instrument name
    size: float            # £ per point, signed: + bought, - sold
    opened: pd.Timestamp   # UTC
    closed: pd.Timestamp   # UTC
    open_level: float
    close_level: float
    profit: float          # IG's realised P&L, after bid/offer


@dataclass(frozen=True)
class LogEvent:
    pair: str
    at: pd.Timestamp       # UTC
    what: str              # an IG instrument name opened, or "take profit" / "stop loss"


@dataclass(frozen=True)
class Trade:
    pair: str
    account: str
    legs: tuple[ClosedLeg, ...]
    financing: float = 0.0  # + received, - paid
    exit: str = ""          # "take profit" or "stop loss", when the pair's log says

    @property
    def opened(self) -> pd.Timestamp:
        return min(leg.opened for leg in self.legs)

    @property
    def closed(self) -> pd.Timestamp:
        return max(leg.closed for leg in self.legs)

    @property
    def realised(self) -> float:
        return sum(leg.profit for leg in self.legs)

    @property
    def net(self) -> float:
        return self.realised + self.financing

    @property
    def markets(self) -> list[str]:
        return list(dict.fromkeys(leg.name for leg in self.legs))

    @property
    def side(self) -> str:
        """LONG or SHORT the spread, as the app names it: the spread is the
        higher-priced leg less the other, so SHORT means that leg was sold."""
        if len(self.markets) != 2:
            return ""
        top = max(self.legs, key=lambda leg: leg.open_level)
        held = sum(leg.size for leg in self.legs if leg.name == top.name)
        return "SHORT" if held < 0 else "LONG"

    @property
    def note(self) -> str:
        if len(self.markets) == 1:
            return "one leg only - its partner was refused or left unpaired"
        return ""


def closed_legs(transactions: pd.DataFrame) -> list[ClosedLeg]:
    if transactions.empty:
        return []
    deals = transactions[transactions.transactionType == "DEAL"]
    return [
        ClosedLeg(
            name=str(d.instrumentName), size=costs._amount(d["size"]),
            opened=costs.utc(d.openDateUtc), closed=costs.utc(d.dateUtc),
            open_level=float(d.openLevel), close_level=float(d.closeLevel),
            profit=costs._amount(d.profitAndLoss),
        )
        for _, d in deals.iterrows()
    ]


def group(legs: list[ClosedLeg]) -> list[tuple[ClosedLeg, ...]]:
    """Legs that opened together, as one trade each."""
    groups: list[list[ClosedLeg]] = []
    for leg in sorted(legs, key=lambda leg: leg.opened):
        current = groups[-1] if groups else None
        if current and leg.opened - current[0].opened <= LEG_WINDOW and (
            leg.name not in {g.name for g in current}
            or any(g.name == leg.name and g.opened == leg.opened and g.open_level == leg.open_level for g in current)
        ):
            current.append(leg)
        else:
            groups.append([leg])
    return [tuple(g) for g in groups]


def read_log(path: Path, pair: PairMarkets) -> list[LogEvent]:
    """The legs this pair opened, and its exits, from one of its log files."""
    events = []
    with path.open(errors="replace") as f:
        for line in f:
            if "opening market position" not in line and "decision: Action.EXIT_" not in line:
                continue
            if m := _OPEN_LINE.match(line):
                if m.group(2) in pair.epics:
                    events.append(LogEvent(pair.pair, _uk_to_utc(m.group(1)), pair.epics[m.group(2)]))
            elif m := _EXIT_LINE.match(line):
                events.append(LogEvent(pair.pair, _uk_to_utc(m.group(1)), m.group(2).replace("_", " ").lower()))
    return [e for e in events if not pd.isna(e.at)]


def _uk_to_utc(text: str) -> pd.Timestamp:
    return pd.Timestamp(text).tz_localize(UK, ambiguous="NaT", nonexistent="NaT").tz_convert("UTC")


def build_trades(
    transactions: pd.DataFrame,
    pairs: list[PairMarkets],
    account: str,
    log_events: list[LogEvent] = (),
    open_positions: list[costs.Holding] = (),
) -> list[Trade]:
    """Every closed pair trade in the history, oldest first, with its share of
    the overnight charges booked while it was held."""
    groups = group(closed_legs(transactions))
    by_markets = {p.names: p.pair for p in pairs}
    first_paired: dict[str, pd.Timestamp] = {}
    for g in groups:
        name = by_markets.get(frozenset(leg.name for leg in g))
        if name and len({leg.name for leg in g}) == 2:
            first_paired.setdefault(name, g[0].opened)

    trades = []
    for g in groups:
        pair = _from_logs(g, log_events) or _from_markets(g, pairs, by_markets, first_paired) or "unassigned"
        trades.append(Trade(pair, account, g))

    others = list(open_positions)
    exits = [e for e in log_events if e.what in ("take profit", "stop loss")]
    out = []
    for t in trades:
        financing = sum(
            costs.overnight_share(transactions, costs.Holding(leg.name, abs(leg.size), leg.opened), others, until=leg.closed)
            for leg in t.legs
        )
        near = [e for e in exits if e.pair == t.pair and abs(e.at - t.closed) <= LOG_MATCH_WITHIN]
        exit_ = min(near, key=lambda e: abs(e.at - t.closed)).what if near else ""
        out.append(replace(t, financing=financing, exit=exit_))
    return out


def _from_logs(g: tuple[ClosedLeg, ...], events: list[LogEvent]) -> str | None:
    found = {
        e.pair for leg in g for e in events
        if e.what == leg.name and abs(e.at - leg.opened) <= LOG_MATCH_WITHIN
    }
    return found.pop() if len(found) == 1 else None


def _from_markets(g, pairs, by_markets, first_paired) -> str | None:
    markets = frozenset(leg.name for leg in g)
    if markets in by_markets:
        return by_markets[markets]
    if len(markets) != 1:
        return None
    (name,) = markets
    candidates = [p.pair for p in pairs if name in p.names]
    if len(candidates) > 1:
        # the pairs that had already traded both their markets by then; if
        # none had yet, the first of them to
        started = {c: first_paired[c] for c in candidates if c in first_paired}
        earlier = [c for c, at in started.items() if at <= g[0].opened]
        candidates = earlier or ([min(started, key=started.get)] if started else candidates)
    return candidates[0] if len(candidates) == 1 else None


def load_pairs(config_dir: Path) -> list[PairMarkets]:
    """Every configured pair's markets (config/<pair>/pair_config.toml)."""
    pairs = []
    for path in sorted(config_dir.glob("*/pair_config.toml")):
        cfg = tomllib.loads(path.read_text())
        epics = {cfg[k]["epic"]: cfg[k]["name"] for k in ("instrument_a", "instrument_b")}
        pairs.append(PairMarkets(path.parent.name, epics))
    return pairs


def read_pair_logs(pairs: list[PairMarkets], logs_dir: Path) -> list[LogEvent]:
    """The legs every pair opened, and its exits, from its logs (logs/<pair>/oil_pair.log*)."""
    events = []
    for pair in pairs:
        for path in sorted((logs_dir / pair.pair).glob("oil_pair.log*")):
            events += read_log(path, pair)
    return events


def ig_closed_trades(client, pairs: list[PairMarkets], logs_dir: Path, since: pd.Timestamp,
                     account: str = "demo") -> list[Trade]:
    """Every closed pair trade in the account's IG history since `since`."""
    transactions = costs.fetch_transactions(client, since)
    open_now = costs.holdings(client.fetch_open_positions())
    return build_trades(transactions, pairs, account, read_pair_logs(pairs, logs_dir), open_now)


def banked_by_day(trades: list[Trade], pairs: list[str], days: pd.DatetimeIndex) -> pd.DataFrame:
    """Each pair's P&L after costs from closed trades, cumulative, at the end of each UK day."""
    out = pd.DataFrame(0.0, index=days, columns=pairs)
    for t in trades:
        if t.pair in out.columns:
            closed_day = t.closed.tz_convert(UK).normalize().tz_localize(None)
            out.loc[out.index >= closed_day, t.pair] += t.net
    return out
