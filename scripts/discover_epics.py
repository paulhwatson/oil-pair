#!/usr/bin/env python3
"""One-time interactive setup: find and confirm the real IG epics for the
Brent Crude / gasoline pair, and write them to config/pair_config.toml.

Re-run this whenever the chosen futures contract rolls to a new expiry.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tomli_w

from oil_pair.discovery import format_candidates_table, search_multiple_terms
from oil_pair.ig_client import IGClient
from oil_pair.settings import DEFAULT_PAIR_CONFIG_PATH, StrategyConfig, load_credentials

GASOLINE_SEARCH_TERMS = ["Unleaded", "Gasoline", "RBOB", "Gas Oil"]


def prompt_for_row(df, label: str) -> dict:
    print(f"\n=== {label} candidates ===")
    print(format_candidates_table(df))
    if len(df) == 0:
        raise SystemExit(f"No candidates found for {label}. Try adjusting the search terms in this script.")
    while True:
        choice = input(f"\nPick the row number for {label} (or 'q' to quit): ").strip()
        if choice.lower() == "q":
            raise SystemExit("Aborted.")
        if choice.isdigit() and int(choice) in range(len(df)):
            return df.iloc[int(choice)].to_dict()
        print("Invalid row number, try again.")


def confirm_instrument(client: IGClient, epic: str, name: str, expiry: str) -> str:
    market = client.fetch_market(epic)
    rules = market.get("dealingRules", {})
    currencies = market.get("instrument", {}).get("currencies", [])
    print(f"\n--- {name} ({epic}) ---")
    print(f"expiry: {expiry}")
    print(f"dealingRules: {rules}")
    print(f"tradable currencies: {[c.get('code') for c in currencies]}")

    default_currency = currencies[0]["code"] if currencies else "USD"
    currency = input(f"Currency code to trade in [{default_currency}]: ").strip() or default_currency
    return currency


def main() -> None:
    creds = load_credentials()
    client = IGClient(creds)
    client.login()

    brent_candidates = client.search_markets("Brent Crude")
    brent_row = prompt_for_row(brent_candidates, "Brent Crude")

    gasoline_candidates = search_multiple_terms(client.search_markets, GASOLINE_SEARCH_TERMS)
    gasoline_row = prompt_for_row(gasoline_candidates, "Gasoline")

    currency_a = confirm_instrument(client, brent_row["epic"], brent_row["instrumentName"], brent_row["expiry"])
    currency_b = confirm_instrument(client, gasoline_row["epic"], gasoline_row["instrumentName"], gasoline_row["expiry"])

    if DEFAULT_PAIR_CONFIG_PATH.exists():
        print(f"\n{DEFAULT_PAIR_CONFIG_PATH} already exists - not overwriting (to avoid clobbering tuned "
              f"thresholds). Delete it first if you want to regenerate from scratch.")
        raise SystemExit(1)

    strategy_defaults = StrategyConfig()
    config = {
        "instrument_a": {
            "epic": brent_row["epic"],
            "name": brent_row["instrumentName"],
            "expiry": brent_row["expiry"],
            "currency_code": currency_a,
        },
        "instrument_b": {
            "epic": gasoline_row["epic"],
            "name": gasoline_row["instrumentName"],
            "expiry": gasoline_row["expiry"],
            "currency_code": currency_b,
        },
        "strategy": {
            "notional_trade_size": strategy_defaults.notional_trade_size,
            "model_update_interval_business_days": strategy_defaults.model_update_interval_business_days,
            "entry_stds": strategy_defaults.entry_stds,
            "limit_stds": strategy_defaults.limit_stds,
            "stop_stds": strategy_defaults.stop_stds,
            "poll_interval_seconds": strategy_defaults.poll_interval_seconds,
            "warmup_minutes": strategy_defaults.warmup_minutes,
        },
    }

    DEFAULT_PAIR_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with DEFAULT_PAIR_CONFIG_PATH.open("wb") as f:
        tomli_w.dump(config, f)

    print(f"\nWrote {DEFAULT_PAIR_CONFIG_PATH}")
    print(f"  instrument_a: {config['instrument_a']}")
    print(f"  instrument_b: {config['instrument_b']}")
    print("\nReview the [strategy] thresholds in that file before running oil_pair/main.py - "
          "these are generic defaults, not validated for this specific pair.")


if __name__ == "__main__":
    main()
