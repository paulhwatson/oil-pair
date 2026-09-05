"""Live trading loop: Brent Crude vs. gasoline pairs strategy on IG demo."""

from __future__ import annotations

import logging
import signal
import sys
import time
from dataclasses import replace
from pathlib import Path

import pandas as pd
from trading_ig.rest import ApiExceededException, IGException, TokenInvalidException

from oil_pair import instance_lock, price_log
from oil_pair.ig_client import IGClient
from oil_pair.logging_setup import configure_logging
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


def reconcile_positions(client: IGClient, pair_config: PairConfig, state_path: Path) -> RunState:
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
        client.close_market_position(deal_id=leg.deal_id, direction=opposite, size=row["size"], epic=instrument.epic)
    else:
        log.info("tracked pair position was already closed while this app was down - resetting to FLAT")

    return RunState(side=PairSide.FLAT, stopped_out=saved.stopped_out)


def _combined_series(cached: pd.Series, buffered: list[tuple[pd.Timestamp, float]]) -> pd.Series:
    if not buffered:
        return cached
    buffered_series = pd.Series([value for _, value in buffered], index=[ts for ts, _ in buffered])
    return pd.concat([cached, buffered_series]).sort_index()


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
    """
    combined_a = _combined_series(cached_a, [(ts, a) for ts, a, _ in buffer])
    combined_b = _combined_series(cached_b, [(ts, b) for ts, _, b in buffer])

    if len(combined_a) < MIN_FIT_OBSERVATIONS or len(combined_b) < MIN_FIT_OBSERVATIONS:
        return None

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
        "initial fit ready (%.1f min of data, %d+%d points): i1=%s i2=%s hedge_ratio=%.6f std_spread=%.6f",
        span_minutes, len(cached_a), len(buffer), model.i1_key, model.i2_key, model.hedge_ratio, model.std_spread,
    )
    return model


def refit_model(buffer: list[tuple[pd.Timestamp, float, float]], pair_config: PairConfig, epic_a: str, epic_b: str) -> Model:
    index = [row[0] for row in buffer]
    series_a = pd.Series([row[1] for row in buffer], index=index)
    series_b = pd.Series([row[2] for row in buffer], index=index)
    strategy = pair_config.strategy
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


def apply_decision(
    client: IGClient,
    decision,
    state: RunState,
    pair_config: PairConfig,
    model: Model,
    rules: dict[str, float],
    mids: dict[str, float],
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
        try:
            result_i2 = client.open_market_position(
                epic_i2, instruments[epic_i2].expiry, direction_i2, size_i2, instruments[epic_i2].currency_code
            )
        except Exception:
            log.critical(
                "second leg (%s) failed to open after first leg (%s) succeeded - closing first leg to avoid "
                "an unhedged position",
                epic_i2, epic_i1,
            )
            opposite = "SELL" if direction_i1 == "BUY" else "BUY"
            client.close_market_position(deal_id=result_i1["dealId"], direction=opposite, size=size_i1, epic=epic_i1)
            raise

        log.info("ENTER %s: %s %s size=%s, %s %s size=%s", decision.side, direction_i1, epic_i1, size_i1, direction_i2, epic_i2, size_i2)
        leg_i1 = LegPosition(deal_id=result_i1["dealId"], direction=direction_i1)
        leg_i2 = LegPosition(deal_id=result_i2["dealId"], direction=direction_i2)
        leg_a = leg_i1 if epic_i1 == pair_config.instrument_a.epic else leg_i2
        leg_b = leg_i2 if epic_i1 == pair_config.instrument_a.epic else leg_i1
        return RunState(side=decision.side, stopped_out=decision.stopped_out, leg_a=leg_a, leg_b=leg_b)

    # EXIT_TAKE_PROFIT or EXIT_STOP_LOSS
    log.info("%s: closing both legs", decision.action)
    for leg, instrument in ((state.leg_a, pair_config.instrument_a), (state.leg_b, pair_config.instrument_b)):
        if leg is None:
            continue
        opposite = "SELL" if leg.direction == "BUY" else "BUY"
        size = compute_leg_size(pair_config.strategy.notional_trade_size, mids[instrument.epic], rules[instrument.epic])
        client.close_market_position(deal_id=leg.deal_id, direction=opposite, size=size, epic=instrument.epic)

    return RunState(side=PairSide.FLAT, stopped_out=decision.stopped_out)


def run(pair_name: str) -> None:
    paths = resolve_paths(pair_name)
    configure_logging(log_dir=paths.log_dir)
    signal.signal(signal.SIGINT, _handle_shutdown_signal)
    signal.signal(signal.SIGTERM, _handle_shutdown_signal)

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
        _run_locked(paths)
    finally:
        instance_lock.release(paths.instance_lock)


def _run_locked(paths: PairPaths) -> None:
    creds = load_credentials()
    pair_config = load_pair_config(paths.pair_config)

    client = IGClient(creds)
    client.login()

    try:
        state = reconcile_positions(client, pair_config, paths.state)
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
    streamer.wait_for_initial_prices()

    # Seeded from the local price_log cache (built by this app's own past
    # runs) plus freshly streamed prices - no historical-data REST call, so
    # this never hits IG's weekly, non-retryable allowance. model stays None
    # until enough data has accumulated; see try_build_initial_model.
    cached_a = price_log.load_mid_series(epic_a, paths.price_log)
    cached_b = price_log.load_mid_series(epic_b, paths.price_log)
    if cached_a.empty and cached_b.empty:
        log.info(
            "no local price cache yet - warming up for %d min before the first fit",
            pair_config.strategy.warmup_minutes,
        )

    model: Model | None = None
    refit_buffer: list[tuple[pd.Timestamp, float, float]] = []
    next_refit_at: pd.Timestamp | None = None
    next_spread_log_at: pd.Timestamp | None = None

    consecutive_errors = 0
    log.info(
        "entering live loop (streamed prices, evaluated every %ds)", pair_config.strategy.poll_interval_seconds
    )

    try:
        while not _shutdown_requested:
            try:
                # Check tradeability from whatever's cached (possibly stale -
                # e.g. over a weekend, nothing has arrived in a while because
                # nothing is trading, not because the stream is broken)
                # before demanding a fresh snapshot. Calling latest_snapshot()
                # unconditionally here used to raise on every iteration once
                # markets closed, tripping MAX_CONSECUTIVE_ERRORS and exiting
                # a couple of minutes into every weekend.
                peek = streamer.peek_snapshot()
                status_a = peek[epic_a].market_status if epic_a in peek else None
                status_b = peek[epic_b].market_status if epic_b in peek else None
                if status_a != "TRADEABLE" or status_b != "TRADEABLE":
                    log.debug("market not tradeable (a=%s b=%s), skipping iteration", status_a, status_b)
                else:
                    snapshot = streamer.latest_snapshot()
                    now = pd.Timestamp.now(tz="UTC")
                    mids = {epic_a: snapshot[epic_a].mid, epic_b: snapshot[epic_b].mid}
                    refit_buffer.append((now, mids[epic_a], mids[epic_b]))
                    price_log.append_tick(epic_a, mids[epic_a], now, paths.price_log)
                    price_log.append_tick(epic_b, mids[epic_b], now, paths.price_log)

                    if model is None:
                        model = try_build_initial_model(
                            cached_a, cached_b, refit_buffer, pair_config.strategy, epic_a, epic_b, now
                        )
                        if model is not None:
                            refit_buffer.clear()
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
                        state = apply_decision(client, decision, state, pair_config, model, rules, mids)
                        save_state(state, paths.state)

                        if now >= next_refit_at and len(refit_buffer) >= 2:
                            model = refit_model(refit_buffer, pair_config, epic_a, epic_b)
                            refit_buffer.clear()
                            next_refit_at = now + pd.offsets.BusinessDay(pair_config.strategy.model_update_interval_business_days)
                            log.info("refit: hedge_ratio=%.6f std_spread=%.6f", model.hedge_ratio, model.std_spread)

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
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} <pair_name>  (e.g. brent_gasoline - matches a directory under config/)")
        raise SystemExit(1)
    run(sys.argv[1])
