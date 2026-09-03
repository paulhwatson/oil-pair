import pandas as pd

from oil_pair.price_log import append_tick, load_mid_series


def test_load_mid_series_returns_empty_series_when_no_file_exists(tmp_path):
    series = load_mid_series("EPIC.A", tmp_path / "nonexistent.csv")

    assert series.empty


def test_append_then_load_roundtrips_values(tmp_path):
    path = tmp_path / "ticks.csv"
    t1 = pd.Timestamp("2026-01-01T10:00:00", tz="UTC")
    t2 = pd.Timestamp("2026-01-01T10:00:10", tz="UTC")

    append_tick("EPIC.A", 100.5, t1, path)
    append_tick("EPIC.A", 101.0, t2, path)

    series = load_mid_series("EPIC.A", path)

    assert list(series.values) == [100.5, 101.0]
    assert list(series.index) == [t1, t2]


def test_load_mid_series_filters_by_epic(tmp_path):
    path = tmp_path / "ticks.csv"
    t = pd.Timestamp("2026-01-01T10:00:00", tz="UTC")

    append_tick("EPIC.A", 100.0, t, path)
    append_tick("EPIC.B", 200.0, t, path)

    series_a = load_mid_series("EPIC.A", path)
    series_b = load_mid_series("EPIC.B", path)

    assert list(series_a.values) == [100.0]
    assert list(series_b.values) == [200.0]


def test_load_mid_series_returns_empty_for_unknown_epic(tmp_path):
    path = tmp_path / "ticks.csv"
    append_tick("EPIC.A", 100.0, pd.Timestamp("2026-01-01T10:00:00", tz="UTC"), path)

    series = load_mid_series("EPIC.NOT_PRESENT", path)

    assert series.empty


def test_append_tick_sorts_out_of_order_writes(tmp_path):
    path = tmp_path / "ticks.csv"
    t1 = pd.Timestamp("2026-01-01T10:00:00", tz="UTC")
    t2 = pd.Timestamp("2026-01-01T10:00:10", tz="UTC")

    # write newer sample first
    append_tick("EPIC.A", 101.0, t2, path)
    append_tick("EPIC.A", 100.0, t1, path)

    series = load_mid_series("EPIC.A", path)

    assert list(series.values) == [100.0, 101.0]
