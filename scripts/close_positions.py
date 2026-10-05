#!/usr/bin/env python3
"""Closes exactly the position(s) this pair's local state currently tracks
(state/<pair_name>/run_state.json), then resets local state to FLAT.

Deliberately does NOT touch any other open position that might exist on
these epics - see reconcile_positions() in oil_pair/main.py for why: this
account may run other strategies (including other pairs run by this same
app) or hold manually-opened positions on the same instruments, and this
app must never assume ownership of a position it doesn't recognize by
deal_id.

Refuses to run while a live `python -m oil_pair.main <pair_name>` instance
of the SAME pair holds the instance lock, since both would read/write the
same state file concurrently (the exact race that corrupted it once
already - see instance_lock.py). Other pairs' instances are unaffected.

Uses IG's demo account unless --live is passed, like the app itself. The
state file records which account a position was opened on, and the script
refuses to run against the other one: there, every tracked deal would look
already closed and local state would be reset to FLAT with the real
position still open.

Usage: close_positions.py <pair_name> [--live]
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from oil_pair import instance_lock
from oil_pair.ig_client import IGClient, deal_accepted, describe_rejection
from oil_pair.logging_setup import configure_logging
from oil_pair.paths import PairPaths, resolve as resolve_paths
from oil_pair.settings import load_credentials, load_pair_config
from oil_pair.state_store import (
    AccountMismatchError,
    LegPosition,
    RunState,
    StateCorruptedError,
    check_account,
    load_state,
    save_state,
)
from oil_pair.strategy_logic import PairSide

log = logging.getLogger(__name__)


def _close_tracked_positions(paths: PairPaths, live: bool = False) -> None:
    try:
        state = load_state(paths.state)
    except StateCorruptedError as exc:
        print(f"Cannot proceed: {exc}")
        raise SystemExit(1) from exc

    if state is None or state.side is PairSide.FLAT:
        print(f"No tracked open position for {paths.pair_name} - nothing to close.")
        return

    account = "live" if live else "demo"
    try:
        check_account(state, account)
    except AccountMismatchError as exc:
        print(f"Refusing to run: {exc}")
        raise SystemExit(1) from exc
    if state.account is None:
        print(f"Note: this position was saved before the account was recorded, so it can't be checked - "
              f"make sure it really is on the {account.upper()} account.")

    print(f"Using the {account.upper()} IG account.")
    pair_config = load_pair_config(paths.pair_config)
    client = IGClient(load_credentials(live=live))
    client.login()

    open_positions = client.fetch_open_positions()

    closed_any = False
    refused: dict[str, LegPosition] = {}  # "leg_a"/"leg_b" -> leg IG would not close
    for key, epic, leg in (
        ("leg_a", pair_config.instrument_a.epic, state.leg_a),
        ("leg_b", pair_config.instrument_b.epic, state.leg_b),
    ):
        if leg is None:
            continue
        matching = open_positions[open_positions["dealId"] == leg.deal_id] if len(open_positions) else open_positions
        if len(matching) == 0:
            print(f"Tracked leg {epic} (deal_id={leg.deal_id}) is already closed on IG - skipping.")
            continue

        size = matching.iloc[0]["size"]
        opposite = "SELL" if leg.direction == "BUY" else "BUY"
        print(f"Closing {epic} deal_id={leg.deal_id} direction={leg.direction} size={size}...")
        confirm = client.close_market_position(deal_id=leg.deal_id, direction=opposite, size=size, epic=epic)
        # A refused close comes back as a normal response - see DealRejectedError.
        if not deal_accepted(confirm):
            print(f"IG REFUSED to close {epic} deal_id={leg.deal_id}: {describe_rejection(confirm)} - still open.")
            refused[key] = leg
            continue
        closed_any = True

    if refused:
        # Keep tracking what IG would not close, so a re-run (or the app) can still find it.
        save_state(replace(state, leg_a=refused.get("leg_a"), leg_b=refused.get("leg_b")), paths.state)
        print("Not done - the refused leg(s) are still open and still tracked in the local state. Re-run once "
              "the market is open, or close them on IG directly.")
        raise SystemExit(1)

    save_state(RunState(side=PairSide.FLAT, stopped_out=False), paths.state)
    print("Done - local state reset to FLAT." if closed_any else "Nothing needed closing - local state reset to FLAT.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Close the position this pair's local state tracks. Uses the demo account by default."
    )
    parser.add_argument("pair_name", help="matches a directory under config/, e.g. brent_gasoline")
    parser.add_argument(
        "--live", action="store_true",
        help="close on IG's LIVE account (real money, IG_LIVE_* credentials) instead of the default demo account",
    )
    args = parser.parse_args()
    paths = resolve_paths(args.pair_name)

    configure_logging(log_dir=paths.log_dir)

    try:
        instance_lock.acquire(paths.instance_lock)
    except instance_lock.AlreadyRunningError as exc:
        print(f"Refusing to run: {exc} Stop the live app for {paths.pair_name} first.")
        raise SystemExit(1) from exc

    try:
        _close_tracked_positions(paths, live=args.live)
    finally:
        instance_lock.release(paths.instance_lock)


if __name__ == "__main__":
    main()
