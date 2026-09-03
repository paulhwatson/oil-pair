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
