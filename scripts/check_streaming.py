#!/usr/bin/env python3
"""Read-only: does IG stream these markets live on this account?

Searches IG for each term and prints, per matching market, whether IG will
stream its prices (streamingPricesAvailable) and with what delay. It logs in
and searches - nothing else. It places no orders and changes nothing on the
account, so it is safe to point at the live account, which is the point:
whether shares stream can differ between demo and live.

Usage:
    ./.venv/bin/python scripts/check_streaming.py            # demo account
    ./.venv/bin/python scripts/check_streaming.py --live     # live account
    ./.venv/bin/python scripts/check_streaming.py --live Tesco Unilever
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from oil_pair.ig_client import IGClient  # noqa: E402
from oil_pair.settings import load_credentials  # noqa: E402

DEFAULT_TERMS = ["Shell", "BP", "Lloyds", "Barclays", "Apple", "Microsoft", "PepsiCo"]
PAUSE_SECONDS = 3  # IG's per-minute request cap is shared with anything else on the account


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only check of which IG markets stream live.")
    parser.add_argument("--live", action="store_true", help="check the LIVE account (read-only) instead of demo")
    parser.add_argument("--type", default="SHARES", help="instrument type to report (default SHARES; ALL for every type)")
    parser.add_argument("terms", nargs="*", help=f"search terms (default: {' '.join(DEFAULT_TERMS)})")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)

    client = IGClient(load_credentials(live=args.live))
    client.login()
    print(f"{'LIVE' if args.live else 'DEMO'} account - read-only search, no orders")

    for term in args.terms or DEFAULT_TERMS:
        found = client.search_markets(term)
        if found is not None and len(found) and args.type != "ALL":
            found = found[found["instrumentType"] == args.type]
        if found is None or not len(found):
            print(f"{term}: nothing found")
        else:
            for row in found.head(2).itertuples():
                print(f"{row.instrumentName[:34]:34s} {row.epic:26s} expiry={row.expiry:7s} "
                      f"streaming={row.streamingPricesAvailable!s:5s} delay={row.delayTime}m")
        time.sleep(PAUSE_SECONDS)


if __name__ == "__main__":
    main()
