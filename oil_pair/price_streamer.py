"""Live prices via IG's Lightstreamer feed, replacing REST polling.

Subscribes to CHART:<epic>:TICK (mode DISTINCT) for each traded epic and keeps
an in-memory, thread-safe cache of the latest Snapshot per epic, updated by
Lightstreamer's own callback thread in the background. main.py's loop reads
from this cache instead of making a REST call every iteration.

Not MARKET:<epic>, which is what this used to subscribe to: as of 2026-09-21
IG rejects that item group outright with Lightstreamer error 21 ("Invalid
group"), for every epic, on both demo account types and both session
versions, while CHART:...:TICK on the same epics streams normally. The one
thing lost in the move is the MARKET_STATE field, which only the MARKET group
carries - market status now comes from a periodic REST snapshot instead (see
_refresh_market_status_if_due). That status is what tells a closed market
apart from a broken stream, so it cannot simply be dropped.

The same REST snapshot carries the current bid/offer, which also keeps a
quiet leg's price fresh: CHART:TICK only pushes changes, so a leg whose
price isn't moving sends nothing, and without that refresh its cached price
would age into staleness errors even though the stream is fine. Staleness
now means neither the stream nor REST has produced a price for a leg -
a real fault.

Separately, IG can also tear the whole Lightstreamer subscription down (not
just go quiet on an item) - observed as onUnsubscription + onSubscriptionError
with no automatic client-side reconnect. subscription_is_broken()/
resubscribe() exist for that: the caller (main.py's loop) polls the former and
calls the latter every iteration until it clears, which it only does once a
real price arrives - re-issuing the subscribe call is not itself evidence of
recovery.

Historical seeding, dealing-rules lookups, and order placement/closing still
go through IGClient's REST calls - only the per-iteration price read is
push-based now.
"""

from __future__ import annotations

import logging
import threading
import time

from lightstreamer.client import Subscription, SubscriptionListener
from trading_ig import IGStreamService

from oil_pair.ig_client import IGClient, Snapshot

log = logging.getLogger(__name__)

# CHART:...:TICK carries no MARKET_STATE; OFR is its name for the offer.
STREAM_FIELDS = ["BID", "OFR"]
DEFAULT_INITIAL_PRICE_TIMEOUT_SECONDS = 15
DEFAULT_MAX_STALENESS_SECONDS = 60
# How often to re-read market status over REST. Status changes at session
# boundaries, not tick by tick, so this is deliberately much slower than the
# price feed - it only has to notice a close within a minute or so.
DEFAULT_MARKET_STATUS_REFRESH_SECONDS = 60
UNKNOWN_STATUS = "UNKNOWN"
# How long every leg may go without a streamed tick, with all markets open,
# before it is logged as a possible dead stream (see _warn_if_stream_silent).
STREAM_SILENCE_WARNING_SECONDS = 15 * 60


class PriceStreamer:
    def __init__(
        self,
        ig_client: IGClient,
        epics: list[str],
        max_staleness_seconds: float = DEFAULT_MAX_STALENESS_SECONDS,
        market_status_refresh_seconds: float = DEFAULT_MARKET_STATUS_REFRESH_SECONDS,
    ):
        self._ig_client = ig_client
        self._epics = list(epics)
        self._max_staleness_seconds = max_staleness_seconds
        self._market_status_refresh_seconds = market_status_refresh_seconds
        self._lock = threading.Lock()
        self._latest: dict[str, Snapshot] = {}
        self._last_updated_at: dict[str, float] = {}
        self._market_status: dict[str, str] = {}
        self._market_status_checked_at: float | None = None
        self._stream_service: IGStreamService | None = None
        self._subscription_broken = threading.Event()
        self._started_at: float | None = None
        self._last_stream_tick_at: float | None = None  # streamed ticks only, never REST
        self._silence_warned = False

    def start(self) -> None:
        self._stream_service = IGStreamService(self._ig_client.service)
        # trading_ig 0.0.22 never sets this itself (IGStreamService.acc_number
        # stays None) - IG's Lightstreamer endpoint expects the account
        # number as the LS username alongside the CST/XST token password.
        self._stream_service.acc_number = self._ig_client.credentials.acc_number
        self._stream_service.create_session(version="2")
        self._stream_service.subscribe(self._build_subscription())
        self._started_at = time.monotonic()
        log.info("price stream subscribed: %s", self._epics)

        # Seed the status cache before the first read, so the loop is not
        # deciding tradeability from UNKNOWN on its first iteration.
        self._refresh_market_status_if_due(force=True)

    def _build_subscription(self) -> Subscription:
        subscription = Subscription(
            mode="DISTINCT",
            items=[f"CHART:{epic}:TICK" for epic in self._epics],
            fields=STREAM_FIELDS,
        )
        subscription.addListener(_MarketListener(self))
        return subscription

    def _mark_subscription_broken(self) -> None:
        self._subscription_broken.set()

    def subscription_is_broken(self) -> bool:
        return self._subscription_broken.is_set()

    def resubscribe(self) -> None:
        """Re-subscribes after IG tore down the previous subscription. Safe
        to call repeatedly: while the market stays closed IG simply won't
        deliver any prices against the new subscription either, but the call
        itself succeeds, so the caller (main.py's loop) can retry this every
        iteration indefinitely without it counting as an error - it
        self-heals once IG accepts the subscription again.

        Deliberately does NOT clear subscription_is_broken() itself -
        re-issuing the subscribe call is not evidence of recovery (the very
        next callback is usually another failure while the market stays
        closed). Only a real price arriving via _handle_update proves it.
        """
        self._stream_service.subscribe(self._build_subscription())

    def _handle_update(self, epic: str, bid: float | None, offer: float | None) -> None:
        """Called from Lightstreamer's own thread.

        A CHART tick can carry only one side (a trade updates LTP/LTV without
        moving both quotes), so a missing side keeps its previous value
        instead of discarding the tick.
        """
        with self._lock:
            previous = self._latest.get(epic)
            bid = bid if bid is not None else (previous.bid if previous else None)
            offer = offer if offer is not None else (previous.offer if previous else None)
            if bid is None or offer is None:
                return
            self._latest[epic] = Snapshot(
                bid=bid,
                offer=offer,
                mid=(bid + offer) / 2,
                market_status=self._market_status.get(epic, UNKNOWN_STATUS),
            )
            self._last_updated_at[epic] = self._last_stream_tick_at = time.monotonic()
            self._subscription_broken.clear()
            resumed = self._silence_warned
            self._silence_warned = False
        if resumed:
            log.info("streamed prices have resumed")

    def _refresh_market_status_if_due(self, force: bool = False) -> None:
        """Re-read each epic's market status - and its current quote - over
        REST, at most every market_status_refresh_seconds.

        Never raises: this runs on the path peek_snapshot() uses, whose whole
        contract is that it does not raise. A failed refresh keeps the last
        known status and is retried on the next due tick - treating a blip in
        one REST call as "market closed" would stop trading for no reason.
        """
        now = time.monotonic()
        if not force and self._market_status_checked_at is not None:
            if now - self._market_status_checked_at < self._market_status_refresh_seconds:
                return

        self._market_status_checked_at = now
        for epic in self._epics:
            requested_at = time.monotonic()
            try:
                snapshot = self._ig_client.fetch_market(epic)["snapshot"]
                status = snapshot["marketStatus"]
            except Exception as exc:  # noqa: BLE001 - see docstring
                log.warning("could not refresh market status for %s: %s", epic, exc)
                continue
            bid, offer = _as_price(snapshot.get("bid")), _as_price(snapshot.get("offer"))

            with self._lock:
                previous = self._market_status.get(epic)
                self._market_status[epic] = status
                cached = self._latest.get(epic)
                if bid is not None and offer is not None and self._last_updated_at.get(epic, -1.0) < requested_at:
                    # The quote IG just returned is the current price, so it is
                    # at least as fresh as anything the stream has sent. This
                    # matters because CHART:TICK only pushes changes: a leg
                    # whose price simply isn't moving goes silent, its cached
                    # price ages past max_staleness, and each iteration used to
                    # raise - ten in a row stopped wti_heating_oil in the thin
                    # hour after the 23:00 reopen (2026-10-06) while the stream
                    # was fine. A tick that lands while this request is in
                    # flight is newer and is kept. Deliberately does not clear
                    # subscription_is_broken(): only a real streamed price does.
                    self._latest[epic] = Snapshot(bid=bid, offer=offer, mid=(bid + offer) / 2, market_status=status)
                    self._last_updated_at[epic] = time.monotonic()
                elif cached is not None and cached.market_status != status:
                    # Republish the cached snapshot so a status change is
                    # visible even when no tick has arrived since - which is
                    # exactly the case at a market close.
                    self._latest[epic] = Snapshot(
                        bid=cached.bid, offer=cached.offer, mid=cached.mid, market_status=status
                    )

            if previous is not None and previous != status:
                log.info("market status changed for %s: %s -> %s", epic, previous, status)

        self._warn_if_stream_silent(now)

    def _warn_if_stream_silent(self, now: float) -> None:
        """Log when no leg has ticked for a long while with every market open.

        With quiet legs refreshed from REST, a stream that died without IG
        saying so would no longer surface as staleness errors - trading would
        carry on, on prices up to a refresh interval old. One leg going quiet
        is normal; every leg silent for this long while all are open is not,
        so it is logged once per episode (and its end, from _handle_update).
        """
        with self._lock:
            all_open = all(self._market_status.get(epic) == "TRADEABLE" for epic in self._epics)
            last_tick = self._last_stream_tick_at if self._last_stream_tick_at is not None else self._started_at
            if last_tick is None or self._silence_warned or not all_open:
                return
            silent_for = now - last_tick
            if silent_for < STREAM_SILENCE_WARNING_SECONDS:
                return
            self._silence_warned = True
        log.warning(
            "no streamed price on any leg for %.0f min while all markets are open - prices are coming only from "
            "IG's REST quote every %.0fs until the stream resumes",
            silent_for / 60, self._market_status_refresh_seconds,
        )

    def wait_for_initial_prices(self, timeout: float = DEFAULT_INITIAL_PRICE_TIMEOUT_SECONDS) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if all(epic in self._latest for epic in self._epics):
                    return
            time.sleep(0.25)
        missing = [epic for epic in self._epics if epic not in self._latest]
        raise TimeoutError(f"no price stream update received within {timeout}s for: {missing}")

    def peek_snapshot(self) -> dict[str, Snapshot]:
        """Returns whatever's cached right now - possibly stale, possibly
        missing an epic that's never sent anything - without raising.
        market_status is refreshed independently via _refresh_market_status_if_due
        (a periodic REST poll), so it stays meaningful even once prices stop
        moving; use this to check tradeability before deciding whether to
        demand a fresh snapshot via latest_snapshot(). Staleness here is
        expected and NOT an error - there is nothing to trade against a
        closed market, not a broken stream.
        """
        self._refresh_market_status_if_due()
        with self._lock:
            return dict(self._latest)

    def latest_snapshot(self) -> dict[str, Snapshot]:
        """Raises RuntimeError if any epic has no price yet, or its last
        update is older than max_staleness_seconds - callers must not
        silently trade on stale/disconnected stream data."""
        self._refresh_market_status_if_due()
        with self._lock:
            missing = [epic for epic in self._epics if epic not in self._latest]
            if missing:
                raise RuntimeError(f"price stream: no price received yet for: {missing}")

            now = time.monotonic()
            stale = [
                epic for epic in self._epics
                if now - self._last_updated_at[epic] > self._max_staleness_seconds
            ]
            if stale:
                raise RuntimeError(
                    f"price stream: no update in over {self._max_staleness_seconds}s for: {stale} "
                    f"(stream may be disconnected)"
                )
            return dict(self._latest)

    def stop(self) -> None:
        if self._stream_service is not None:
            self._stream_service.disconnect()
            log.info("price stream disconnected")


def _as_price(value) -> float | None:
    """A bid/offer from IG's REST snapshot, which is null while a market is shut."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class _MarketListener(SubscriptionListener):
    def __init__(self, streamer: PriceStreamer):
        self._streamer = streamer

    def onItemUpdate(self, update) -> None:
        # Item name is CHART:<epic>:TICK, and an epic itself contains colons.
        epic = update.getItemName().split(":", 1)[1].rsplit(":", 1)[0]
        bid = update.getValue("BID")
        offer = update.getValue("OFR")
        self._streamer._handle_update(
            epic,
            float(bid) if bid is not None else None,
            float(offer) if offer is not None else None,
        )

    def onSubscriptionError(self, code, message) -> None:
        # main.py's loop already logs one clear warning on the transition
        # into "broken" and resubscribes every iteration until this clears -
        # while a market stays closed (e.g. over a weekend) that retry fails
        # the same way every ~15s for as long as the closure lasts, so
        # logging each repeat at ERROR would flood the log with an
        # already-known, already-being-handled condition. Only the first
        # occurrence of a given outage is worth ERROR; the rest are DEBUG.
        if self._streamer.subscription_is_broken():
            log.debug("price stream subscription error (still recovering): code=%s message=%s", code, message)
        else:
            log.error("price stream subscription error: code=%s message=%s", code, message)
        self._streamer._mark_subscription_broken()

    def onUnsubscription(self) -> None:
        if not self._streamer.subscription_is_broken():
            log.warning("price stream unsubscribed")
        self._streamer._mark_subscription_broken()
