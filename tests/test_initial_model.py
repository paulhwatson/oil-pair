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
    n = MIN_FIT_OBSERVATIONS + 10  # hourly, since fits sample one price per hour
    start = now - pd.Timedelta(hours=n)
    cached_a = _series(rng, n, start, freq_minutes=60.0, base=100.0)
    cached_b = _series(rng, n, start, freq_minutes=60.0, base=50.0)

    model = try_build_initial_model(cached_a, cached_b, [], strategy, "EPIC.A", "EPIC.B", now)

    assert model is not None
    assert model.i1_key == "EPIC.A"  # higher mean price
    assert model.i2_key == "EPIC.B"


def test_combines_cached_and_buffered_data(rng):
    strategy = StrategyConfig(warmup_minutes=30)
    now = pd.Timestamp("2026-01-01T12:00:00", tz="UTC")
    # cached alone is short of MIN_FIT_OBSERVATIONS (hourly points), ending
    # well before the buffer starts so no hour holds both
    start = now - pd.Timedelta(hours=36)
    cached_a = _series(rng, MIN_FIT_OBSERVATIONS - 10, start, freq_minutes=60.0, base=100.0)
    cached_b = _series(rng, MIN_FIT_OBSERVATIONS - 10, start, freq_minutes=60.0, base=50.0)
    # buffer supplies the rest, one point an hour up to `now`
    buffer = [
        (now - pd.Timedelta(hours=i), 100.0 + i * 0.01, 50.0 + i * 0.01)
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


def test_a_days_warmup_is_satisfied_once_there_are_enough_hours(rng):
    strategy = StrategyConfig(warmup_minutes=1440)
    now = pd.Timestamp("2026-01-01T12:00:00", tz="UTC")
    start = now - pd.Timedelta(hours=31)
    cached_a = _series(rng, 31 * 12, start, freq_minutes=5.0, base=100.0)
    cached_b = _series(rng, 31 * 12, start, freq_minutes=5.0, base=50.0)

    assert try_build_initial_model(cached_a, cached_b, [], strategy, "A", "B", now) is not None


def test_a_dense_day_is_not_enough_without_thirty_hours(rng):
    """The cost of sampling hourly: a cold start needs MIN_FIT_OBSERVATIONS
    (30) distinct hours, not just a day's span - however many ticks."""
    strategy = StrategyConfig(warmup_minutes=1440)
    now = pd.Timestamp("2026-01-01T12:00:00", tz="UTC")
    start = now - pd.Timedelta(hours=25)
    cached_a = _series(rng, 25 * 240, start, freq_minutes=0.25, base=100.0)  # a 15s feed
    cached_b = _series(rng, 25 * 240, start, freq_minutes=0.25, base=50.0)

    assert try_build_initial_model(cached_a, cached_b, [], strategy, "A", "B", now) is None


def test_a_restart_against_a_month_of_cache_fits_immediately(rng):
    """The point of keeping the cache: a restart does not re-serve the warmup."""
    strategy = StrategyConfig(warmup_minutes=1440, max_fit_lookback_days=30)
    now = pd.Timestamp("2026-02-01T12:00:00", tz="UTC")
    cached_a = _series(rng, 1000, now - pd.Timedelta(days=29), freq_minutes=40.0, base=100.0)
    cached_b = _series(rng, 1000, now - pd.Timedelta(days=29), freq_minutes=40.0, base=50.0)

    model = try_build_initial_model(cached_a, cached_b, [], strategy, "A", "B", now)

    assert model is not None
    # 1000 points 40 minutes apart cover ~667 distinct hours - one per hour is fitted
    assert 660 <= model.n_observations <= 670


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


def test_a_dense_burst_of_ticks_does_not_sway_the_fit(rng):
    """Real incident: two days of live 15s ticks (~240/hour) outweighed 30
    days of hourly backfill (1/hour) in an unweighted fit, putting std_spread
    ~2x off in both directions across the live pairs. Fits now take one
    price per hour, so adding noisy intra-hour ticks - keeping the last tick
    in each hour unchanged - must not change the model at all."""
    strategy = StrategyConfig(warmup_minutes=1440, max_fit_lookback_days=30)
    now = pd.Timestamp("2026-06-01T12:00:00", tz="UTC")
    hours = pd.date_range(now - pd.Timedelta(days=29), now, freq="1h", inclusive="left")
    a = pd.Series(100.0 + np.cumsum(rng.normal(0, 0.3, len(hours))), index=hours)
    b = 0.5 * a + rng.normal(0, 0.2, len(hours))

    # Like the real price log: both legs share the same unique tick timestamps.
    minutes = pd.to_timedelta(range(1, 59), unit="min")

    def with_burst(series):
        extra = []
        for h in series.index[-48:]:
            extra.append(pd.Series(series[h] + rng.normal(0, 5.0, len(minutes)), index=h + minutes))
        # each hour's real value lands last within its hour, so hourly sampling keeps it
        last = series.copy()
        last.index = last.index + pd.Timedelta(minutes=59)
        return pd.concat([last, *extra]).sort_index()

    plain = try_build_initial_model(a, b, [], strategy, "A", "B", now)
    bursty = try_build_initial_model(with_burst(a), with_burst(b), [], strategy, "A", "B", now)

    assert plain is not None and bursty is not None
    assert bursty.n_observations == plain.n_observations
    assert bursty.hedge_ratio == pytest.approx(plain.hedge_ratio)
    assert bursty.std_spread == pytest.approx(plain.std_spread)


def test_time_even_keeps_the_last_price_in_each_hour():
    from oil_pair.main import _time_even

    idx = pd.to_datetime(
        ["2026-01-01T10:05Z", "2026-01-01T10:55Z", "2026-01-01T11:30Z", "2026-01-01T13:10Z"]
    )
    out = _time_even(pd.Series([1.0, 2.0, 3.0, 4.0], index=idx))

    assert list(out.to_numpy()) == [2.0, 3.0, 4.0]  # empty 12:00 hour dropped, not filled


def test_fit_window_of_zero_days_is_treated_as_no_limit(rng):
    """Guards against a misconfigured 0 silently throwing away every tick."""
    from oil_pair.main import _fit_window

    now = pd.Timestamp("2026-06-01T12:00:00", tz="UTC")
    series = _series(rng, 50, now - pd.Timedelta(days=5), freq_minutes=60.0)

    assert len(_fit_window(series, now, lookback_days=0)) == 50
