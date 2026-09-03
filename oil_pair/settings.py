"""Credentials (.env) and pair/strategy config (config/pair_config.toml) loading."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PAIR_CONFIG_PATH = REPO_ROOT / "config" / "pair_config.toml"

_REQUIRED_ENV_VARS = (
    "IG_DEMO_USERNAME",
    "IG_DEMO_PASSWORD",
    "IG_DEMO_API_KEY",
    "IG_DEMO_ACC_NUMBER",
)


@dataclass(frozen=True)
class IGCredentials:
    username: str
    password: str
    api_key: str
    acc_number: str
    acc_type: str = "demo"


def load_credentials() -> IGCredentials:
    load_dotenv(REPO_ROOT / ".env")

    missing = [name for name in _REQUIRED_ENV_VARS if not os.environ.get(name)]
    if missing:
        raise RuntimeError(
            f"Missing required .env variables: {', '.join(missing)}. "
            f"Copy .env.example to .env and fill in your IG demo credentials."
        )

    return IGCredentials(
        username=os.environ["IG_DEMO_USERNAME"],
        password=os.environ["IG_DEMO_PASSWORD"],
        api_key=os.environ["IG_DEMO_API_KEY"],
        acc_number=os.environ["IG_DEMO_ACC_NUMBER"],
    )


@dataclass(frozen=True)
class InstrumentConfig:
    epic: str
    name: str
    expiry: str
    currency_code: str


@dataclass(frozen=True)
class StrategyConfig:
    notional_trade_size: float = 1000.0
    model_update_interval_business_days: int = 7
    entry_stds: float = 1.5
    limit_stds: float = 1.0
    stop_stds: float = 3.0
    poll_interval_seconds: int = 10
    # Minutes of locally-cached streamed prices required before the first
    # hedge-ratio fit - no historical-data REST call is used (see
    # oil_pair/price_log.py), so this only affects a cold start with no
    # existing price_log/ticks.csv.
    warmup_minutes: int = 30


@dataclass(frozen=True)
class PairConfig:
    instrument_a: InstrumentConfig
    instrument_b: InstrumentConfig
    strategy: StrategyConfig


def load_pair_config(path: Path = DEFAULT_PAIR_CONFIG_PATH) -> PairConfig:
    if not path.exists():
        raise RuntimeError(
            f"{path} does not exist. Run `python scripts/discover_epics.py` first "
            f"to find and confirm the real IG epics for this pair."
        )

    with path.open("rb") as f:
        data = tomllib.load(f)

    try:
        instrument_a = InstrumentConfig(**data["instrument_a"])
        instrument_b = InstrumentConfig(**data["instrument_b"])
        strategy = StrategyConfig(**data.get("strategy", {}))
    except (KeyError, TypeError) as exc:
        raise RuntimeError(f"{path} is malformed: {exc}") from exc

    return PairConfig(instrument_a=instrument_a, instrument_b=instrument_b, strategy=strategy)
