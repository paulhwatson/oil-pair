"""Live prices via IG's Lightstreamer feed, replacing REST polling.

Subscribes to MARKET:<epic> (mode MERGE) for each traded epic and keeps an
in-memory, thread-safe cache of the latest Snapshot per epic, updated by
Lightstreamer's own callback thread in the background. main.py's loop reads
from this cache instead of making a REST call every iteration.

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

STREAM_FIELDS = ["BID", "OFFER", "MARKET_STATE"]
DEFAULT_INITIAL_PRICE_TIMEOUT_SECONDS = 15
DEFAULT_MAX_STALENESS_SECONDS = 60


class PriceStreamer:
    def __init__(
        self,
        ig_client: IGClient,
        epics: list[str],
        max_staleness_seconds: float = DEFAULT_MAX_STALENESS_SECONDS,
    ):
        self._ig_client = ig_client
        self._epics = list(epics)
        self._max_staleness_seconds = max_staleness_seconds
        self._lock = threading.Lock()
        self._latest: dict[str, Snapshot] = {}
        self._last_updated_at: dict[str, float] = {}
        self._stream_service: IGStreamService | None = None

    def start(self) -> None:
        self._stream_service = IGStreamService(self._ig_client.service)
        # trading_ig 0.0.22 never sets this itself (IGStreamService.acc_number
        # stays None) - IG's Lightstreamer endpoint expects the account
        # number as the LS username alongside the CST/XST token password.
        self._stream_service.acc_number = self._ig_client.credentials.acc_number
        self._stream_service.create_session(version="2")

        subscription = Subscription(
            mode="MERGE",
            items=[f"MARKET:{epic}" for epic in self._epics],
            fields=STREAM_FIELDS,
        )
        subscription.addListener(_MarketListener(self))
        self._stream_service.subscribe(subscription)
        log.info("price stream subscribed: %s", self._epics)

    def _handle_update(self, epic: str, bid: float | None, offer: float | None, market_status: str | None) -> None:
        if bid is None or offer is None:
            return
        with self._lock:
            self._latest[epic] = Snapshot(
                bid=bid, offer=offer, mid=(bid + offer) / 2, market_status=market_status or "UNKNOWN"
            )
            self._last_updated_at[epic] = time.monotonic()

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
        missing an epic that's never sent anything - without raising. IG
        pushes a MARKET_STATE update when a market closes (e.g. for the
        weekend), so the cached status is still meaningful even once prices
        stop moving; use this to check tradeability before deciding whether
        to demand a fresh snapshot via latest_snapshot(). Staleness here is
        expected and NOT an error - there is nothing to trade against a
        closed market, not a broken stream.
        """
        with self._lock:
            return dict(self._latest)

    def latest_snapshot(self) -> dict[str, Snapshot]:
        """Raises RuntimeError if any epic has no price yet, or its last
        update is older than max_staleness_seconds - callers must not
        silently trade on stale/disconnected stream data."""
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


class _MarketListener(SubscriptionListener):
    def __init__(self, streamer: PriceStreamer):
        self._streamer = streamer

    def onItemUpdate(self, update) -> None:
        epic = update.getItemName().split(":", 1)[1]
        bid = update.getValue("BID")
        offer = update.getValue("OFFER")
        market_status = update.getValue("MARKET_STATE")
        self._streamer._handle_update(
            epic,
            float(bid) if bid is not None else None,
            float(offer) if offer is not None else None,
            market_status,
        )

    def onSubscriptionError(self, code, message) -> None:
        log.error("price stream subscription error: code=%s message=%s", code, message)

    def onUnsubscription(self) -> None:
        log.warning("price stream unsubscribed")
