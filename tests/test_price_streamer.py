"""Covers three incidents in this streamer's history:

1. A market closing for the weekend (no new ticks, but not a broken stream)
   got treated as a staleness error, tripping MAX_CONSECUTIVE_ERRORS in
   main.py's loop within a couple of minutes of every market close.
   latest_snapshot() SHOULD still raise on staleness (used right before
   trading) - peek_snapshot() must not, since it's used only to check
   tradeability.

2. IG deprecated the MARKET:<epic> item type in May 2026 (confirmed by
   reproducing error 21 "Invalid group" directly against Lightstreamer,
   bypassing this app's code entirely). This streamer now uses
   CHART:<epic>:TICK instead, whose field set has no MARKET_STATE
   equivalent - status comes from a periodic REST poll instead.

3. Separately, IG can tear the whole Lightstreamer subscription down (not
   just go quiet on an item) - observed as onUnsubscription +
   onSubscriptionError with no automatic client-side reconnect, which left
   a process silently dead in the water for 13 days even though the market
   reopened and closed several times in between. subscription_is_broken()/
   resubscribe() exist for that.
"""

import time

import pytest

from oil_pair.price_streamer import PriceStreamer, _MarketListener


class FakeIGClient:
    """Serves market status over 'REST', and counts the calls."""

    def __init__(self, statuses=None):
        self.statuses = statuses or {}
        self.calls = 0
        self.fail_with: Exception | None = None

    def fetch_market(self, epic):
        self.calls += 1
        if self.fail_with is not None:
            raise self.fail_with
        return {"snapshot": {"marketStatus": self.statuses.get(epic, "TRADEABLE")}}


class FakeStreamService:
    def __init__(self):
        self.subscribe_calls = 0

    def subscribe(self, subscription):
        self.subscribe_calls += 1


@pytest.fixture
def ig_client():
    return FakeIGClient({"EPIC.A": "TRADEABLE", "EPIC.B": "TRADEABLE"})


@pytest.fixture
def streamer(ig_client):
    return PriceStreamer(
        ig_client=ig_client,
        epics=["EPIC.A", "EPIC.B"],
        max_staleness_seconds=0.05,
        market_status_refresh_seconds=600,  # never re-polls mid-test
    )


def prime(streamer, status="TRADEABLE"):
    """Seed the status cache as start() does, without a real stream."""
    for epic in streamer._epics:
        streamer._market_status[epic] = status
    streamer._market_status_checked_at = time.monotonic()


def test_peek_snapshot_returns_empty_dict_before_any_update(streamer):
    assert streamer.peek_snapshot() == {}


def test_peek_snapshot_never_raises_even_when_stale(streamer):
    prime(streamer, "CLOSED")
    streamer._handle_update("EPIC.A", 100.0, 101.0)
    streamer._handle_update("EPIC.B", 50.0, 51.0)
    time.sleep(0.1)  # well past max_staleness_seconds=0.05

    snapshot = streamer.peek_snapshot()  # must not raise

    assert snapshot["EPIC.A"].market_status == "CLOSED"
    assert snapshot["EPIC.B"].market_status == "CLOSED"


def test_latest_snapshot_still_raises_on_missing_epic(streamer):
    prime(streamer)
    streamer._handle_update("EPIC.A", 100.0, 101.0)
    # EPIC.B never received anything

    with pytest.raises(RuntimeError):
        streamer.latest_snapshot()


def test_latest_snapshot_still_raises_on_staleness(streamer):
    prime(streamer)
    streamer._handle_update("EPIC.A", 100.0, 101.0)
    streamer._handle_update("EPIC.B", 50.0, 51.0)
    time.sleep(0.1)  # past max_staleness_seconds=0.05

    with pytest.raises(RuntimeError):
        streamer.latest_snapshot()


def test_latest_snapshot_succeeds_with_fresh_data(streamer):
    prime(streamer)
    streamer._handle_update("EPIC.A", 100.0, 101.0)
    streamer._handle_update("EPIC.B", 50.0, 51.0)

    snapshot = streamer.latest_snapshot()

    assert snapshot["EPIC.A"].mid == pytest.approx(100.5)
    assert snapshot["EPIC.B"].mid == pytest.approx(50.5)


# --- market status now comes from REST ---------------------------------


def test_a_closure_is_seen_even_though_no_tick_follows_it(ig_client):
    """The case the whole REST poll exists for: at a market close the ticks
    simply stop, so the status change can only arrive out of band."""
    streamer = PriceStreamer(ig_client, ["EPIC.A"], market_status_refresh_seconds=0)
    streamer._refresh_market_status_if_due(force=True)
    streamer._handle_update("EPIC.A", 100.0, 101.0)
    assert streamer.peek_snapshot()["EPIC.A"].market_status == "TRADEABLE"

    ig_client.statuses["EPIC.A"] = "CLOSED"  # market closes; no further ticks

    assert streamer.peek_snapshot()["EPIC.A"].market_status == "CLOSED"


def test_status_is_not_re_polled_inside_the_refresh_interval(ig_client):
    streamer = PriceStreamer(ig_client, ["EPIC.A"], market_status_refresh_seconds=600)
    streamer._refresh_market_status_if_due(force=True)
    calls_after_seed = ig_client.calls

    for _ in range(5):
        streamer.peek_snapshot()

    assert ig_client.calls == calls_after_seed


def test_a_failed_status_refresh_keeps_the_last_known_status(ig_client):
    """A blip in one REST call must not read as 'market closed'."""
    streamer = PriceStreamer(ig_client, ["EPIC.A"], market_status_refresh_seconds=0)
    streamer._refresh_market_status_if_due(force=True)
    streamer._handle_update("EPIC.A", 100.0, 101.0)

    ig_client.fail_with = RuntimeError("IG unreachable")

    assert streamer.peek_snapshot()["EPIC.A"].market_status == "TRADEABLE"


def test_a_failed_status_refresh_does_not_raise_from_peek(ig_client):
    streamer = PriceStreamer(ig_client, ["EPIC.A"], market_status_refresh_seconds=0)
    ig_client.fail_with = RuntimeError("IG unreachable")

    assert streamer.peek_snapshot() == {}  # must not raise


def test_status_is_unknown_until_the_first_poll(ig_client):
    streamer = PriceStreamer(ig_client, ["EPIC.A"], market_status_refresh_seconds=600)
    ig_client.fail_with = RuntimeError("IG unreachable")
    streamer._handle_update("EPIC.A", 100.0, 101.0)

    assert streamer.peek_snapshot()["EPIC.A"].market_status == "UNKNOWN"


# --- CHART:TICK specifics ------------------------------------------------


def test_a_one_sided_tick_keeps_the_other_side(streamer):
    """A CHART tick can move only one quote; dropping it would stall the feed
    and eventually look like staleness."""
    prime(streamer)
    streamer._handle_update("EPIC.A", 100.0, 101.0)

    streamer._handle_update("EPIC.A", 100.5, None)

    snapshot = streamer.peek_snapshot()["EPIC.A"]
    assert (snapshot.bid, snapshot.offer) == (100.5, 101.0)


def test_a_tick_with_neither_side_before_any_price_is_ignored(streamer):
    prime(streamer)
    streamer._handle_update("EPIC.A", None, None)
    assert streamer.peek_snapshot() == {}


def test_the_epic_is_parsed_out_of_a_chart_item_name():
    """Item name is CHART:<epic>:TICK, and the epic itself contains dots,
    and rsplit must take the suffix."""
    captured = {}

    class Streamer:
        def _handle_update(self, epic, bid, offer):
            captured.update(epic=epic, bid=bid, offer=offer)

    class Update:
        def getItemName(self):
            return "CHART:CC.D.LCO.USS.IP:TICK"

        def getValue(self, field):
            return {"BID": "9728.2", "OFR": "9731.0"}.get(field)

    _MarketListener(Streamer()).onItemUpdate(Update())

    assert captured == {"epic": "CC.D.LCO.USS.IP", "bid": 9728.2, "offer": 9731.0}


# --- subscription torn down entirely (not just a quiet item) -------------


def test_subscription_starts_not_broken(streamer):
    assert streamer.subscription_is_broken() is False


def test_subscription_error_marks_broken(streamer):
    # Real incident: IG tore down the whole subscription and never
    # recovered on its own - the process sat alive but silently dead for
    # 13 days, through several market opens and closes, since nothing ever
    # noticed the need to resubscribe. main.py's loop polls
    # subscription_is_broken() to notice this and resubscribe.
    listener = _MarketListener(streamer)

    listener.onSubscriptionError(21, "Invalid group")

    assert streamer.subscription_is_broken() is True


def test_unsubscription_marks_broken(streamer):
    listener = _MarketListener(streamer)

    listener.onUnsubscription()

    assert streamer.subscription_is_broken() is True


def test_repeated_subscription_errors_during_same_outage_are_throttled_to_debug(streamer, caplog):
    # Real incident: over a ~50h weekend closure, every ~15s resubscribe
    # attempt fails the same way and re-triggers this callback, which used
    # to log every single one at ERROR - looked like an ongoing crash when
    # it was actually the fix's expected, self-healing retry loop. Only the
    # first occurrence of a given outage should be loud.
    listener = _MarketListener(streamer)

    with caplog.at_level("DEBUG", logger="oil_pair.price_streamer"):
        listener.onSubscriptionError(21, "Invalid group")
        listener.onSubscriptionError(21, "Invalid group")
        listener.onSubscriptionError(21, "Invalid group")

    levels = [r.levelname for r in caplog.records]
    assert levels == ["ERROR", "DEBUG", "DEBUG"]


def test_error_logs_at_error_again_after_recovering_and_breaking_again(streamer, caplog):
    # Real incident: resubscribe() alone used to (wrongly) clear the broken
    # flag before the retry even had a chance to fail, so the "already
    # broken?" check below always saw False and logged ERROR on every
    # single retry - the exact opposite of throttling. Only a genuine
    # recovery (a real price arriving) should reset it.
    listener = _MarketListener(streamer)

    with caplog.at_level("DEBUG", logger="oil_pair.price_streamer"):
        listener.onSubscriptionError(21, "Invalid group")  # first outage: ERROR
        streamer._handle_update("EPIC.A", 100.0, 101.0)  # genuine recovery
        listener.onSubscriptionError(21, "Invalid group")  # second outage: ERROR again

    levels = [r.levelname for r in caplog.records]
    assert levels == ["ERROR", "ERROR"]


def test_resubscribe_does_not_clear_broken_flag_by_itself(streamer):
    # Re-issuing the subscribe call is not evidence of recovery - the very
    # next callback while a market stays closed is usually another
    # failure. Only _handle_update receiving a real price proves it.
    streamer._stream_service = FakeStreamService()
    streamer._mark_subscription_broken()

    streamer.resubscribe()

    assert streamer.subscription_is_broken() is True
    assert streamer._stream_service.subscribe_calls == 1


def test_real_price_update_clears_broken_flag(streamer):
    streamer._mark_subscription_broken()

    streamer._handle_update("EPIC.A", 100.0, 101.0)

    assert streamer.subscription_is_broken() is False
