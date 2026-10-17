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
import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
PRINT_LIMIT = 20_000


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only client for Jake's market-data API.")
    parser.add_argument("method", choices=["get", "post"])
    parser.add_argument("path", help="path under the API's base address, e.g. / or tables")
    parser.add_argument("--json", help="request body for post")
    parser.add_argument("--out", type=Path, help="write the response here instead of printing it")
    parser.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args()

    load_dotenv(REPO_ROOT / ".env")
    base, token = os.environ.get("JAKEFX_DB_URL"), os.environ.get("JAKEFX_DB_TOKEN")
    if not base or not token:
        sys.exit("JAKEFX_DB_URL and JAKEFX_DB_TOKEN must be set in .env")
    url = base.rstrip("/") + "/" + args.path.lstrip("/")
    headers = {"Authorization": f"Bearer {token}"}
    if args.method == "get":
        response = requests.get(url, headers=headers, timeout=args.timeout)
    else:
        response = requests.post(url, headers=headers, json=json.loads(args.json or "{}"), timeout=args.timeout)

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
