"""Local persistence of this app's own pair-position state.

IG's API has no way to tag which strategy/process opened a position, and the
same demo account may run other strategies (or hold manually-opened
positions) on the very same epics this app trades. So ownership of an open
position is tracked here by deal_id, in a local file - never inferred from
"any position that happens to exist on epic_a/epic_b belongs to us".
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from oil_pair.strategy_logic import PairSide

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_STATE_PATH = REPO_ROOT / "state" / "run_state.json"


class StateCorruptedError(Exception):
    """run_state.json exists but doesn't contain a valid RunState. Raised
    instead of letting a raw KeyError/ValueError surface, and deliberately
    NOT treated the same as "no state file" (load_state returning None) -
    silently treating corruption as "we own nothing" could abandon tracking
    of a position that's still genuinely open on IG. The operator must
    check IG's actual open positions and either fix or deliberately delete
    this file before restarting."""


@dataclass(frozen=True)
class LegPosition:
    deal_id: str
    direction: str  # "BUY" or "SELL", as currently held


@dataclass(frozen=True)
class RunState:
    side: PairSide
    stopped_out: bool
    leg_a: LegPosition | None = None
    leg_b: LegPosition | None = None


def save_state(state: RunState, path: Path = DEFAULT_STATE_PATH) -> None:
    payload = {
        "side": state.side.value,
        "stopped_out": state.stopped_out,
        "leg_a": asdict(state.leg_a) if state.leg_a else None,
        "leg_b": asdict(state.leg_b) if state.leg_b else None,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    # Per-process-unique tmp name: two processes both writing state (which
    # should never happen - see instance_lock.py - but did, once, before
    # that guard existed) must not be able to interleave writes to the same
    # tmp file and truncate/corrupt each other's content.
    tmp_path = path.with_suffix(f".{os.getpid()}.tmp")
    tmp_path.write_text(json.dumps(payload, indent=2))
    tmp_path.replace(path)


def load_state(path: Path = DEFAULT_STATE_PATH) -> RunState | None:
    """Returns None if no state has ever been saved (first run) - callers
    must treat that as "we own nothing", not "start FLAT but there might be
    an existing position to adopt". Raises StateCorruptedError (rather than
    a raw KeyError/ValueError) if the file exists but is malformed - that
    is NOT the same situation as "no file", see StateCorruptedError."""
    if not path.exists():
        return None

    try:
        data = json.loads(path.read_text())
        return RunState(
            side=PairSide(data["side"]),
            stopped_out=data["stopped_out"],
            leg_a=LegPosition(**data["leg_a"]) if data.get("leg_a") else None,
            leg_b=LegPosition(**data["leg_b"]) if data.get("leg_b") else None,
        )
    except (json.JSONDecodeError, KeyError, ValueError, TypeError) as exc:
        raise StateCorruptedError(
            f"{path} exists but could not be parsed as a valid RunState ({exc!r}). Check IG's actual open "
            f"positions before deciding how to proceed - do not assume this means no position is open."
        ) from exc
