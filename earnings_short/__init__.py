"""Short S&P 500 shares after their biggest earnings misses, hedged with the US 500 (demo by default).

The backtest (2016 - 2026, scripts in ~/yahoo-price-data/screens/events_pead*): a report whose EPS
surprise, scaled by the price before it, is in the bottom 20% of the past year's, shorted at the
close of the session after the report and held 19 sessions, hedged with the S&P 500. It has worked
every year since 2023 (+25 to +170 bp a trade after IG's costs) and was a coin toss before, so this
runs on demo to see whether it holds going forward. See earnings_short/main.py.
"""
