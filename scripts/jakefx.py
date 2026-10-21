#!/usr/bin/env python3
"""Read-only client for Jake's market-data API (TimescaleDB behind a REST API).

The address and token come from .env (JAKEFX_DB_URL, JAKEFX_DB_TOKEN), so the
token never appears on a command line. Responses print to stdout (trimmed),
or go to a file with --out, e.g. a Parquet download.

Usage:
    ./.venv/bin/python scripts/jakefx.py get <path> [--out FILE]
    ./.venv/bin/python scripts/jakefx.py post <path> --json '<body>' [--out FILE]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from oil_pair import jakefx_api  # noqa: E402

PRINT_LIMIT = 20_000


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only client for Jake's market-data API.")
    parser.add_argument("method", choices=["get", "post"])
    parser.add_argument("path", help="path under the API's base address, e.g. / or tables")
    parser.add_argument("--json", help="request body for post")
    parser.add_argument("--out", type=Path, help="write the response here instead of printing it")
    parser.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args()

    try:
        response = jakefx_api.request(args.method, args.path, json.loads(args.json or "{}"), timeout=args.timeout)
    except RuntimeError as exc:
        sys.exit(str(exc))

    print(f"HTTP {response.status_code} {response.headers.get('content-type', '')} {len(response.content)} bytes",
          file=sys.stderr)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_bytes(response.content)
        print(f"saved {args.out}", file=sys.stderr)
    else:
        text = response.text
        print(text[:PRINT_LIMIT] + ("\n... (trimmed)" if len(text) > PRINT_LIMIT else ""))
    sys.exit(0 if response.ok else 1)


if __name__ == "__main__":
    main()
