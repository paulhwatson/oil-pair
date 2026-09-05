"""Single-instance guard, scoped per pair (see oil_pair/paths.py).

Two concurrent instances of the SAME pair is a real failure mode, not a
hypothetical one: they'd independently read/write the same
state/<pair_name>/run_state.json with no coordination, corrupting it (this
is exactly what caused a real incident - see state_store.py) and could race
each other placing/closing orders. This lock makes a second concurrent
start of the same pair fail loudly instead of silently doing that. Two
different pairs use different lock files and don't interact.
"""

from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LOCK_PATH = REPO_ROOT / "state" / "instance.lock"


class AlreadyRunningError(Exception):
    """Another instance of this app appears to already be running."""


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # process exists, just owned by someone else
    return True


def acquire(path: Path = DEFAULT_LOCK_PATH) -> None:
    """Raises AlreadyRunningError if a live process already holds the lock.
    A lock file whose pid is no longer running is treated as stale and
    silently reclaimed (e.g. after a crash that skipped release())."""
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.exists():
        try:
            existing_pid = int(path.read_text().strip())
        except ValueError:
            existing_pid = None
        if existing_pid is not None and _pid_is_alive(existing_pid):
            raise AlreadyRunningError(
                f"another instance appears to already be running (pid {existing_pid}, lock file {path}). "
                f"Stop it first, or delete the lock file if you're certain it's stale."
            )

    path.write_text(str(os.getpid()))


def release(path: Path = DEFAULT_LOCK_PATH) -> None:
    try:
        if path.exists() and path.read_text().strip() == str(os.getpid()):
            path.unlink()
    except OSError:
        pass
