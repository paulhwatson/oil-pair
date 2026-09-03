#!/usr/bin/env python3
"""Closes exactly the position(s) this app's local state currently tracks
(state/run_state.json), then resets local state to FLAT.

Deliberately does NOT touch any other open position that might exist on
these epics - see reconcile_positions() in oil_pair/main.py for why: this
account may run other strategies or hold manually-opened positions on the
same instruments, and this app must never assume ownership of a position it
doesn't recognize by deal_id.

Refuses to run while a live `python -m oil_pair.main` instance holds the
instance lock, since both would read/write the same state file concurrently
(the exact race that corrupted it once already - see instance_lock.py).
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from oil_pair import instance_lock
from oil_pair.ig_client import IGClient
from oil_pair.logging_setup import configure_logging
from oil_pair.settings import load_credentials, load_pair_config
from oil_pair.state_store import RunState, StateCorruptedError, load_state, save_state
from oil_pair.strategy_logic import PairSide

log = logging.getLogger(__name__)


def _close_tracked_positions() -> None:
    try:
        state = load_state()
    except StateCorruptedError as exc:
        print(f"Cannot proceed: {exc}")
        raise SystemExit(1) from exc

    if state is None or state.side is PairSide.FLAT:
        print("No tracked open position - nothing to close.")
        return

    pair_config = load_pair_config()
    client = IGClient(load_credentials())
    client.login()

    open_positions = client.fetch_open_positions()

    closed_any = False
    for epic, leg in (
        (pair_config.instrument_a.epic, state.leg_a),
        (pair_config.instrument_b.epic, state.leg_b),
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
        client.close_market_position(deal_id=leg.deal_id, direction=opposite, size=size, epic=epic)
        closed_any = True

    save_state(RunState(side=PairSide.FLAT, stopped_out=False))
    print("Done - local state reset to FLAT." if closed_any else "Nothing needed closing - local state reset to FLAT.")


def main() -> None:
    configure_logging()

    try:
        instance_lock.acquire()
    except instance_lock.AlreadyRunningError as exc:
        print(f"Refusing to run: {exc} Stop the live app first.")
        raise SystemExit(1) from exc

    try:
        _close_tracked_positions()
    finally:
        instance_lock.release()


if __name__ == "__main__":
    main()
