"""Resilient wrapper around trading_ig's IGService (REST only - no streaming).

Centralizes retry/re-login/backoff behavior so main.py's loop doesn't need to
know about IG-specific exceptions.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import pandas as pd
import trading_ig.rest as _trading_ig_rest
from pandas.tseries.frequencies import to_offset
from trading_ig import IGService
from trading_ig.rest import ApiExceededException, IGException, TokenInvalidException

from oil_pair.settings import IGCredentials

log = logging.getLogger(__name__)

BACKOFF_INITIAL_SECONDS = 30
BACKOFF_MAX_SECONDS = 300


def _conv_resol_pandas3_safe(resolution):
    """Replacement for trading_ig.utils.conv_resol.

    The installed trading-ig (0.0.22) builds its resolution lookup with
    to_offset("1H")/to_offset("M"), which are pandas < 2.2 aliases. pandas
    3.0 removed them entirely (now "1h"/"ME"), so the original function
    raises ValueError on *every* call regardless of the resolution actually
    requested - including our own "1Min". Patched in below rather than
    forking the library.
    """
    mapping = {
        to_offset("1s"): "SECOND",
        to_offset("1min"): "MINUTE",
        to_offset("2min"): "MINUTE_2",
        to_offset("3min"): "MINUTE_3",
        to_offset("5min"): "MINUTE_5",
        to_offset("10min"): "MINUTE_10",
        to_offset("15min"): "MINUTE_15",
        to_offset("30min"): "MINUTE_30",
        to_offset("1h"): "HOUR",
        to_offset("2h"): "HOUR_2",
        to_offset("3h"): "HOUR_3",
        to_offset("4h"): "HOUR_4",
        to_offset("D"): "DAY",
        to_offset("W"): "WEEK",
        to_offset("ME"): "MONTH",
    }
    offset = to_offset(resolution)
    return mapping.get(offset, resolution)


_trading_ig_rest.conv_resol = _conv_resol_pandas3_safe


@dataclass(frozen=True)
class Snapshot:
    bid: float
    offer: float
    mid: float
    market_status: str


class IGClient:
    def __init__(self, credentials: IGCredentials):
        self._credentials = credentials
        self._service: IGService | None = None

    def login(self) -> None:
        self._service = IGService(
            self._credentials.username,
            self._credentials.password,
            self._credentials.api_key,
            acc_type="demo",
            acc_number=self._credentials.acc_number,
        )
        self._service.create_session(version="2")
        log.info("logged in to IG demo API as %s", self._credentials.username)

    @property
    def service(self) -> IGService:
        if self._service is None:
            raise RuntimeError("IGClient.login() must be called before use")
        return self._service

    @property
    def credentials(self) -> IGCredentials:
        return self._credentials

    def _call(self, fn, *args, max_retries: int = 3, **kwargs):
        backoff = BACKOFF_INITIAL_SECONDS
        for attempt in range(1, max_retries + 1):
            try:
                return fn(*args, **kwargs)
            except TokenInvalidException:
                log.warning("IG session token invalid, re-logging in (attempt %d/%d)", attempt, max_retries)
                self.login()
            except ApiExceededException:
                log.warning("IG API rate limit hit, backing off %ds (attempt %d/%d)", backoff, attempt, max_retries)
                time.sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX_SECONDS)
        raise IGException(f"exceeded {max_retries} retries calling {getattr(fn, '__name__', fn)}")

    def search_markets(self, term: str) -> pd.DataFrame:
        return self._call(self.service.search_markets, term)

    def fetch_market(self, epic: str) -> dict:
        return self._call(self.service.fetch_market_by_epic, epic)

    def fetch_open_positions(self) -> pd.DataFrame:
        return self._call(self.service.fetch_open_positions)

    def get_dealing_rules(self, epic: str) -> float:
        """Returns the market's minDealSize - the floor for position size.
        (dealingRules.minStepDistance is the min stop/limit distance, not a
        size increment, so it's not used for sizing.)
        """
        market = self.fetch_market(epic)
        return float(market["dealingRules"]["minDealSize"]["value"])

    def open_market_position(self, epic: str, expiry: str, direction: str, size: float, currency_code: str) -> dict:
        log.info("opening market position: %s %s size=%s", direction, epic, size)
        return self._call(
            self.service.create_open_position,
            currency_code=currency_code,
            direction=direction,
            epic=epic,
            expiry=expiry,
            force_open=True,
            guaranteed_stop=False,
            level=None,
            limit_distance=None,
            limit_level=None,
            order_type="MARKET",
            quote_id=None,
            size=size,
            stop_distance=None,
            stop_level=None,
            trailing_stop=False,
            trailing_stop_increment=None,
        )

    def close_market_position(self, deal_id: str, direction: str, size: float, epic: str | None = None) -> dict:
        # IG's close-position endpoint identifies the position EITHER by
        # dealId OR by epic+expiry, not both - sending both raises
        # "validation.mutual-exclusive-value.request". We always know the
        # dealId (from local state), so epic/expiry are only used here for
        # the log line, never sent to IG.
        log.info("closing market position: deal_id=%s direction=%s epic=%s size=%s", deal_id, direction, epic, size)
        return self._call(
            self.service.close_open_position,
            deal_id=deal_id,
            direction=direction,
            epic=None,
            expiry=None,
            level=None,
            order_type="MARKET",
            quote_id=None,
            size=size,
        )
