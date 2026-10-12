#!/usr/bin/env python3
"""Backtests a pair's strategy on stored prices, net of bid/offer.

Two price sources:

- hourly (default): ig-prices' hourly bars, <data-root>/<group>/1hour/
  <fmp_symbol>.parquet, closes only. Months of history, but decisions only
  at each bar's close.
- ticks: this app's own price_log/<pair_name>/ticks.csv. The live feed, so
  closest to what the app actually sees, but only as far back as the app
  has been running.

Strategy thresholds come from config/<pair_name>/pair_config.toml unless
overridden, entry sizing from config/risk.toml as live (--fixed-notional to
trade the pair's notional_trade_size instead), and costs from
config/backtest_costs.toml. See oil_pair/backtest.py
for exactly what is and isn't simulated.

Usage:
  backtest.py <pair_name> [--source hourly|ticks] [rule options] [--trades]
  backtest.py <pair_name> --compare      # the live rules against each refinement

Examples:
  backtest.py brent_wti --retrace-stds 0.25 --max-hold-hours 72 --trades
  backtest.py brent_gasoline --blocked-hours 22-6 --compare
"""

from __future__ import annotations

import argparse
import dataclasses
import sys
import tomllib
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from oil_pair import price_log
from oil_pair.backtest import LONDON, InstrumentCosts, Rules, parse_hours, run_backtest, summarize
from oil_pair.paths import REPO_ROOT, resolve as resolve_paths
from oil_pair.main import max_loss_for
from oil_pair.settings import InstrumentConfig, StrategyConfig, load_pair_config, load_risk_config

DEFAULT_DATA_ROOT = Path("~/ig-price-data").expanduser()
COSTS_PATH = REPO_ROOT / "config" / "backtest_costs.toml"


def load_costs(path: Path = COSTS_PATH) -> dict[str, InstrumentCosts]:
    with path.open("rb") as f:
        return {epic: InstrumentCosts(**values) for epic, values in tomllib.load(f).items()}


def load_hourly(instrument: InstrumentConfig, data_root: Path) -> pd.Series:
    if not instrument.fmp_symbol:
        raise SystemExit(f"{instrument.epic} has no fmp_symbol in its pair config, so its hourly file can't be found")
    matches = sorted(data_root.glob(f"*/1hour/{instrument.fmp_symbol}.parquet"))
    if not matches:
        raise SystemExit(f"no hourly file for {instrument.fmp_symbol} under {data_root}")
    bars = pd.read_parquet(matches[0])
    # ig-prices stores naive Europe/London times; drop the hour that a
    # clock change makes ambiguous rather than guess which one it was.
    index = bars.index.tz_localize(LONDON, ambiguous="NaT", nonexistent="NaT")
    closes = pd.Series(bars["close"].to_numpy(dtype=float), index=index)
    return closes[closes.index.notna()].tz_convert("UTC")


def load_prices(pair_name: str, source: str, data_root: Path) -> pd.DataFrame:
    config = load_pair_config(resolve_paths(pair_name).pair_config)
    a, b = config.instrument_a, config.instrument_b
    if source == "hourly":
        # Bars only count where both instruments printed one.
        return pd.DataFrame({a.epic: load_hourly(a, data_root), b.epic: load_hourly(b, data_root)}).dropna()

    log_path = resolve_paths(pair_name).price_log
    series_a = price_log.load_mid_series(a.epic, log_path)
    series_b = price_log.load_mid_series(b.epic, log_path)
    if series_a.empty or series_b.empty:
        raise SystemExit(f"{log_path} has no ticks for both legs")
    # Each logged tick is a decision point, priced at both legs' latest mid.
    prices = pd.DataFrame({a.epic: series_a, b.epic: series_b})
    prices = prices.groupby(level=0).last().sort_index().ffill().dropna()
    return prices


def compare_variants(base: Rules) -> list[tuple[str, Rules]]:
    """The live rules (plus any options given on the command line), then each
    refinement on top of them one at a time, then a couple of combinations."""
    replace = dataclasses.replace
    return [
        ("as configured", base),
        ("confirm 1h", replace(base, confirm_minutes=60)),
        ("confirm 3h", replace(base, confirm_minutes=180)),
        ("retrace 0.25σ", replace(base, retrace_stds=0.25)),
        ("retrace 0.5σ", replace(base, retrace_stds=0.5)),
        ("time stop 24h", replace(base, max_hold_hours=24)),
        ("time stop 72h", replace(base, max_hold_hours=72)),
        ("no entries 22-06", replace(base, blocked_entry_hours=base.blocked_entry_hours | parse_hours("22-6"))),
        ("retrace 0.25σ + stop 72h", replace(base, retrace_stds=0.25, max_hold_hours=72)),
        ("confirm 1h + retrace 0.25σ", replace(base, confirm_minutes=60, retrace_stds=0.25)),
    ]


def _parse_hours_arg(text: str) -> frozenset[int]:
    try:
        return parse_hours(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _row(name: str, stats: dict, first: dict, second: dict) -> str:
    return (
        f"{name:<28} {stats['trades']:>6} {stats['win_rate']:>5.0%} {stats['net']:>9.2f} {stats['per_trade']:>8.2f} "
        f"{stats['worst']:>8.2f} {stats['max_drawdown']:>8.2f} {stats['avg_hold_h']:>7.1f} {stats['cost']:>7.2f} "
        f"{stats['stops']:>5} {stats['time_stops']:>5}   {first['net']:>8.2f} ({first['trades']:>2}) "
        f"{second['net']:>8.2f} ({second['trades']:>2})"
    )


HEADER = (
    f"{'rules':<28} {'trades':>6} {'win':>5} {'net £':>9} {'£/trade':>8} {'worst':>8} {'max DD':>8} "
    f"{'hold h':>7} {'cost':>7} {'stops':>5} {'t/out':>5}   {'1st half (n)':>13} {'2nd half (n)':>13}"
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("pair_name")
    parser.add_argument("--source", choices=["hourly", "ticks"], default="hourly")
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    parser.add_argument("--since", help="ignore prices before this date, e.g. 2026-08-01")
    parser.add_argument("--entry-stds", type=float)
    parser.add_argument("--limit-stds", type=float)
    parser.add_argument("--stop-stds", type=float)
    parser.add_argument("--confirm-minutes", type=float, default=0.0)
    parser.add_argument("--retrace-stds", type=float, default=0.0)
    parser.add_argument("--max-hold-hours", type=float)
    parser.add_argument("--blocked-hours", type=_parse_hours_arg, default=frozenset(),
                        help="Europe/London hours with no new entries, e.g. 22-6 (end exclusive)")
    parser.add_argument("--fixed-notional", action="store_true",
                        help="size every entry at notional_trade_size, ignoring config/risk.toml")
    parser.add_argument("--compare", action="store_true", help="run the live rules and each refinement side by side")
    parser.add_argument("--trades", action="store_true", help="list every trade")
    args = parser.parse_args()

    pair_config = load_pair_config(resolve_paths(args.pair_name).pair_config)
    overrides = {
        name: value
        for name, value in (("entry_stds", args.entry_stds), ("limit_stds", args.limit_stds), ("stop_stds", args.stop_stds))
        if value is not None
    }
    strategy: StrategyConfig = dataclasses.replace(pair_config.strategy, **overrides)

    prices = load_prices(args.pair_name, args.source, args.data_root)
    if args.since:
        prices = prices[prices.index >= pd.Timestamp(args.since, tz="UTC")]
    costs = load_costs()
    risk = None if args.fixed_notional else load_risk_config()
    max_loss = max_loss_for(strategy, risk)

    base = Rules(
        confirm_minutes=args.confirm_minutes,
        retrace_stds=args.retrace_stds,
        max_hold_hours=args.max_hold_hours,
        blocked_entry_hours=args.blocked_hours,
    )
    variants = compare_variants(base) if args.compare else [("as given", base)]

    print(
        f"{args.pair_name}: {args.source} prices, {len(prices)} points, "
        f"{prices.index[0]:%Y-%m-%d %H:%M} to {prices.index[-1]:%Y-%m-%d %H:%M} UTC"
    )
    print(
        f"entry {strategy.entry_stds}σ, limit {strategy.limit_stds}σ, stop {strategy.stop_stds}σ, "
        + (f"sized for ~£{max_loss:g} lost at the stop" if max_loss is not None
           else f"£{strategy.notional_trade_size:g} per leg")
        + f", refit every {strategy.model_update_interval_business_days} "
        f"business days on up to {strategy.max_fit_lookback_days} days"
    )

    results = [(name, run_backtest(prices, strategy, costs, rules, risk)) for name, rules in variants]
    first_fit_at = results[0][1].first_fit_at
    if first_fit_at is None:
        raise SystemExit("never fitted a model: not enough data for the warmup")
    midpoint = first_fit_at + (prices.index[-1] - first_fit_at) / 2
    print(f"trading from {first_fit_at:%Y-%m-%d %H:%M}; halves split at {midpoint:%Y-%m-%d %H:%M} by entry time\n")

    print(HEADER)
    for name, result in results:
        first = [t for t in result.trades if t.entry_time < midpoint]
        second = [t for t in result.trades if t.entry_time >= midpoint]
        print(_row(name, summarize(result.trades), summarize(first), summarize(second)))

    if args.trades:
        for name, result in results:
            print(f"\n{name}:")
            for t in result.trades:
                print(
                    f"  {t.side.value:<5} {t.entry_time:%Y-%m-%d %H:%M} -> {t.exit_time:%Y-%m-%d %H:%M} "
                    f"({t.hold_hours:6.1f}h)  {t.entry_stds:+5.2f}σ -> {t.exit_stds:+5.2f}σ  "
                    f"{t.exit_reason.value:<12} gross {t.gross_pnl:8.2f}  cost {t.cost:6.2f}  net {t.net_pnl:8.2f}"
                )


if __name__ == "__main__":
    main()
