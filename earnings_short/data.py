"""What the strategy reads from Jake's DB: S&P 500 earnings (EPS and revenue), the close before each,
SPY's closes, and IG's epic and whether it can be shorted.

earnings_sp500 keeps every version of a report's row (the forward calendar, then the actual once it
is out - in by the evening of the report day); the newest version with an actual is used. Its symbols
are FMP's (BRK-B); the price panel and IG's map use BRK.B.
"""

from __future__ import annotations

import pandas as pd

from oil_pair import jakefx_api

REPORTS_SQL = """
WITH r AS (
  SELECT DISTINCT ON (data->>'symbol', time::date)
         replace(data->>'symbol', '-', '.') AS symbol, time::date AS report_date,
         (data->>'eps_actual')::float AS eps_actual, (data->>'eps_estimated')::float AS eps_estimated,
         (data->>'revenue_actual')::float AS revenue_actual, (data->>'revenue_estimated')::float AS revenue_estimated
  FROM earnings_sp500
  WHERE time >= '{start}' AND time < '{end}'
    AND data->>'eps_actual' IS NOT NULL AND data->>'eps_estimated' IS NOT NULL
  ORDER BY data->>'symbol', time::date, available_at DESC NULLS LAST
)
SELECT r.symbol, r.report_date, r.eps_actual, r.eps_estimated, r.revenue_actual, r.revenue_estimated,
       p.close AS prev_close, p.day AS prev_day
FROM r LEFT JOIN LATERAL (
  SELECT day, close FROM us_minute_paths WHERE symbol = r.symbol AND day < r.report_date ORDER BY day DESC LIMIT 1
) p ON true
"""

CLOSES_SQL = """
SELECT day, close FROM us_minute_paths WHERE symbol = '{symbol}' AND day = ANY(ARRAY[{days}]::date[])
"""

TERMS_SQL = """
SELECT DISTINCT ON (m.symbol) m.symbol, m.epic, t.unborrowable
FROM us_minute_ig_map m
LEFT JOIN LATERAL (
  SELECT (data->>'unborrowable')::boolean AS unborrowable FROM ig_share_terms
  WHERE data->>'epic' = m.epic AND data->>'env' = 'live' ORDER BY time DESC LIMIT 1
) t ON true
WHERE m.valid_to IS NULL AND m.symbol = ANY(ARRAY[{symbols}])
ORDER BY m.symbol, (m.epic LIKE '%.DAILY.IP') DESC, m.epic
"""


def reports(start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    """Reports dated in [start, end) with an actual and a consensus, and the close before each."""
    df = jakefx_api.sql(REPORTS_SQL.format(start=f"{start:%Y-%m-%d}", end=f"{end:%Y-%m-%d}"), limit=20000, timeout_s=240)
    if len(df):
        df["report_date"] = pd.to_datetime(df["report_date"])
    return df


def closes(symbol: str, days: list[pd.Timestamp]) -> pd.Series:
    """`symbol`'s close on each of `days` (those it has), indexed by day."""
    if not days:
        return pd.Series(dtype=float)
    quoted = ", ".join(f"'{d:%Y-%m-%d}'" for d in days)
    df = jakefx_api.sql(CLOSES_SQL.format(symbol=symbol.replace("'", "''"), days=quoted), limit=1000)
    if df.empty:
        return pd.Series(dtype=float)
    return pd.Series(df["close"].astype(float).to_numpy(), index=pd.to_datetime(df["day"]))


def ig_terms(symbols: list[str]) -> pd.DataFrame:
    """symbol -> IG spread-bet epic and IG's live 'unborrowable' flag (None when IG gives none)."""
    if not symbols:
        return pd.DataFrame(columns=["symbol", "epic", "unborrowable"])
    quoted = ", ".join("'" + s.replace("'", "''") + "'" for s in symbols)
    return jakefx_api.sql(TERMS_SQL.format(symbols=quoted), limit=5000)
