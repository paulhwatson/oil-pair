"""Covers the bug where a market closing for the weekend (no new ticks, but
not a broken stream) got treated as a staleness error, tripping
MAX_CONSECUTIVE_ERRORS in main.py's loop within a couple of minutes of
every market close. latest_snapshot() SHOULD still raise on staleness (used
right before trading) - peek_snapshot() must not, since it's used only to
check tradeability."""

import time

import pytest

from oil_pair.price_streamer import PriceStreamer, _MarketListener


@pytest.fixture
def streamer():
    return PriceStreamer(ig_client=None, epics=["EPIC.A", "EPIC.B"], max_staleness_seconds=0.05)


class FakeStreamService:
    def __init__(self):
        self.subscribe_calls = 0

    def subscribe(self, subscription):
        self.subscribe_calls += 1


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


def test_status_only_update_with_no_price_still_updates_market_status(streamer):
    # Real incident: closing for the weekend, IG pushed MARKET_STATE=CLOSED
    # with null BID/OFFER (no valid quote once closed). The old
    # _handle_update dropped the whole update whenever bid/offer were None,
    # discarding the status change along with the missing price - so
    # peek_snapshot() kept reporting "TRADEABLE" straight through the
    # close, the loop kept calling latest_snapshot(), and it crashed on
    # staleness a couple of minutes later. Confirm the status now lands
    # even without a fresh price.
    streamer._handle_update("EPIC.A", 100.0, 101.0, "TRADEABLE")
    streamer._handle_update("EPIC.A", None, None, "CLOSED")

    snapshot = streamer.peek_snapshot()["EPIC.A"]
    assert snapshot.market_status == "CLOSED"
    assert snapshot.mid == pytest.approx(100.5)  # last known price retained


def test_status_only_update_does_not_refresh_staleness_clock(streamer):
    # No real price arrived, so this must not look "fresh" to latest_snapshot().
    streamer._handle_update("EPIC.A", 100.0, 101.0, "TRADEABLE")
    streamer._handle_update("EPIC.B", 50.0, 51.0, "TRADEABLE")
    time.sleep(0.1)  # past max_staleness_seconds=0.05
    streamer._handle_update("EPIC.A", None, None, "CLOSED")

    with pytest.raises(RuntimeError):
        streamer.latest_snapshot()


def test_status_only_update_ignored_before_any_real_price_seen(streamer):
    # Nothing to attach the status to yet - must not raise or fabricate a
    # snapshot with no price.
    streamer._handle_update("EPIC.A", None, None, "CLOSED")

    assert streamer.peek_snapshot() == {}


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


def test_subscription_starts_not_broken(streamer):
    assert streamer.subscription_is_broken() is False


def test_subscription_error_marks_broken():
    # Real incident: IG tears down the whole subscription at a weekend
    # close (onUnsubscription then onSubscriptionError code=21 "Invalid
    # group"), with no automatic client-side reconnect. main.py's loop
    # polls subscription_is_broken() to notice this and resubscribe.
    streamer = PriceStreamer(ig_client=None, epics=["EPIC.A"])
    listener = _MarketListener(streamer)

    listener.onSubscriptionError(21, "Invalid group")

    assert streamer.subscription_is_broken() is True


def test_unsubscription_marks_broken():
    streamer = PriceStreamer(ig_client=None, epics=["EPIC.A"])
    listener = _MarketListener(streamer)

    listener.onUnsubscription()

    assert streamer.subscription_is_broken() is True


def test_resubscribe_clears_broken_flag_and_resubscribes(streamer):
    streamer._stream_service = FakeStreamService()
    streamer._mark_subscription_broken()

    streamer.resubscribe()

    assert streamer.subscription_is_broken() is False
    assert streamer._stream_service.subscribe_calls == 1
