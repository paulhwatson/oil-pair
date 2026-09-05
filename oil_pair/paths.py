"""Per-pair-instance file layout.

Running multiple pairs means running multiple separate processes (see
instance_lock.py - a single process/lock is scoped to one pair), so each
pair gets its own config/state/price_log/logs subtree, named after the
pair, so two pairs' processes never share a file.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class PairPaths:
    pair_name: str
    pair_config: Path
    state: Path
    instance_lock: Path
    price_log: Path
    log_dir: Path


def resolve(pair_name: str) -> PairPaths:
    if not pair_name or any(c in pair_name for c in "/\\"):
        raise ValueError(f"invalid pair name: {pair_name!r}")
    return PairPaths(
        pair_name=pair_name,
        pair_config=REPO_ROOT / "config" / pair_name / "pair_config.toml",
        state=REPO_ROOT / "state" / pair_name / "run_state.json",
        instance_lock=REPO_ROOT / "state" / pair_name / "instance.lock",
        price_log=REPO_ROOT / "price_log" / pair_name / "ticks.csv",
        log_dir=REPO_ROOT / "logs" / pair_name,
    )
