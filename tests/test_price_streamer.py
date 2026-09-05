"""Covers the bug where a market closing for the weekend (no new ticks, but
not a broken stream) got treated as a staleness error, tripping
MAX_CONSECUTIVE_ERRORS in main.py's loop within a couple of minutes of
every market close. latest_snapshot() SHOULD still raise on staleness (used
right before trading) - peek_snapshot() must not, since it's used only to
check tradeability."""

import time

import pytest

from oil_pair.price_streamer import PriceStreamer


@pytest.fixture
def streamer():
    return PriceStreamer(ig_client=None, epics=["EPIC.A", "EPIC.B"], max_staleness_seconds=0.05)


def test_peek_snapshot_returns_empty_dict_before_any_update(streamer):
    assert streamer.peek_snapshot() == {}


def test_peek_snapshot_never_raises_even_when_stale(streamer):
    streamer._handle_update("EPIC.A", 100.0, 101.0, "CLOSED")
    streamer._handle_update("EPIC.B", 50.0, 51.0, "CLOSED")
    time.sleep(0.1)  # well past max_staleness_seconds=0.05

    snapshot = streamer.peek_snapshot()  # must not raise

    assert snapshot["EPIC.A"].market_status == "CLOSED"
    assert snapshot["EPIC.B"].market_status == "CLOSED"


def test_peek_snapshot_reflects_last_known_market_status_over_a_closure():
    # Simulates: market was TRADEABLE, then IG pushes a MARKET_STATE update
    # when it closes for the weekend (bid/offer unchanged, status changes).
    streamer = PriceStreamer(ig_client=None, epics=["EPIC.A"], max_staleness_seconds=0.05)
    streamer._handle_update("EPIC.A", 100.0, 101.0, "TRADEABLE")
    streamer._handle_update("EPIC.A", 100.0, 101.0, "CLOSED")
    time.sleep(0.1)

    assert streamer.peek_snapshot()["EPIC.A"].market_status == "CLOSED"


def test_latest_snapshot_still_raises_on_missing_epic(streamer):
    streamer._handle_update("EPIC.A", 100.0, 101.0, "TRADEABLE")
    # EPIC.B never received anything

    with pytest.raises(RuntimeError):
        streamer.latest_snapshot()


def test_latest_snapshot_still_raises_on_staleness(streamer):
    streamer._handle_update("EPIC.A", 100.0, 101.0, "TRADEABLE")
    streamer._handle_update("EPIC.B", 50.0, 51.0, "TRADEABLE")
    time.sleep(0.1)  # past max_staleness_seconds=0.05

    with pytest.raises(RuntimeError):
        streamer.latest_snapshot()


def test_latest_snapshot_succeeds_with_fresh_data(streamer):
    streamer._handle_update("EPIC.A", 100.0, 101.0, "TRADEABLE")
    streamer._handle_update("EPIC.B", 50.0, 51.0, "TRADEABLE")

    snapshot = streamer.latest_snapshot()

    assert snapshot["EPIC.A"].mid == pytest.approx(100.5)
    assert snapshot["EPIC.B"].mid == pytest.approx(50.5)
