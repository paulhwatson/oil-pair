import numpy as np
import pandas as pd
import pytest


@pytest.fixture
def rng():
    return np.random.default_rng(seed=42)


def make_price_series(rng, n=200, start=100.0, drift=0.0, noise_std=0.05):
    steps = rng.normal(loc=drift, scale=noise_std, size=n)
    prices = start + np.cumsum(steps)
    index = pd.date_range("2026-01-01", periods=n, freq="1min", tz="UTC")
    return pd.Series(prices, index=index)
