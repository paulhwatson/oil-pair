"""Read-only access to Jake's market-data API (a TimescaleDB behind a REST API).

The address and token come from .env (JAKEFX_DB_URL, JAKEFX_DB_TOKEN), so the token never appears on a
command line or in the repo. The server is shared and bounds every query (at most 200,000 rows and 300 s,
three queries at once), so callers ask for what they need and no more.
"""

from __future__ import annotations

import os

import pandas as pd
import requests
from dotenv import load_dotenv

from oil_pair.settings import REPO_ROOT

DEFAULT_TIMEOUT = 120


def _base_and_headers() -> tuple[str, dict]:
    load_dotenv(REPO_ROOT / ".env")
    base, token = os.environ.get("JAKEFX_DB_URL"), os.environ.get("JAKEFX_DB_TOKEN")
    if not base or not token:
        raise RuntimeError("JAKEFX_DB_URL and JAKEFX_DB_TOKEN must be set in .env")
    return base.rstrip("/"), {"Authorization": f"Bearer {token}"}


def request(method: str, path: str, body: dict | None = None, timeout: float = DEFAULT_TIMEOUT) -> requests.Response:
    base, headers = _base_and_headers()
    url = base + "/" + path.lstrip("/")
    if method == "get":
        return requests.get(url, headers=headers, timeout=timeout)
    return requests.post(url, headers=headers, json=body or {}, timeout=timeout)


def sql(query: str, limit: int = 5000, timeout_s: int = 120) -> pd.DataFrame:
    """Run one read-only SELECT and return its rows. Raises on an error or a truncated result."""
    response = request("post", "sql", {"sql": query, "limit": limit, "timeout_s": timeout_s}, timeout=timeout_s + 30)
    body = response.json()
    if not response.ok or "error" in body:
        raise RuntimeError(f"Jake's DB refused the query: {body.get('error', response.status_code)}")
    if body.get("truncated"):
        raise RuntimeError(f"Jake's DB truncated the result at {limit} rows - ask for less")
    return pd.DataFrame(body["rows"], columns=body["columns"])
