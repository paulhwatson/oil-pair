"""Replays a price history through the live strategy, with optional entry/exit
refinements, and charges each fill IG's bid/offer.

Pure: no IG or network access, so it is unit-testable on synthetic prices.
The replay mirrors oil_pair/main.py's loop as closely as a bar-by-bar replay
can:

- the first fit waits for `warmup_minutes` of data, then fits on the last
  `max_fit_lookback_days` (try_build_initial_model);
- the model is refit every `model_update_interval_business_days`, after that
  bar's decision, on the same lookback (refit_model); an open position keeps
  running against the new thresholds, as it does live;
- each fit sees one price per hour (main._time_even), whatever the density
  of the input;
- decisions come from strategy_logic.next_decision; each entry is sized as
  main.apply_decision sizes it - from config/risk.toml when there is one
  (main.entry_notional), with both legs raised together to meet IG's minimum
  stake and the entry skipped when that would exceed 1.5x the risk
  (main._meet_minimum_stakes) - and leg directions follow
  main.direction_for_leg (a SHORT pair sells i1 and buys i2).

On top of that, `Rules` adds the refinements under evaluation. Each one
defaults to off, so `Rules()` is the live strategy exactly:

- confirm_minutes: the spread must stay beyond the entry threshold, on the
  same side, for this long before an entry - filters single-tick spikes;
- retrace_stds: once confirmed, wait for the spread to come back this many
  standard deviations from its furthest point before entering ("wait for the
  turn"), instead of entering while it may still be widening;
- max_hold_hours: close a position that has not reverted within this long
  (a time stop), since the price stop is typically 5-7σ away;
- blocked_entry_hours: Europe/London hours of the day in which no new
  position is opened. Exits are never blocked.

What a bar-close replay cannot see: anything that happens inside a bar (on
hourly data a stop could be breached and recovered within the hour), slippage
beyond the quoted bid/offer, and overnight funding.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

import pandas as pd

from oil_pair.main import EntryFailedError, _meet_minimum_stakes, _time_even, direction_for_leg, entry_notional
from oil_pair.settings import InstrumentConfig, RiskConfig, StrategyConfig
from oil_pair.strategy_logic import (
    Action,
    Decision,
    Model,
    PairSide,
    compute_leg_size,
    compute_spread,
    fit_model,
    next_decision,
)

LONDON = "Europe/London"


@dataclass(frozen=True)
class InstrumentCosts:
    # Offer minus bid, in price points. Half is paid on every fill.
    bid_offer_points: float
    min_deal_size: float


@dataclass(frozen=True)
class Rules:
    confirm_minutes: float = 0.0
    retrace_stds: float = 0.0
    max_hold_hours: float | None = None
    blocked_entry_hours: frozenset[int] = field(default_factory=frozenset)


class ExitReason(Enum):
    TAKE_PROFIT = "take_profit"
    STOP_LOSS = "stop_loss"
    TIME_STOP = "time_stop"
    END_OF_DATA = "end_of_data"


@dataclass(frozen=True)
class Trade:
    side: PairSide
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    entry_stds: float  # spread / std_spread at entry
    exit_stds: float
    exit_reason: ExitReason
    gross_pnl: float  # mid to mid, in account currency
    cost: float  # bid/offer paid on all four fills

    @property
    def net_pnl(self) -> float:
        return self.gross_pnl - self.cost

    @property
    def hold_hours(self) -> float:
        return (self.exit_time - self.entry_time).total_seconds() / 3600


@dataclass(frozen=True)
class BacktestResult:
    trades: list[Trade]
    first_fit_at: pd.Timestamp | None
    start: pd.Timestamp
    end: pd.Timestamp


@dataclass
class _Leg:
    key: str
    direction: int  # +1 bought, -1 sold
    size: float
    entry_mid: float


def run_backtest(
    prices: pd.DataFrame,
    strategy: StrategyConfig,
    costs: dict[str, InstrumentCosts],
    rules: Rules = Rules(),
    risk: RiskConfig | None = None,
) -> BacktestResult:
    """`prices` has exactly two columns (one per instrument, named as in
    `costs`) of mid prices on a tz-aware DatetimeIndex, sorted, no NaNs.
    Each row is one decision point, like one live poll. `risk` sizes entries
    as config/risk.toml does live; None trades the fixed notional_trade_size."""
    if prices.shape[1] != 2:
        raise ValueError(f"prices must have exactly 2 columns, got {list(prices.columns)}")
    if prices.index.tz is None:
        raise ValueError("prices index must be tz-aware")
    missing = [c for c in prices.columns if c not in costs]
    if missing:
        raise ValueError(f"no costs for {missing}")

    key_a, key_b = prices.columns
    timestamps = prices.index
    values_a = prices[key_a].to_numpy(dtype=float)
    values_b = prices[key_b].to_numpy(dtype=float)
    start = timestamps[0]

    model: Model | None = None
    first_fit_at: pd.Timestamp | None = None
    next_refit_at: pd.Timestamp | None = None

    side = PairSide.FLAT
    stopped_out = False
    legs: list[_Leg] = []
    entry_time: pd.Timestamp | None = None
    entry_stds = 0.0
    trades: list[Trade] = []

    # Entry-refinement state, all reset whenever the spread leaves the zone.
    beyond_side: PairSide | None = None  # which side of the entry band the spread is on
    beyond_since: pd.Timestamp | None = None
    extreme = 0.0  # furthest |spread| seen since beyond_since

    def fit(now: pd.Timestamp) -> Model | None:
        window = prices[prices.index >= now - pd.Timedelta(days=strategy.max_fit_lookback_days)]
        window = window[window.index <= now]
        try:
            return fit_model(
                {key_a: _time_even(window[key_a]), key_b: _time_even(window[key_b])},
                strategy.entry_stds,
                strategy.limit_stds,
                strategy.stop_stds,
                fitted_at=now,
            )
        except ValueError:
            return None

    def close(now: pd.Timestamp, mids: dict[str, float], spread_stds: float, reason: ExitReason) -> None:
        gross = sum(leg.size * (mids[leg.key] - leg.entry_mid) * leg.direction for leg in legs)
        cost = sum(leg.size * costs[leg.key].bid_offer_points for leg in legs)
        trades.append(Trade(side, entry_time, now, entry_stds, spread_stds, reason, gross, cost))

    for i, now in enumerate(timestamps):
        mids = {key_a: values_a[i], key_b: values_b[i]}

        if model is None:
            if (now - start).total_seconds() / 60 < strategy.warmup_minutes:
                continue
            model = fit(now)
            if model is not None:
                first_fit_at = now
                next_refit_at = now + pd.offsets.BusinessDay(strategy.model_update_interval_business_days)
            continue

        spread = compute_spread(mids[model.i1_key], mids[model.i2_key], model.hedge_ratio)
        spread_stds = spread / model.std_spread if model.std_spread else 0.0

        if side is PairSide.FLAT:
            decision = next_decision(spread, model, side, stopped_out)
            stopped_out = decision.stopped_out

            if decision.action is Action.ENTER:
                if beyond_side is not decision.side:
                    beyond_side, beyond_since, extreme = decision.side, now, abs(spread)
                extreme = max(extreme, abs(spread))
            elif beyond_side is not None and not _still_armed(spread, beyond_side, model, rules):
                beyond_side = beyond_since = None

            enter_side = _entry_side(now, spread, model, rules, beyond_side, beyond_since, extreme, decision)
            if enter_side is not None and now.tz_convert(LONDON).hour not in rules.blocked_entry_hours:
                sizes = _entry_sizes(strategy, risk, model, mids, costs, enter_side)
                if sizes is not None:
                    side = enter_side
                    entry_time, entry_stds = now, spread_stds
                    legs = [
                        _Leg(key, 1 if direction_for_leg(side, is_i1_leg=(key == model.i1_key)) == "BUY" else -1,
                             sizes[key], mids[key])
                        for key in (model.i1_key, model.i2_key)
                    ]
                    beyond_side = beyond_since = None
        else:
            decision = next_decision(spread, model, side, stopped_out)
            reason = None
            if decision.action is Action.EXIT_TAKE_PROFIT:
                reason = ExitReason.TAKE_PROFIT
            elif decision.action is Action.EXIT_STOP_LOSS:
                reason = ExitReason.STOP_LOSS
            elif rules.max_hold_hours is not None and now - entry_time >= pd.Timedelta(hours=rules.max_hold_hours):
                # Blocks re-entry until the spread is back inside the entry
                # band, as a stop loss does: otherwise a spread that is still
                # stretched would be re-entered on the very next bar.
                reason = ExitReason.TIME_STOP
                decision = Decision(decision.action, PairSide.FLAT, True, "time stop")

            stopped_out = decision.stopped_out
            if reason is not None:
                close(now, mids, spread_stds, reason)
                side, legs, entry_time = PairSide.FLAT, [], None

        if now >= next_refit_at:
            refitted = fit(now)
            if refitted is not None:
                model = refitted
            next_refit_at = now + pd.offsets.BusinessDay(strategy.model_update_interval_business_days)

    if side is not PairSide.FLAT:
        last = {key_a: values_a[-1], key_b: values_b[-1]}
        spread = compute_spread(last[model.i1_key], last[model.i2_key], model.hedge_ratio)
        close(timestamps[-1], last, spread / model.std_spread, ExitReason.END_OF_DATA)

    return BacktestResult(trades=trades, first_fit_at=first_fit_at, start=start, end=timestamps[-1])


def _entry_sizes(
    strategy: StrategyConfig,
    risk: RiskConfig | None,
    model: Model,
    mids: dict[str, float],
    costs: dict[str, InstrumentCosts],
    side: PairSide,
) -> dict[str, float] | None:
    """Leg sizes for an entry, or None where the app would decline it for
    IG's minimum stake. Live, a declined entry also pauses entries for 30
    minutes and up; here it is simply retried on the next bar."""
    notional, sizing = entry_notional(strategy, risk, model, mids)
    keys = (model.i1_key, model.i2_key)
    minimums = {key: costs[key].min_deal_size for key in keys}
    directions = {key: direction_for_leg(side, is_i1_leg=(key == model.i1_key)) for key in keys}
    instruments = {key: InstrumentConfig(epic=key, name=key, expiry="", currency_code="") for key in keys}
    try:
        notional, _ = _meet_minimum_stakes(notional, sizing, directions, minimums, mids, instruments)
    except EntryFailedError:
        return None
    return {key: compute_leg_size(notional, mids[key], minimums[key]) for key in keys}


def _still_armed(spread: float, beyond_side: PairSide, model: Model, rules: Rules) -> bool:
    """Whether a spread that has dropped back inside the entry band is still
    a candidate for a retrace entry. Only the retrace rule enters inside the
    band; and once the spread is back inside the take-profit band, the
    reversion has already happened without us."""
    if rules.retrace_stds <= 0:
        return False
    signed = spread if beyond_side is PairSide.SHORT else -spread
    return signed > model.limit_threshold


def _entry_side(
    now: pd.Timestamp,
    spread: float,
    model: Model,
    rules: Rules,
    beyond_side: PairSide | None,
    beyond_since: pd.Timestamp | None,
    extreme: float,
    decision: Decision,
) -> PairSide | None:
    if beyond_side is None:
        return None
    if now - beyond_since < pd.Timedelta(minutes=rules.confirm_minutes):
        return None
    if rules.retrace_stds <= 0:
        return beyond_side if decision.action is Action.ENTER else None
    if abs(spread) <= extreme - rules.retrace_stds * model.std_spread:
        return beyond_side
    return None


def parse_hours(text: str) -> frozenset[int]:
    """'22-6,12' -> {22, 23, 0, 1, ..., 5, 12}. A range's end is exclusive and
    may wrap past midnight."""
    hours: set[int] = set()
    for part in filter(None, text.split(",")):
        bounds = [int(x) for x in part.split("-")]
        if len(bounds) > 2 or any(not 0 <= h <= 23 for h in bounds):
            raise ValueError(f"hours must be 0-23, as H or H-H: {text}")
        if len(bounds) == 1:
            hours.add(bounds[0])
            continue
        hour, end = bounds
        while hour != end:
            hours.add(hour)
            hour = (hour + 1) % 24
    return frozenset(hours)


def summarize(trades: list[Trade]) -> dict[str, float]:
    if not trades:
        return {"trades": 0, "net": 0.0, "per_trade": 0.0, "win_rate": 0.0, "worst": 0.0,
                "max_drawdown": 0.0, "avg_hold_h": 0.0, "cost": 0.0, "stops": 0, "time_stops": 0}
    net = [t.net_pnl for t in trades]
    equity = pd.Series(net).cumsum()
    drawdown = (equity.cummax().clip(lower=0) - equity).max()
    return {
        "trades": len(trades),
        "net": float(sum(net)),
        "per_trade": float(sum(net) / len(net)),
        "win_rate": sum(n > 0 for n in net) / len(net),
        "worst": float(min(net)),
        "max_drawdown": float(drawdown),
        "avg_hold_h": sum(t.hold_hours for t in trades) / len(trades),
        "cost": float(sum(t.cost for t in trades)),
        "stops": sum(t.exit_reason is ExitReason.STOP_LOSS for t in trades),
        "time_stops": sum(t.exit_reason is ExitReason.TIME_STOP for t in trades),
    }
