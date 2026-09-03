import numpy as np
import pandas as pd
import pytest

from conftest import make_price_series
from oil_pair.strategy_logic import (
    Action,
    PairSide,
    compute_leg_size,
    compute_spread,
    fit_model,
    next_decision,
)


def test_fit_model_recovers_known_hedge_ratio(rng):
    # x must stay the higher-priced series so it lands as i1 (the regressor);
    # fit_model then estimates hedge_ratio = k directly.
    x = make_price_series(rng, n=200, start=100.0, noise_std=0.2)
    k = 0.4
    noise = rng.normal(0, 0.01, size=len(x))
    y = k * x.to_numpy() + noise

    model = fit_model({"x": x, "y": pd.Series(y, index=x.index)}, entry_stds=1.5, limit_stds=1.0, stop_stds=3.0)

    assert model.hedge_ratio == pytest.approx(k, rel=0.05)


def test_fit_model_orders_instruments_by_mean_price(rng):
    higher = make_price_series(rng, n=200, start=200.0, noise_std=0.1)
    lower = make_price_series(rng, n=200, start=50.0, noise_std=0.1)

    model_ab = fit_model({"higher": higher, "lower": lower}, entry_stds=1.5, limit_stds=1.0, stop_stds=3.0)
    model_ba = fit_model({"lower": lower, "higher": higher}, entry_stds=1.5, limit_stds=1.0, stop_stds=3.0)

    assert model_ab.i1_key == "higher"
    assert model_ab.i2_key == "lower"
    assert model_ba.i1_key == "higher"
    assert model_ba.i2_key == "lower"


def test_fit_model_raises_on_insufficient_data(rng):
    x = make_price_series(rng, n=5)
    y = make_price_series(rng, n=5)

    with pytest.raises(ValueError):
        fit_model({"x": x, "y": y}, entry_stds=1.5, limit_stds=1.0, stop_stds=3.0)


def test_fit_model_rejects_wrong_number_of_instruments(rng):
    x = make_price_series(rng, n=50)

    with pytest.raises(ValueError):
        fit_model({"x": x}, entry_stds=1.5, limit_stds=1.0, stop_stds=3.0)


def test_compute_spread_matches_formula():
    assert compute_spread(mid_i1=100.0, mid_i2=40.0, hedge_ratio=0.5) == pytest.approx(10.0)


def _model(entry=2.0, limit=1.0, stop=4.0):
    return fit_model(
        {
            "i1": pd.Series(
                np.linspace(100, 100, 50), index=pd.date_range("2026-01-01", periods=50, freq="1min", tz="UTC")
            ),
            "i2": pd.Series(
                np.linspace(50, 50, 50) + np.array([0.01 if i % 2 else -0.01 for i in range(50)]),
                index=pd.date_range("2026-01-01", periods=50, freq="1min", tz="UTC"),
            ),
        },
        entry_stds=entry,
        limit_stds=limit,
        stop_stds=stop,
    )


def test_next_decision_enters_short_above_entry_threshold():
    model = _model()
    decision = next_decision(spread=model.entry_threshold + 0.5, model=model, current_side=PairSide.FLAT, stopped_out=False)

    assert decision.action is Action.ENTER
    assert decision.side is PairSide.SHORT
    assert decision.stopped_out is False


def test_next_decision_enters_long_below_negative_entry_threshold():
    model = _model()
    decision = next_decision(spread=-model.entry_threshold - 0.5, model=model, current_side=PairSide.FLAT, stopped_out=False)

    assert decision.action is Action.ENTER
    assert decision.side is PairSide.LONG
    assert decision.stopped_out is False


def test_next_decision_no_reentry_while_stopped_out_until_band_reentered():
    model = _model()

    still_stopped = next_decision(spread=model.entry_threshold + 0.5, model=model, current_side=PairSide.FLAT, stopped_out=True)
    assert still_stopped.action is Action.NONE
    assert still_stopped.stopped_out is True

    back_in_band = next_decision(spread=0.0, model=model, current_side=PairSide.FLAT, stopped_out=True)
    assert back_in_band.action is Action.NONE
    assert back_in_band.stopped_out is False

    re_entry = next_decision(spread=model.entry_threshold + 0.5, model=model, current_side=PairSide.FLAT, stopped_out=False)
    assert re_entry.action is Action.ENTER
    assert re_entry.side is PairSide.SHORT


def test_next_decision_exit_take_profit_short():
    model = _model()
    decision = next_decision(spread=model.limit_threshold - 0.01, model=model, current_side=PairSide.SHORT, stopped_out=False)

    assert decision.action is Action.EXIT_TAKE_PROFIT
    assert decision.side is PairSide.FLAT


def test_next_decision_exit_take_profit_long():
    model = _model()
    decision = next_decision(spread=-model.limit_threshold + 0.01, model=model, current_side=PairSide.LONG, stopped_out=False)

    assert decision.action is Action.EXIT_TAKE_PROFIT
    assert decision.side is PairSide.FLAT


def test_next_decision_exit_stop_loss_sets_stopped_out_true_short():
    model = _model()
    decision = next_decision(spread=model.stop_threshold + 0.01, model=model, current_side=PairSide.SHORT, stopped_out=False)

    assert decision.action is Action.EXIT_STOP_LOSS
    assert decision.side is PairSide.FLAT
    assert decision.stopped_out is True


def test_next_decision_exit_stop_loss_sets_stopped_out_true_long():
    model = _model()
    decision = next_decision(spread=-model.stop_threshold - 0.01, model=model, current_side=PairSide.LONG, stopped_out=False)

    assert decision.action is Action.EXIT_STOP_LOSS
    assert decision.side is PairSide.FLAT
    assert decision.stopped_out is True


def test_next_decision_holds_short_between_limit_and_stop():
    model = _model()
    midpoint = (model.limit_threshold + model.stop_threshold) / 2
    decision = next_decision(spread=midpoint, model=model, current_side=PairSide.SHORT, stopped_out=False)

    assert decision.action is Action.NONE
    assert decision.side is PairSide.SHORT


def test_compute_leg_size_rounds_to_two_decimal_places():
    size = compute_leg_size(notional_trade_size=1000, mid_price=97.0, min_deal_size=0.04)

    assert size == pytest.approx(round(1000 / 97.0, 2))


def test_compute_leg_size_floors_at_minimum_deal_size():
    size = compute_leg_size(notional_trade_size=1, mid_price=1000.0, min_deal_size=0.5)

    assert size == pytest.approx(0.5)


def test_compute_leg_size_rejects_nonpositive_mid_price():
    with pytest.raises(ValueError):
        compute_leg_size(notional_trade_size=1000, mid_price=0.0, min_deal_size=0.04)
