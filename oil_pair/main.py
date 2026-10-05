"""Live trading loop: Brent Crude vs. gasoline pairs strategy on IG. Trades
IG's demo API by default; pass --live to trade the real account instead
(see load_credentials)."""

from __future__ import annotations

import argparse
import logging
import signal
import time
from dataclasses import replace
from pathlib import Path

import pandas as pd
from trading_ig.rest import ApiExceededException, IGException, TokenInvalidException

from oil_pair import instance_lock, price_log, trade_report
from oil_pair.ig_client import IGClient, deal_accepted, describe_rejection, require_accepted
from oil_pair.logging_setup import configure_logging
from oil_pair.notify import Notifier, load_email_config
from oil_pair.paths import PairPaths, resolve as resolve_paths
from oil_pair.price_streamer import PriceStreamer
from oil_pair.settings import InstrumentConfig, PairConfig, StrategyConfig, load_credentials, load_pair_config
from oil_pair.state_store import LegPosition, RunState, StateCorruptedError, load_state, save_state
from oil_pair.strategy_logic import (
    MIN_FIT_OBSERVATIONS,
    Action,
    Model,
    PairSide,
    compute_leg_size,
    compute_spread,
    fit_model,
    next_decision,
)

log = logging.getLogger(__name__)

MAX_CONSECUTIVE_ERRORS = 10
SPREAD_LOG_INTERVAL_SECONDS = 60

_shutdown_requested = False


def _handle_shutdown_signal(signum, frame):
    global _shutdown_requested
    log.info("shutdown signal received (%s), finishing current iteration then exiting", signum)
    _shutdown_requested = True


def reconcile_positions(
    client: IGClient, pair_config: PairConfig, state_path: Path, notifier: Notifier | None = None
) -> RunState:
    """Resumes state from the local state file, keyed by deal_id - NOT by
    scanning IG for "any position on epic_a/epic_b", since other strategies
    or manual trades may hold positions on the same instruments. A missing
    state file means "we have no record of owning anything", not "adopt
    whatever's open".
    """
    saved = load_state(state_path)
    if saved is None:
        log.info("no saved state found - starting FLAT (existing positions on these epics, if any, are not "
                  "assumed to belong to this app)")
        return RunState(side=PairSide.FLAT, stopped_out=False)

    if saved.side is PairSide.FLAT:
        return saved

    open_positions = client.fetch_open_positions()
    open_deal_ids = set(open_positions["dealId"]) if len(open_positions) else set()

    a_open = saved.leg_a is not None and saved.leg_a.deal_id in open_deal_ids
    b_open = saved.leg_b is not None and saved.leg_b.deal_id in open_deal_ids

    if a_open and b_open:
        log.info("resuming tracked pair position: side=%s", saved.side)
        return saved

    if a_open or b_open:
        leg = saved.leg_a if a_open else saved.leg_b
        instrument = pair_config.instrument_a if a_open else pair_config.instrument_b
        row = open_positions[open_positions["dealId"] == leg.deal_id].iloc[0]
        log.critical(
            "tracked leg %s (deal_id=%s) is still open but its pair leg is gone (closed by IG's own risk "
            "controls, expired, or closed manually while this app was down) - closing the remaining leg to "
            "avoid unhedged exposure",
            instrument.epic, leg.deal_id,
        )
        opposite = "SELL" if leg.direction == "BUY" else "BUY"
        confirm = client.close_market_position(deal_id=leg.deal_id, direction=opposite, size=row["size"], epic=instrument.epic)
        # Raising stops the app at startup with the leg still open, which is
        # the point: carrying on as FLAT would leave it open and untracked.
        require_accepted(confirm, f"close of orphaned {instrument.epic} leg (deal_id={leg.deal_id})")
        if notifier is not None:
            notifier.send(
                f"Closed {saved.side.value} pair on startup",
                f"While the app was down, one leg of the {saved.side.value} pair was closed outside the app.\n"
                f"Closed the remaining {instrument.name} leg ({instrument.epic}, deal {leg.deal_id}) to avoid "
                f"unhedged exposure. Now FLAT.",
            )
    else:
        log.info("tracked pair position was already closed while this app was down - resetting to FLAT")

    return RunState(side=PairSide.FLAT, stopped_out=saved.stopped_out)


def _combined_series(cached: pd.Series, buffered: list[tuple[pd.Timestamp, float]]) -> pd.Series:
    if not buffered:
        return cached
    buffered_series = pd.Series([value for _, value in buffered], index=[ts for ts, _ in buffered])
    return pd.concat([cached, buffered_series]).sort_index()


def _fit_window(series: pd.Series, now: pd.Timestamp, lookback_days: int) -> pd.Series:
    """The most recent `lookback_days` of a series.

    price_log/ticks.csv grows without bound across runs, so an unbounded fit
    would eventually weight a regime from months ago as heavily as this week's.
    Bounding the window here is what lets the cache keep everything while the
    model only ever sees recent history.
    """
    if series.empty or lookback_days <= 0:
        return series
    return series[series.index >= now - pd.Timedelta(days=lookback_days)]


def _time_even(series: pd.Series) -> pd.Series:
    """One price per hour - the last in each - so a fit weighs every hour of
    its window equally. The price log mixes densities: the live 15s feed adds
    ~240 prices an hour, hourly backfill adds one, and an outage adds none.
    Fitting on raw rows let whichever few days were streamed live decide the
    whole 30-day fit, putting std_spread (and so every threshold) off by ~2x.
    """
    if series.empty:
        return series
    series = series.copy()
    # A cache-plus-buffer concat can leave an object index of Timestamps.
    series.index = pd.DatetimeIndex(series.index)
    return series.resample("1h").last().dropna()


def try_build_initial_model(
    cached_a: pd.Series,
    cached_b: pd.Series,
    buffer: list[tuple[pd.Timestamp, float, float]],
    strategy: StrategyConfig,
    epic_a: str,
    epic_b: str,
    now: pd.Timestamp,
) -> Model | None:
    """Builds the initial hedge-ratio fit from locally-cached + freshly
    streamed prices instead of IG's historical-data REST endpoint (see
    oil_pair/price_log.py). Returns None while there isn't yet enough data
    (both a minimum point count and a minimum time span) - the caller keeps
    accumulating and retrying each iteration until this returns a Model.

    The two limits work in opposite directions: warmup_minutes is the minimum
    span needed before trading at all, max_fit_lookback_days the maximum span
    fitted on. A cold start waits for the former; a restart against a cache
    that already holds months of ticks fits immediately, on the latter.
    """
    combined_a = _time_even(_fit_window(
        _combined_series(cached_a, [(ts, a) for ts, a, _ in buffer]), now, strategy.max_fit_lookback_days
    ))
    combined_b = _time_even(_fit_window(
        _combined_series(cached_b, [(ts, b) for ts, _, b in buffer]), now, strategy.max_fit_lookback_days
    ))

    if len(combined_a) < MIN_FIT_OBSERVATIONS or len(combined_b) < MIN_FIT_OBSERVATIONS:
        return None

    # Measured on the windowed data, since that is what actually gets fitted -
    # a cache reaching further back than the window does not shorten the wait,
    # but it must not be allowed to fake it either.
    earliest = min(combined_a.index.min(), combined_b.index.min())
    span_minutes = (now - earliest).total_seconds() / 60
    if span_minutes < strategy.warmup_minutes:
        return None

    model = fit_model(
        {epic_a: combined_a, epic_b: combined_b},
        entry_stds=strategy.entry_stds,
        limit_stds=strategy.limit_stds,
        stop_stds=strategy.stop_stds,
    )
    log.info(
        "initial fit ready (%.1f h of data within a %d-day window, %d+%d hourly points): "
        "i1=%s i2=%s hedge_ratio=%.6f std_spread=%.6f",
        span_minutes / 60, strategy.max_fit_lookback_days, len(combined_a), len(combined_b),
        model.i1_key, model.i2_key, model.hedge_ratio, model.std_spread,
    )
    return model


def refit_model(
    pair_config: PairConfig,
    epic_a: str,
    epic_b: str,
    price_log_path: Path,
    now: pd.Timestamp,
) -> Model | None:
    """Refits on up to max_fit_lookback_days of cached prices.

    Reads price_log/ticks.csv rather than an in-memory buffer of ticks since
    the last fit: that buffer only ever holds one refit interval, which is
    less than the lookback this is supposed to use. Every tick is appended to
    the log as it arrives, so the log is a superset of any such buffer and
    concatenating both would double-count everything since the last fit.

    Returns None when the window holds too few aligned observations to fit -
    the caller keeps the model it already has rather than dropping to none
    mid-position.
    """
    strategy = pair_config.strategy
    series_a = _time_even(_fit_window(
        price_log.load_mid_series(epic_a, price_log_path), now, strategy.max_fit_lookback_days
    ))
    series_b = _time_even(_fit_window(
        price_log.load_mid_series(epic_b, price_log_path), now, strategy.max_fit_lookback_days
    ))

    if len(series_a) < MIN_FIT_OBSERVATIONS or len(series_b) < MIN_FIT_OBSERVATIONS:
        log.warning(
            "refit skipped: only %d/%d hourly points in the last %d day(s), need %d each",
            len(series_a), len(series_b), strategy.max_fit_lookback_days, MIN_FIT_OBSERVATIONS,
        )
        return None

    return fit_model(
        {epic_a: series_a, epic_b: series_b},
        entry_stds=strategy.entry_stds,
        limit_stds=strategy.limit_stds,
        stop_stds=strategy.stop_stds,
    )


def direction_for_leg(side: PairSide, is_i1_leg: bool) -> str:
    # SHORT the pair = SELL i1, BUY i2. LONG the pair = BUY i1, SELL i2.
    if side is PairSide.SHORT:
        return "SELL" if is_i1_leg else "BUY"
    return "BUY" if is_i1_leg else "SELL"


def _notify(notifier: Notifier | None, build) -> None:
    """Send a trade email built by `build()`, which returns (subject, body).

    Called after IG has already dealt, before the new state is returned to
    be saved - so a formatting bug raising here would lose track of real
    positions. It is logged and swallowed instead: a missing email is a
    nuisance, an untracked position is not.
    """
    if notifier is None:
        return
    try:
        subject, body = build()
    except Exception:
        log.exception("could not build the trade email - the trade itself is unaffected")
        return
    notifier.send(subject, body)


def _ig_position_size(client: IGClient, deal_id: str) -> float | None:
    """The size IG holds for a deal, or None if it can't be read or isn't listed."""
    try:
        positions = client.fetch_open_positions()
    except Exception as exc:
        log.warning("could not read open positions from IG to size a close (%s)", exc)
        return None
    if len(positions) == 0 or "dealId" not in positions or "size" not in positions:
        return None
    rows = positions[positions["dealId"] == deal_id]
    if rows.empty:
        return None
    try:
        return float(rows.iloc[0]["size"])
    except (TypeError, ValueError):
        return None


def _close_size(
    client: IGClient, leg: LegPosition, pair_config: PairConfig, instrument: InstrumentConfig,
    mids: dict[str, float], rules: dict[str, float],
) -> float:
    """How much to close: whatever IG holds for this deal.

    This used to be worked out afresh from the current price, which drifts
    off the size that was opened once the price crosses a rounding boundary:
    Robusta opened at £0.85/pt near 3,514 becomes £0.86/pt below ~3,509. A
    bigger close is refused and a smaller one leaves a sliver open, and
    either way nothing tracks what's left.

    If IG can't be asked, the size recorded at entry is used. The size
    worked out from the current price is only a last resort, for a leg
    opened before sizes were recorded. The close is attempted either way:
    a deal missing from IG's list is left to the failed-close cross-check
    to decide, so a hiccup in that list can't skip a real close.
    """
    held = _ig_position_size(client, leg.deal_id)
    if held is not None:
        return held
    if leg.size is not None:
        return leg.size
    return compute_leg_size(pair_config.strategy.notional_trade_size, mids[instrument.epic], rules[instrument.epic])


def _confirm_float(confirm: dict | None, key: str) -> float | None:
    """A number from IG's deal confirmation, or None if it isn't there.
    Only used for the emails, so a missing or odd field must never raise."""
    if not isinstance(confirm, dict) or confirm.get(key) is None:
        return None
    try:
        return float(confirm[key])
    except (TypeError, ValueError):
        return None


def _names(pair_config: PairConfig) -> dict[str, str]:
    return {
        pair_config.instrument_a.epic: pair_config.instrument_a.name,
        pair_config.instrument_b.epic: pair_config.instrument_b.name,
    }


def _leg_fill(leg: LegPosition, instrument: InstrumentConfig) -> trade_report.LegFill:
    return trade_report.LegFill(
        name=instrument.name, epic=instrument.epic, direction=leg.direction,
        size=leg.size, open_level=leg.open_level, deal_id=leg.deal_id,
    )


def _closed_leg_fill(
    leg: LegPosition | None, instrument: InstrumentConfig, close_confirms: dict[str, dict | None]
) -> trade_report.LegFill:
    if leg is None:  # closed on an earlier iteration - its details went with it
        return trade_report.LegFill(name=instrument.name, epic=instrument.epic, direction="", closed_earlier=True)
    confirm = close_confirms.get(instrument.epic)
    close_level = _confirm_float(confirm, "level")
    profit = _confirm_float(confirm, "profit")
    from_ig = profit is not None
    if profit is None:
        profit = trade_report.leg_profit(leg.direction, leg.size, leg.open_level, close_level)
    return replace(
        _leg_fill(leg, instrument), close_level=close_level, profit=profit, profit_from_ig=from_ig
    )


def apply_decision(
    client: IGClient,
    decision,
    state: RunState,
    pair_config: PairConfig,
    model: Model,
    rules: dict[str, float],
    mids: dict[str, float],
    notifier: Notifier | None = None,
) -> RunState:
    instruments: dict[str, InstrumentConfig] = {
        pair_config.instrument_a.epic: pair_config.instrument_a,
        pair_config.instrument_b.epic: pair_config.instrument_b,
    }
    epic_i1, epic_i2 = model.i1_key, model.i2_key

    if decision.action is Action.NONE:
        return replace(state, side=decision.side, stopped_out=decision.stopped_out)

    if decision.action is Action.ENTER:
        size_i1 = compute_leg_size(pair_config.strategy.notional_trade_size, mids[epic_i1], rules[epic_i1])
        size_i2 = compute_leg_size(pair_config.strategy.notional_trade_size, mids[epic_i2], rules[epic_i2])

        direction_i1 = direction_for_leg(decision.side, is_i1_leg=True)
        direction_i2 = direction_for_leg(decision.side, is_i1_leg=False)

        result_i1 = client.open_market_position(
            epic_i1, instruments[epic_i1].expiry, direction_i1, size_i1, instruments[epic_i1].currency_code
        )
        require_accepted(result_i1, f"open of {epic_i1}")
        try:
            result_i2 = client.open_market_position(
                epic_i2, instruments[epic_i2].expiry, direction_i2, size_i2, instruments[epic_i2].currency_code
            )
            require_accepted(result_i2, f"open of {epic_i2}")
        except Exception:
            log.critical(
                "second leg (%s) failed to open after first leg (%s) succeeded - closing first leg to avoid "
                "an unhedged position",
                epic_i2, epic_i1,
            )
            opposite = "SELL" if direction_i1 == "BUY" else "BUY"
            unwind = client.close_market_position(deal_id=result_i1["dealId"], direction=opposite, size=size_i1, epic=epic_i1)
            if not deal_accepted(unwind):
                log.critical(
                    "IG rejected closing the first leg (%s, deal_id=%s, %s) - it is OPEN AND UNTRACKED; close it by hand",
                    epic_i1, result_i1["dealId"], describe_rejection(unwind),
                )
            raise

        log.info("ENTER %s: %s %s size=%s, %s %s size=%s", decision.side, direction_i1, epic_i1, size_i1, direction_i2, epic_i2, size_i2)
        now = pd.Timestamp.now(tz="UTC")
        leg_i1 = LegPosition(
            deal_id=result_i1["dealId"], direction=direction_i1, size=size_i1, open_level=_confirm_float(result_i1, "level")
        )
        leg_i2 = LegPosition(
            deal_id=result_i2["dealId"], direction=direction_i2, size=size_i2, open_level=_confirm_float(result_i2, "level")
        )
        leg_a = leg_i1 if epic_i1 == pair_config.instrument_a.epic else leg_i2
        leg_b = leg_i2 if epic_i1 == pair_config.instrument_a.epic else leg_i1
        new_state = RunState(
            side=decision.side, stopped_out=decision.stopped_out, leg_a=leg_a, leg_b=leg_b,
            opened_at=now,
            entry_spread=compute_spread(mids[epic_i1], mids[epic_i2], model.hedge_ratio),
            entry_std_spread=model.std_spread,
        )
        _notify(notifier, lambda: trade_report.opened(
            decision.side, decision.reason,
            [_leg_fill(leg, instrument) for leg, instrument in
             ((leg_a, pair_config.instrument_a), (leg_b, pair_config.instrument_b))],
            model, mids, _names(pair_config), now,
        ))
        return new_state

    # EXIT_TAKE_PROFIT or EXIT_STOP_LOSS
    log.info("%s: closing both legs", decision.action)
    close_confirms: dict[str, dict | None] = {}  # epic -> IG's deal confirmation, for the email

    def _close_leg(leg: LegPosition | None, instrument: InstrumentConfig) -> LegPosition | None:
        """Returns None once the leg is confirmed closed (or if it was
        already None), or the same leg unchanged if closing it failed - so
        the caller keeps retrying just that leg on future iterations rather
        than losing track of it. One leg failing must never prevent
        attempting the other (this used to be one un-isolated loop, so an
        IG-side error closing the first leg left the second leg never even
        attempted, with local state stuck reporting both as still open)."""
        if leg is None:
            return None
        opposite = "SELL" if leg.direction == "BUY" else "BUY"
        size = _close_size(client, leg, pair_config, instrument, mids, rules)
        try:
            confirm = client.close_market_position(deal_id=leg.deal_id, direction=opposite, size=size, epic=instrument.epic)
            # A rejected close still comes back as a normal response, so
            # without this a refused close was recorded as done and the
            # position was left open with nothing tracking it.
            require_accepted(confirm, f"close of {instrument.epic} leg (deal_id={leg.deal_id})")
            close_confirms[instrument.epic] = confirm
            return None
        except Exception as exc:
            # IG can fail to close a deal_id that's already gone from the
            # account (observed: closing an already-closed DFB position
            # returns an unrelated-looking "notional.details.null" error,
            # not a clean "not found") - retrying that forever spams
            # CRITICAL logs over a leg that was never actually exposed.
            # Cross-check against IG's own open positions before assuming
            # the worst.
            try:
                open_positions = client.fetch_open_positions()
                still_open = len(open_positions) > 0 and leg.deal_id in set(open_positions["dealId"])
            except Exception:
                still_open = True  # can't confirm either way - assume the worst and keep retrying

            if not still_open:
                log.warning(
                    "close failed for %s leg (deal_id=%s): %s - but IG shows no such open position, "
                    "treating it as already closed",
                    instrument.epic, leg.deal_id, exc,
                )
                return None

            log.critical(
                "failed to close %s leg (deal_id=%s): %s - still exposed, will keep retrying on future iterations",
                instrument.epic, leg.deal_id, exc,
            )
            return leg

    remaining_leg_a = _close_leg(state.leg_a, pair_config.instrument_a)
    remaining_leg_b = _close_leg(state.leg_b, pair_config.instrument_b)

    if remaining_leg_a is None and remaining_leg_b is None:
        outcome = "take profit" if decision.action is Action.EXIT_TAKE_PROFIT else "stop loss"
        _notify(notifier, lambda: trade_report.closed(
            state.side, outcome, decision.reason,
            [_closed_leg_fill(leg, instrument, close_confirms)
             for leg, instrument in ((state.leg_a, pair_config.instrument_a), (state.leg_b, pair_config.instrument_b))],
            model, mids, _names(pair_config), pd.Timestamp.now(tz="UTC"),
            opened_at=state.opened_at, entry_spread=state.entry_spread, entry_std_spread=state.entry_std_spread,
        ))
        return RunState(side=PairSide.FLAT, stopped_out=decision.stopped_out)
    return replace(state, leg_a=remaining_leg_a, leg_b=remaining_leg_b, stopped_out=decision.stopped_out)


def run(pair_name: str, live: bool = False) -> None:
    paths = resolve_paths(pair_name)
    configure_logging(log_dir=paths.log_dir)
    signal.signal(signal.SIGINT, _handle_shutdown_signal)
    signal.signal(signal.SIGTERM, _handle_shutdown_signal)

    if live:
        log.warning("LIVE TRADING MODE - orders will be placed against a real IG account, not the demo API")

    try:
        instance_lock.acquire(paths.instance_lock)
    except instance_lock.AlreadyRunningError as exc:
        log.critical(
            "%s Running two instances of the same pair against the same account corrupts shared state "
            "(%s) and can race placing/closing orders.",
            exc, paths.state,
        )
        raise SystemExit(1) from exc

    try:
        _run_locked(paths, live=live)
    finally:
        instance_lock.release(paths.instance_lock)


def _run_locked(paths: PairPaths, live: bool = False) -> None:
    creds = load_credentials(live=live)
    pair_config = load_pair_config(paths.pair_config)

    notifier = Notifier(paths.pair_name, load_email_config())

    client = IGClient(creds)
    client.login()

    try:
        state = reconcile_positions(client, pair_config, paths.state, notifier)
    except StateCorruptedError as exc:
        log.critical("%s", exc)
        raise SystemExit(1) from exc
    save_state(state, paths.state)

    epic_a, epic_b = pair_config.instrument_a.epic, pair_config.instrument_b.epic
    rules = {
        epic_a: client.get_dealing_rules(epic_a),
        epic_b: client.get_dealing_rules(epic_b),
    }

    streamer = PriceStreamer(client, [epic_a, epic_b])
    streamer.start()
    try:
        streamer.wait_for_initial_prices()
    except TimeoutError:
        # Real incident: this used to only tolerate the timeout when
        # subscription_is_broken() was true, re-raising (crashing the
        # process, uncaught) otherwise - on the assumption that a clean
        # subscribe with no price within 15s meant a bad epic. That doesn't
        # hold for CHART:<epic>:TICK (DISTINCT mode): unlike the old
        # MARKET:<epic> (MERGE mode), it pushes nothing at all until the
        # next genuine tick, so a brief quiet moment can outlast the
        # timeout on a perfectly healthy subscription. A typo'd epic is
        # already caught earlier and loudly, via get_dealing_rules()'s REST
        # call above - this one can simply wait; the main loop already
        # handles "no price yet" as "not tradeable, skip" indefinitely.
        log.warning(
            "no initial prices within timeout - entering the main loop anyway; CHART:<epic>:TICK "
            "only pushes on a genuine new tick, so this can happen on a healthy subscription too"
        )

    # Seeded from the local price_log cache (built by this app's own past
    # runs) plus freshly streamed prices - no historical-data REST call, so
    # this never hits IG's weekly, non-retryable allowance. model stays None
    # until enough data has accumulated; see try_build_initial_model.
    cached_a = price_log.load_mid_series(epic_a, paths.price_log)
    cached_b = price_log.load_mid_series(epic_b, paths.price_log)
    strategy = pair_config.strategy
    if cached_a.empty and cached_b.empty:
        log.info(
            "no local price cache yet - warming up for %.1f h before the first fit",
            strategy.warmup_minutes / 60,
        )
    else:
        # Only over whichever series actually has ticks: one epic can be empty
        # while the other isn't (a renamed epic, or a crash between the two
        # append_tick calls), and .index.min() on an empty series is NaT.
        earliest = [s.index.min() for s in (cached_a, cached_b) if not s.empty]
        cached_span_hours = (
            pd.Timestamp.now(tz="UTC") - min(earliest)
        ).total_seconds() / 3600
        log.info(
            "local price cache spans %.1f h (%d+%d ticks); fits use at most %d day(s) of it, "
            "and need %.1f h before the first one",
            cached_span_hours, len(cached_a), len(cached_b),
            strategy.max_fit_lookback_days, strategy.warmup_minutes / 60,
        )

    model: Model | None = None
    # Only holds ticks until the first fit lands. Refits read the price log
    # instead (see refit_model), so nothing accumulates here for the life of
    # the process.
    warmup_buffer: list[tuple[pd.Timestamp, float, float]] = []
    next_refit_at: pd.Timestamp | None = None
    next_spread_log_at: pd.Timestamp | None = None
    subscription_was_broken = False

    consecutive_errors = 0
    log.info(
        "entering live loop (streamed prices, evaluated every %ds)", pair_config.strategy.poll_interval_seconds
    )

    try:
        while not _shutdown_requested:
            try:
                # IG can tear the whole Lightstreamer subscription down (not
                # just go quiet on an item) - observed as onUnsubscription +
                # onSubscriptionError with no automatic client-side
                # reconnect, which once left a process silently dead for 13
                # days through several market opens and closes. Left alone,
                # no further price update ever arrives, so peek_snapshot()'s
                # cached status never changes and latest_snapshot()
                # eventually raises on staleness instead. Resubscribing is
                # safe to retry every iteration: it's a no-op error (not
                # counted against MAX_CONSECUTIVE_ERRORS), and self-heals
                # once IG accepts the subscription again.
                if streamer.subscription_is_broken():
                    if not subscription_was_broken:
                        log.warning(
                            "price stream subscription is broken - will keep attempting to "
                            "resubscribe automatically until it recovers"
                        )
                        subscription_was_broken = True
                    streamer.resubscribe()
                    consecutive_errors = 0
                    time.sleep(pair_config.strategy.poll_interval_seconds)
                    continue
                subscription_was_broken = False

                # Check tradeability from whatever's cached (possibly stale -
                # e.g. over a weekend, nothing has arrived in a while because
                # nothing is trading, not because the stream is broken)
                # before demanding a fresh snapshot. Calling latest_snapshot()
                # unconditionally here used to raise on every iteration once
                # markets closed, tripping MAX_CONSECUTIVE_ERRORS and exiting
                # a couple of minutes into every weekend. market_status comes
                # from a periodic REST poll inside PriceStreamer itself (see
                # price_streamer.py) - CHART:<epic>:TICK carries no status
                # field of its own.
                peek = streamer.peek_snapshot()
                status_a = peek[epic_a].market_status if epic_a in peek else None
                status_b = peek[epic_b].market_status if epic_b in peek else None
                if status_a != "TRADEABLE" or status_b != "TRADEABLE":
                    log.debug("market not tradeable (a=%s b=%s), skipping iteration", status_a, status_b)
                else:
                    snapshot = streamer.latest_snapshot()
                    now = pd.Timestamp.now(tz="UTC")
                    mids = {epic_a: snapshot[epic_a].mid, epic_b: snapshot[epic_b].mid}
                    price_log.append_tick(epic_a, mids[epic_a], now, paths.price_log)
                    price_log.append_tick(epic_b, mids[epic_b], now, paths.price_log)

                    if model is None:
                        warmup_buffer.append((now, mids[epic_a], mids[epic_b]))
                        model = try_build_initial_model(
                            cached_a, cached_b, warmup_buffer, pair_config.strategy, epic_a, epic_b, now
                        )
                        if model is not None:
                            warmup_buffer.clear()
                            next_refit_at = now + pd.offsets.BusinessDay(
                                pair_config.strategy.model_update_interval_business_days
                            )
                    else:
                        assert next_refit_at is not None  # set alongside model, always together
                        spread = compute_spread(mids[model.i1_key], mids[model.i2_key], model.hedge_ratio)

                        if next_spread_log_at is None or now >= next_spread_log_at:
                            log.info(
                                "spread=%.6f (%.2f std) entry=+-%.6f limit=+-%.6f stop=+-%.6f std_spread=%.6f side=%s",
                                spread, spread / model.std_spread if model.std_spread else float("nan"),
                                model.entry_threshold, model.limit_threshold, model.stop_threshold,
                                model.std_spread, state.side,
                            )
                            next_spread_log_at = now + pd.Timedelta(seconds=SPREAD_LOG_INTERVAL_SECONDS)

                        decision = next_decision(spread, model, state.side, state.stopped_out)
                        if decision.action is not Action.NONE:
                            log.info("decision: %s reason=%r spread=%.6f", decision.action, decision.reason, spread)
                        state = apply_decision(client, decision, state, pair_config, model, rules, mids, notifier)
                        save_state(state, paths.state)

                        if now >= next_refit_at:
                            refitted = refit_model(pair_config, epic_a, epic_b, paths.price_log, now)
                            if refitted is not None:
                                model = refitted
                                log.info(
                                    "refit: hedge_ratio=%.6f std_spread=%.6f",
                                    model.hedge_ratio, model.std_spread,
                                )
                            # Reschedule either way: a thin window now is no
                            # reason to retry on every single iteration.
                            next_refit_at = now + pd.offsets.BusinessDay(pair_config.strategy.model_update_interval_business_days)

                consecutive_errors = 0

            except TokenInvalidException:
                log.warning("session token invalid, re-logging in")
                client.login()
            except ApiExceededException:
                log.warning("IG rate limit hit, backing off")
                time.sleep(30)
            except IGException as exc:
                log.error("IG API error this iteration: %s", exc)
                consecutive_errors += 1
            except Exception:
                log.exception("unexpected error this iteration")
                consecutive_errors += 1

            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                log.critical("too many consecutive errors (%d), exiting for operator attention", consecutive_errors)
                raise SystemExit(1)

            time.sleep(pair_config.strategy.poll_interval_seconds)
    finally:
        streamer.stop()

    log.info("shutdown complete, final state: side=%s stopped_out=%s", state.side, state.stopped_out)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Pairs-trading loop for IG Markets. Trades the demo API by default."
    )
    parser.add_argument("pair_name", help="matches a directory under config/, e.g. brent_gasoline")
    parser.add_argument(
        "--live", action="store_true",
        help="trade IG's LIVE account (real money, IG_LIVE_* credentials) instead of the default demo account",
    )
    args = parser.parse_args()
    run(args.pair_name, live=args.live)
