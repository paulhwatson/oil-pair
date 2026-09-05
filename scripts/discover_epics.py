#!/usr/bin/env python3
"""One-time interactive setup: find and confirm the real IG epics for a
pair, and write them to config/<pair_name>/pair_config.toml.

Usage:
    discover_epics.py <pair_name> <search terms for instrument A> -- <search terms for instrument B>

Example:
    discover_epics.py brent_gasoline "Brent Crude" -- "Unleaded" "Gasoline" "RBOB" "Gas Oil"

Re-run this whenever the chosen futures contract rolls to a new expiry
(delete config/<pair_name>/pair_config.toml first).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tomli_w

from oil_pair.discovery import format_candidates_table, search_multiple_terms
from oil_pair.ig_client import IGClient
from oil_pair.paths import resolve as resolve_paths
from oil_pair.settings import StrategyConfig, load_credentials


def prompt_for_row(df, label: str) -> dict:
    print(f"\n=== {label} candidates ===")
    print(format_candidates_table(df))
    if len(df) == 0:
        raise SystemExit(f"No candidates found for {label}. Try adjusting the search terms.")
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
    if "--" not in sys.argv:
        raise SystemExit(
            "usage: discover_epics.py <pair_name> <search terms for A...> -- <search terms for B...>\n"
            'example: discover_epics.py brent_gasoline "Brent Crude" -- "Unleaded" "Gasoline" "RBOB"'
        )
    pair_name = sys.argv[1]
    split = sys.argv.index("--")
    terms_a = sys.argv[2:split]
    terms_b = sys.argv[split + 1:]
    if not terms_a or not terms_b:
        raise SystemExit("need at least one search term on each side of --")

    paths = resolve_paths(pair_name)

    creds = load_credentials()
    client = IGClient(creds)
    client.login()

    candidates_a = search_multiple_terms(client.search_markets, terms_a)
    row_a = prompt_for_row(candidates_a, "instrument A")

    candidates_b = search_multiple_terms(client.search_markets, terms_b)
    row_b = prompt_for_row(candidates_b, "instrument B")

    currency_a = confirm_instrument(client, row_a["epic"], row_a["instrumentName"], row_a["expiry"])
    currency_b = confirm_instrument(client, row_b["epic"], row_b["instrumentName"], row_b["expiry"])

    if paths.pair_config.exists():
        print(f"\n{paths.pair_config} already exists - not overwriting (to avoid clobbering tuned "
              f"thresholds). Delete it first if you want to regenerate from scratch.")
        raise SystemExit(1)

    strategy_defaults = StrategyConfig()
    config = {
        "instrument_a": {
            "epic": row_a["epic"],
            "name": row_a["instrumentName"],
            "expiry": row_a["expiry"],
            "currency_code": currency_a,
        },
        "instrument_b": {
            "epic": row_b["epic"],
            "name": row_b["instrumentName"],
            "expiry": row_b["expiry"],
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

    paths.pair_config.parent.mkdir(parents=True, exist_ok=True)
    with paths.pair_config.open("wb") as f:
        tomli_w.dump(config, f)

    print(f"\nWrote {paths.pair_config}")
    print(f"  instrument_a: {config['instrument_a']}")
    print(f"  instrument_b: {config['instrument_b']}")
    print(f"\nReview the [strategy] thresholds in that file before running "
          f"`python -m oil_pair.main {pair_name}` - these are generic defaults, not validated for this pair.")


if __name__ == "__main__":
    main()
