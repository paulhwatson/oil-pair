"""Credentials (.env) and pair/strategy config (config/<pair_name>/pair_config.toml,
see oil_pair/paths.py) loading."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PAIR_CONFIG_PATH = REPO_ROOT / "config" / "pair_config.toml"


@dataclass(frozen=True)
class IGCredentials:
    username: str
    password: str
    api_key: str
    acc_number: str
    acc_type: str = "demo"


def load_credentials(live: bool = False) -> IGCredentials:
    """Reads IG_LIVE_* env vars when live=True, IG_DEMO_* otherwise. The two
    namespaces are kept completely separate (never a shared/generic IG_*
    var) so a credential meant for one account can never accidentally
    authenticate against the other - demo is what runs unless a caller
    explicitly asks for live.
    """
    load_dotenv(REPO_ROOT / ".env")

    prefix = "IG_LIVE" if live else "IG_DEMO"
    required = [f"{prefix}_USERNAME", f"{prefix}_PASSWORD", f"{prefix}_API_KEY", f"{prefix}_ACC_NUMBER"]

    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise RuntimeError(
            f"Missing required .env variables: {', '.join(missing)}. "
            f"Copy .env.example to .env and fill in your IG {'live' if live else 'demo'} credentials."
        )

    return IGCredentials(
        username=os.environ[f"{prefix}_USERNAME"],
        password=os.environ[f"{prefix}_PASSWORD"],
        api_key=os.environ[f"{prefix}_API_KEY"],
        acc_number=os.environ[f"{prefix}_ACC_NUMBER"],
        acc_type="live" if live else "demo",
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


@dataclass(frozen=True)
class EmailConfig:
    smtp_username: str
    smtp_app_password: str
    to_address: str


def load_email_config() -> EmailConfig | None:
    """Trade-close email notifications are optional. Returns None (never
    raises) when unset, so an incomplete/missing email setup can never
    prevent the trading loop itself from starting or running - this is a
    convenience on top of trading, not part of it.
    """
    load_dotenv(REPO_ROOT / ".env")

    username = os.environ.get("ICLOUD_SMTP_USERNAME")
    app_password = os.environ.get("ICLOUD_SMTP_APP_PASSWORD")
    if not username or not app_password:
        return None

    return EmailConfig(
        smtp_username=username,
        smtp_app_password=app_password,
        to_address=os.environ.get("TRADE_NOTIFY_TO") or username,
    )
