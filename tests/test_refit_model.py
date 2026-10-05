"""refit_model reads the price log, not an in-memory buffer.

The buffer only ever held one refit interval, which is less than
max_fit_lookback_days; the log holds everything and is windowed instead.
"""

import numpy as np
import pandas as pd
import pytest

from oil_pair.main import refit_model
from oil_pair.price_log import append_tick
from oil_pair.settings import InstrumentConfig, PairConfig, StrategyConfig
from oil_pair.strategy_logic import MIN_FIT_OBSERVATIONS

EPIC_A = "CC.D.LCO.USS.IP"
EPIC_B = "CC.D.RB.USS.IP"
NOW = pd.Timestamp("2026-06-01T12:00:00", tz="UTC")


@pytest.fixture
def rng():
    return np.random.default_rng(11)


def pair_config(**strategy) -> PairConfig:
    return PairConfig(
        instrument_a=InstrumentConfig(epic=EPIC_A, name="A", expiry="DFB", currency_code="GBP"),
        instrument_b=InstrumentConfig(epic=EPIC_B, name="B", expiry="DFB", currency_code="GBP"),
        strategy=StrategyConfig(**strategy),
    )


def write_log(path, rng, n, start, freq_minutes):
    """A price log as the live loop writes it: both epics, every tick."""
    index = pd.date_range(start, periods=n, freq=f"{freq_minutes}min", tz="UTC")
    a = 100.0 + np.cumsum(rng.normal(0, 0.1, size=n))
    b = 50.0 + np.cumsum(rng.normal(0, 0.1, size=n))
    for ts, va, vb in zip(index, a, b):
        append_tick(EPIC_A, float(va), ts, path)
        append_tick(EPIC_B, float(vb), ts, path)
    return path


def test_refits_from_the_price_log(tmp_path, rng):
    path = write_log(tmp_path / "ticks.csv", rng, 500, NOW - pd.Timedelta(days=10), 20.0)

    model = refit_model(pair_config(), EPIC_A, EPIC_B, path, NOW)

    assert model is not None
    assert model.i1_key == EPIC_A  # higher mean price
    # 500 ticks 20 minutes apart cover 167 distinct hours - one price per hour is fitted
    assert model.n_observations == 167


def test_the_refit_window_is_capped_at_max_fit_lookback_days(tmp_path, rng):
    """Six months in the log, one month in the fit."""
    path = write_log(tmp_path / "ticks.csv", rng, 180 * 24, NOW - pd.Timedelta(days=180), 60.0)

    model = refit_model(pair_config(max_fit_lookback_days=30), EPIC_A, EPIC_B, path, NOW)

    assert model is not None
    assert model.n_observations <= 30 * 24 + 1
    assert model.n_observations > 29 * 24


def test_the_refit_uses_more_than_one_refit_interval(tmp_path, rng):
    """The reason for the change: a 7-business-day buffer would have seen
    about a week; the log gives the whole lookback."""
    path = write_log(tmp_path / "ticks.csv", rng, 30 * 24, NOW - pd.Timedelta(days=30), 60.0)

    model = refit_model(
        pair_config(model_update_interval_business_days=7, max_fit_lookback_days=30),
        EPIC_A, EPIC_B, path, NOW,
    )

    assert model is not None
    assert model.n_observations > 7 * 24  # more than one refit interval's worth


def test_a_thin_window_keeps_the_existing_model(tmp_path, rng):
    """Returns None rather than raising, so the caller holds what it has
    instead of dropping to no model mid-position."""
    path = write_log(tmp_path / "ticks.csv", rng, MIN_FIT_OBSERVATIONS - 5, NOW - pd.Timedelta(hours=2), 1.0)

    assert refit_model(pair_config(), EPIC_A, EPIC_B, path, NOW) is None


def test_a_log_that_is_entirely_stale_gives_no_refit(tmp_path, rng):
    """Everything is older than the lookback, so the window is empty."""
    path = write_log(tmp_path / "ticks.csv", rng, 500, NOW - pd.Timedelta(days=200), 60.0)

    assert refit_model(pair_config(max_fit_lookback_days=30), EPIC_A, EPIC_B, path, NOW) is None


def test_a_missing_log_gives_no_refit(tmp_path):
    assert refit_model(pair_config(), EPIC_A, EPIC_B, tmp_path / "absent.csv", NOW) is None


def test_thresholds_come_from_the_strategy_config(tmp_path, rng):
    path = write_log(tmp_path / "ticks.csv", rng, 500, NOW - pd.Timedelta(days=10), 20.0)

    model = refit_model(
        pair_config(entry_stds=2.0, limit_stds=0.5, stop_stds=4.0), EPIC_A, EPIC_B, path, NOW
    )

    assert model.entry_threshold == pytest.approx(model.std_spread * 2.0)
    assert model.limit_threshold == pytest.approx(model.std_spread * 0.5)
    assert model.stop_threshold == pytest.approx(model.std_spread * 4.0)
