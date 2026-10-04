import numpy as np
import pandas as pd
import pytest

from oil_pair.main import try_build_initial_model
from oil_pair.settings import StrategyConfig
from oil_pair.strategy_logic import MIN_FIT_OBSERVATIONS


def _series(rng, n, start, freq_minutes, base=100.0, noise_std=0.1):
    index = pd.date_range(start, periods=n, freq=f"{freq_minutes}min", tz="UTC")
    values = base + np.cumsum(rng.normal(0, noise_std, size=n))
    return pd.Series(values, index=index)


@pytest.fixture
def rng():
    return np.random.default_rng(7)


def test_returns_none_with_too_few_points(rng):
    strategy = StrategyConfig(warmup_minutes=5)
    now = pd.Timestamp("2026-01-01T12:00:00", tz="UTC")
    buffer = [(now, 100.0, 50.0)] * 5  # far fewer than MIN_FIT_OBSERVATIONS

    model = try_build_initial_model(
        pd.Series(dtype=float), pd.Series(dtype=float), buffer, strategy, "A", "B", now
    )

    assert model is None


def test_returns_none_when_span_too_short_despite_enough_points(rng):
    strategy = StrategyConfig(warmup_minutes=60)
    now = pd.Timestamp("2026-01-01T12:00:00", tz="UTC")
    # MIN_FIT_OBSERVATIONS points, all within the last minute - plenty of
    # points but nowhere near warmup_minutes of actual elapsed time.
    cached_a = _series(rng, MIN_FIT_OBSERVATIONS, now - pd.Timedelta(seconds=50), freq_minutes=0.05)
    cached_b = _series(rng, MIN_FIT_OBSERVATIONS, now - pd.Timedelta(seconds=50), freq_minutes=0.05, base=50.0)

    model = try_build_initial_model(cached_a, cached_b, [], strategy, "A", "B", now)

    assert model is None


def test_returns_model_once_points_and_span_thresholds_met(rng):
    strategy = StrategyConfig(warmup_minutes=30, entry_stds=1.5, limit_stds=1.0, stop_stds=3.0)
    now = pd.Timestamp("2026-01-01T12:00:00", tz="UTC")
    start = now - pd.Timedelta(minutes=40)
    cached_a = _series(rng, MIN_FIT_OBSERVATIONS + 10, start, freq_minutes=1.0, base=100.0)
    cached_b = _series(rng, MIN_FIT_OBSERVATIONS + 10, start, freq_minutes=1.0, base=50.0)

    model = try_build_initial_model(cached_a, cached_b, [], strategy, "EPIC.A", "EPIC.B", now)

    assert model is not None
    assert model.i1_key == "EPIC.A"  # higher mean price
    assert model.i2_key == "EPIC.B"


def test_combines_cached_and_buffered_data(rng):
    strategy = StrategyConfig(warmup_minutes=30)
    now = pd.Timestamp("2026-01-01T12:00:00", tz="UTC")
    start = now - pd.Timedelta(minutes=40)
    # cached alone is short of MIN_FIT_OBSERVATIONS
    cached_a = _series(rng, MIN_FIT_OBSERVATIONS - 10, start, freq_minutes=1.0, base=100.0)
    cached_b = _series(rng, MIN_FIT_OBSERVATIONS - 10, start, freq_minutes=1.0, base=50.0)
    # buffer supplies the rest, spanning up to `now`
    buffer = [
        (now - pd.Timedelta(minutes=i), 100.0 + i * 0.01, 50.0 + i * 0.01)
        for i in range(15, 0, -1)
    ]

    model = try_build_initial_model(cached_a, cached_b, buffer, strategy, "EPIC.A", "EPIC.B", now)

    assert model is not None
    assert model.n_observations == len(cached_a) + len(buffer)


def test_waits_a_full_day_when_warmup_is_a_day(rng):
    """The shipped default: two hours of ticks is plenty of points but is not
    a day, so there is still no model."""
    strategy = StrategyConfig(warmup_minutes=1440)
    now = pd.Timestamp("2026-01-01T12:00:00", tz="UTC")
    start = now - pd.Timedelta(hours=2)
    cached_a = _series(rng, 200, start, freq_minutes=0.5, base=100.0)
    cached_b = _series(rng, 200, start, freq_minutes=0.5, base=50.0)

    assert try_build_initial_model(cached_a, cached_b, [], strategy, "A", "B", now) is None


def test_a_days_warmup_is_satisfied_by_a_days_cache(rng):
    strategy = StrategyConfig(warmup_minutes=1440)
    now = pd.Timestamp("2026-01-01T12:00:00", tz="UTC")
    start = now - pd.Timedelta(hours=25)
    cached_a = _series(rng, 300, start, freq_minutes=5.0, base=100.0)
    cached_b = _series(rng, 300, start, freq_minutes=5.0, base=50.0)

    assert try_build_initial_model(cached_a, cached_b, [], strategy, "A", "B", now) is not None


def test_a_restart_against_a_month_of_cache_fits_immediately(rng):
    """The point of keeping the cache: a restart does not re-serve the warmup."""
    strategy = StrategyConfig(warmup_minutes=1440, max_fit_lookback_days=30)
    now = pd.Timestamp("2026-02-01T12:00:00", tz="UTC")
    cached_a = _series(rng, 1000, now - pd.Timedelta(days=29), freq_minutes=40.0, base=100.0)
    cached_b = _series(rng, 1000, now - pd.Timedelta(days=29), freq_minutes=40.0, base=50.0)

    model = try_build_initial_model(cached_a, cached_b, [], strategy, "A", "B", now)

    assert model is not None
    assert model.n_observations == 1000


def test_the_fit_uses_at_most_max_fit_lookback_days(rng):
    """A cache reaching back six months only contributes its last month."""
    strategy = StrategyConfig(warmup_minutes=1440, max_fit_lookback_days=30)
    now = pd.Timestamp("2026-06-01T12:00:00", tz="UTC")
    # One point an hour for 180 days; only the last 30 days may be fitted.
    cached_a = _series(rng, 180 * 24, now - pd.Timedelta(days=180), freq_minutes=60.0, base=100.0)
    cached_b = _series(rng, 180 * 24, now - pd.Timedelta(days=180), freq_minutes=60.0, base=50.0)

    model = try_build_initial_model(cached_a, cached_b, [], strategy, "A", "B", now)

    assert model is not None
    assert model.n_observations <= 30 * 24 + 1
    assert model.n_observations > 29 * 24


def test_data_entirely_older_than_the_lookback_gives_no_model(rng):
    """A cache from months ago is not a head start - the window empties it."""
    strategy = StrategyConfig(warmup_minutes=1440, max_fit_lookback_days=30)
    now = pd.Timestamp("2026-06-01T12:00:00", tz="UTC")
    start = now - pd.Timedelta(days=120)
    cached_a = _series(rng, 500, start, freq_minutes=60.0, base=100.0)
    cached_b = _series(rng, 500, start, freq_minutes=60.0, base=50.0)

    assert try_build_initial_model(cached_a, cached_b, [], strategy, "A", "B", now) is None


def test_fit_window_keeps_only_the_recent_tail(rng):
    from oil_pair.main import _fit_window

    now = pd.Timestamp("2026-06-01T12:00:00", tz="UTC")
    series = _series(rng, 100, now - pd.Timedelta(days=50), freq_minutes=60.0 * 12)

    windowed = _fit_window(series, now, lookback_days=30)

    assert len(windowed) < len(series)
    assert windowed.index.min() >= now - pd.Timedelta(days=30)
    assert windowed.index.max() == series.index.max()


def test_fit_window_passes_an_empty_series_through():
    from oil_pair.main import _fit_window

    now = pd.Timestamp("2026-06-01T12:00:00", tz="UTC")
    assert _fit_window(pd.Series(dtype=float), now, 30).empty


def test_fit_window_of_zero_days_is_treated_as_no_limit(rng):
    """Guards against a misconfigured 0 silently throwing away every tick."""
    from oil_pair.main import _fit_window

    now = pd.Timestamp("2026-06-01T12:00:00", tz="UTC")
    series = _series(rng, 50, now - pd.Timedelta(days=5), freq_minutes=60.0)

    assert len(_fit_window(series, now, lookback_days=0)) == 50
