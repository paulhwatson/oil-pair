import numpy as np
import pandas as pd
import pytest

from oil_pair.backtest import ExitReason, InstrumentCosts, Rules, parse_hours, run_backtest, summarize
from oil_pair.settings import RiskConfig, StrategyConfig
from oil_pair.strategy_logic import PairSide, compute_leg_size, fit_model

WARMUP_BARS = 60
STRATEGY = StrategyConfig(
    notional_trade_size=1000.0,
    model_update_interval_business_days=100,  # no refit inside a test
    entry_stds=2.0,
    limit_stds=1.0,
    stop_stds=5.0,
    warmup_minutes=(WARMUP_BARS - 1) * 60,  # first fit on the last warmup bar
    max_fit_lookback_days=30,
)
COSTS = {"a": InstrumentCosts(bid_offer_points=1.0, min_deal_size=0.01), "b": InstrumentCosts(2.0, 0.01)}


def build_prices(rng, zs, start="2026-01-01 00:00"):
    """Hourly bars (the fit takes one price an hour): WARMUP_BARS of b ~ 0.5 * a plus noise, then one bar
    per entry of `zs` with `a` held still and `b` placed so the spread sits
    exactly z standard deviations out under the model fitted on the warmup."""
    a = 100.0 + np.cumsum(rng.normal(0, 0.2, WARMUP_BARS))
    b = 0.5 * a + rng.normal(0, 0.05, WARMUP_BARS)
    index = pd.date_range(start, periods=WARMUP_BARS + len(zs), freq="1h", tz="UTC")
    warmup = pd.DataFrame({"a": a, "b": b}, index=index[:WARMUP_BARS])

    model = fit_model({"a": warmup["a"], "b": warmup["b"]}, STRATEGY.entry_stds, STRATEGY.limit_stds, STRATEGY.stop_stds)
    assert model.i1_key == "a"
    a_after = np.full(len(zs), a[-1])
    b_after = model.hedge_ratio * a_after - np.asarray(zs) * model.std_spread
    after = pd.DataFrame({"a": a_after, "b": b_after}, index=index[WARMUP_BARS:])
    return pd.concat([warmup, after]), model


def bar_time(prices, i):
    """Timestamp of the i-th bar after the warmup."""
    return prices.index[WARMUP_BARS + i]


def test_default_rules_enter_on_the_first_bar_beyond_entry_and_charge_bid_offer(rng):
    prices, model = build_prices(rng, [0.0, 2.5, 1.0, 0.5])

    result = run_backtest(prices, STRATEGY, COSTS)

    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.side is PairSide.SHORT
    assert trade.entry_time == bar_time(prices, 1)
    assert trade.exit_time == bar_time(prices, 3)  # 1.0 is not yet < limit 1.0
    assert trade.exit_reason is ExitReason.TAKE_PROFIT

    size_a = compute_leg_size(1000.0, prices["a"].iloc[WARMUP_BARS + 1], 0.01)
    size_b = compute_leg_size(1000.0, prices["b"].iloc[WARMUP_BARS + 1], 0.01)
    # SHORT the spread = sell a, buy b. a didn't move; b rose by 2 std.
    assert trade.gross_pnl == pytest.approx(size_b * (2.5 - 0.5) * model.std_spread)
    assert trade.cost == pytest.approx(size_a * 1.0 + size_b * 2.0)
    assert trade.net_pnl == pytest.approx(trade.gross_pnl - trade.cost)


def test_long_side_when_spread_is_below_negative_entry(rng):
    prices, _ = build_prices(rng, [0.0, -2.5, -0.5])

    result = run_backtest(prices, STRATEGY, COSTS)

    assert [t.side for t in result.trades] == [PairSide.LONG]
    assert result.trades[0].gross_pnl > 0


def test_confirm_minutes_skips_a_one_bar_spike_and_waits_for_persistence(rng):
    prices, _ = build_prices(rng, [0.0, 2.5, 0.0, 0.0, 2.5, 2.5, 2.5, 2.5, 0.5])

    result = run_backtest(prices, STRATEGY, COSTS, Rules(confirm_minutes=180))

    assert len(result.trades) == 1
    assert result.trades[0].entry_time == bar_time(prices, 7)  # beyond since bar 4, + 3 hours


def test_retrace_waits_for_the_spread_to_come_back_from_its_extreme(rng):
    prices, _ = build_prices(rng, [0.0, 2.5, 3.0, 2.8, 2.4, 0.5])

    result = run_backtest(prices, STRATEGY, COSTS, Rules(retrace_stds=0.5))

    assert len(result.trades) == 1
    assert result.trades[0].entry_time == bar_time(prices, 4)
    assert result.trades[0].entry_stds == pytest.approx(2.4)


def test_retrace_can_enter_inside_the_entry_band(rng):
    prices, _ = build_prices(rng, [0.0, 2.2, 1.6, 0.5])

    result = run_backtest(prices, STRATEGY, COSTS, Rules(retrace_stds=0.5))

    assert [t.entry_time for t in result.trades] == [bar_time(prices, 2)]


def test_retrace_gives_up_once_the_spread_is_back_inside_the_take_profit_band(rng):
    prices, _ = build_prices(rng, [0.0, 2.5, 3.0, 0.5, 0.0])

    result = run_backtest(prices, STRATEGY, COSTS, Rules(retrace_stds=0.5))

    assert result.trades == []


def test_time_stop_closes_and_blocks_reentry_until_the_spread_is_back_inside_entry(rng):
    prices, _ = build_prices(rng, [0.0, 2.5, 2.5, 2.5, 2.5, 2.5, 2.5, 0.0, 2.5, 0.5])

    result = run_backtest(prices, STRATEGY, COSTS, Rules(max_hold_hours=3))

    assert [(t.exit_reason, t.exit_time) for t in result.trades] == [
        (ExitReason.TIME_STOP, bar_time(prices, 4)),
        (ExitReason.TAKE_PROFIT, bar_time(prices, 9)),
    ]
    assert result.trades[1].entry_time == bar_time(prices, 8)


def test_stop_loss(rng):
    prices, _ = build_prices(rng, [0.0, 2.5, 5.5, 0.0])

    result = run_backtest(prices, STRATEGY, COSTS)

    assert [t.exit_reason for t in result.trades] == [ExitReason.STOP_LOSS]
    assert result.trades[0].net_pnl < 0


def test_blocked_hours_prevent_entries_in_london_time(rng):
    # 60 hourly warmup bars from 2026-07-01 00:00 UTC put the entry bar at
    # 2026-07-03 13:00 UTC, which is 14:00 in London (BST).
    prices, _ = build_prices(rng, [0.0, 2.5, 0.5], start="2026-07-01 00:00")
    assert bar_time(prices, 1) == pd.Timestamp("2026-07-03 13:00", tz="UTC")

    assert len(run_backtest(prices, STRATEGY, COSTS, Rules(blocked_entry_hours=frozenset({13}))).trades) == 1
    blocked = run_backtest(prices, STRATEGY, COSTS, Rules(blocked_entry_hours=frozenset({14}))).trades
    assert [t.entry_time for t in blocked] == []  # by 15:00 London the spread has reverted


def test_position_still_open_at_the_end_is_marked_to_market(rng):
    prices, _ = build_prices(rng, [0.0, 2.5, 2.5])

    result = run_backtest(prices, STRATEGY, COSTS)

    assert [t.exit_reason for t in result.trades] == [ExitReason.END_OF_DATA]


def test_no_trades_before_the_warmup_has_elapsed(rng):
    prices, _ = build_prices(rng, [])
    strategy = StrategyConfig(**{**STRATEGY.__dict__, "warmup_minutes": 100_000})

    result = run_backtest(prices, strategy, COSTS)

    assert result.first_fit_at is None
    assert result.trades == []


def test_rejects_prices_without_costs(rng):
    prices, _ = build_prices(rng, [0.0])

    with pytest.raises(ValueError):
        run_backtest(prices, STRATEGY, {"a": COSTS["a"]})


def test_summarize_drawdown_and_counts(rng):
    prices, _ = build_prices(rng, [0.0, 2.5, 0.5, 2.5, 5.5, 0.0, 2.5, 0.5])
    # Costs small enough that the take-profits are winners net, so the only
    # drawdown is the stop.
    cheap = {key: InstrumentCosts(bid_offer_points=0.001, min_deal_size=0.01) for key in ("a", "b")}
    trades = run_backtest(prices, STRATEGY, cheap).trades
    stats = summarize(trades)

    assert stats["trades"] == 3
    assert stats["stops"] == 1
    assert stats["win_rate"] == pytest.approx(2 / 3)
    assert stats["net"] == pytest.approx(sum(t.net_pnl for t in trades))
    assert stats["max_drawdown"] == pytest.approx(-trades[1].net_pnl)
    assert summarize([])["trades"] == 0


def test_parse_hours():
    assert parse_hours("22-6,12") == frozenset({22, 23, 0, 1, 2, 3, 4, 5, 12})
    assert parse_hours("9") == frozenset({9})
    assert parse_hours("") == frozenset()
    with pytest.raises(ValueError):
        parse_hours("22-30")
    with pytest.raises(ValueError):
        parse_hours("1-2-3")


def test_risk_sizing_sizes_legs_from_the_loss_at_the_stop(rng):
    prices, model = build_prices(rng, [0.0, 2.5, 0.5])
    risk = RiskConfig(max_loss_per_trade=50.0, max_notional_per_leg=1e9)

    trade = run_backtest(prices, STRATEGY, COSTS, risk=risk).trades[0]
    fixed = run_backtest(prices, STRATEGY, COSTS).trades[0]

    # notional = max_loss * price_i2 / ((stop - entry) * std); same trade, rescaled.
    price_b = prices["b"].iloc[WARMUP_BARS + 1]
    notional = 50.0 * price_b / ((STRATEGY.stop_stds - STRATEGY.entry_stds) * model.std_spread)
    size_b = compute_leg_size(notional, price_b, 0.01)
    assert trade.gross_pnl == pytest.approx(size_b * (2.5 - 0.5) * model.std_spread)
    assert trade.gross_pnl != pytest.approx(fixed.gross_pnl)


def test_entry_skipped_when_ig_minimum_stake_needs_more_than_1_5x_the_risk(rng):
    prices, _ = build_prices(rng, [0.0, 2.5, 0.5])
    # £1 a leg is 0.02/pt of b at ~50, so b's 0.04 minimum needs 2x the notional.
    costs = {"a": InstrumentCosts(1.0, 0.01), "b": InstrumentCosts(2.0, 0.04)}
    big = StrategyConfig(**{**STRATEGY.__dict__, "notional_trade_size": 1.0})

    assert run_backtest(prices, big, costs).trades == []
