# oil_pair — Brent Crude / Gasoline pairs trader (IG demo)

A standalone pairs-trading app: Brent Crude vs. unleaded gasoline, using IG
Markets for both price data and execution. Self-contained — no dependency on
any other repo. **Hardcoded to IG's demo API only.**

## Strategy

OLS hedge ratio (no intercept) between the two instruments' mid prices,
refit on a rolling business-day schedule. Enters a market-neutral pair
position (equal notional per leg) when the spread moves `entry_stds`
standard deviations from its fitted mean, exits at `limit_stds` (take
profit) or `stop_stds` (stop loss). See `oil_pair/strategy_logic.py` for the
exact, pure, unit-tested logic.

**The statistical validation step used elsewhere for new pairs (ADF/KPSS/
Hurst/cointegration screening) is deliberately skipped here** — this app
goes straight to live wiring with reasonable default thresholds, per an
explicit choice made when building it. There is no guarantee Brent Crude and
gasoline actually form a statistically valid mean-reverting pair.

## Setup

This app has its own virtual environment (`.venv/`), separate from any other
conda/venv on the machine, so there's no ambiguity about which interpreter
has its dependencies installed:

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
cp .env.example .env   # fill in your IG_DEMO_* credentials
./.venv/bin/python scripts/discover_epics.py   # interactive: finds & confirms real IG epics
```

If using VSCode, `.vscode/settings.json` already points the Python
extension at `.venv` - reload the window (or "Python: Select Interpreter")
if it's still resolving imports against a different environment.

`discover_epics.py` searches IG for Brent Crude and gasoline markets, lets
you pick the right one from the results, and writes `config/pair_config.toml`
with the chosen epics/expiries/currencies plus default strategy thresholds.
**Review that `[strategy]` block before running live** — the defaults are
generic, not tuned for this pair.

Re-run `discover_epics.py` (after deleting `config/pair_config.toml`)
whenever the chosen futures contract rolls to a new expiry.

## Running

```bash
./.venv/bin/python -m oil_pair.main
```

Logs to `logs/oil_pair.log` (rotating) and stdout.

## Manually closing a position

The app deliberately doesn't auto-flatten on shutdown (see below), so if you
need to close out now rather than wait for the strategy's own exit signal:

```bash
./.venv/bin/python scripts/close_positions.py
```

This closes exactly the leg(s) recorded in `state/run_state.json` - it will
**not** touch any other position that happens to exist on these epics, for
the same ownership reasons `reconcile_positions` won't adopt one on startup.
It refuses to run while `oil_pair.main` is live (same instance lock), since
both would write the same state file. If it reports nothing to close but
you believe a position is open, check IG directly rather than assuming the
local state is right - see `StateCorruptedError` below.

## Testing

```bash
./.venv/bin/python -m pytest tests/
```

All of `strategy_logic.py`'s math (hedge ratio, entry/exit decisions, leg
sizing) is pure and unit-tested with synthetic data — no network/IG
dependency required to run the test suite.

## Known simplifications (intentional, not bugs)

- **Live prices via IG's Lightstreamer feed** (`oil_pair/price_streamer.py`),
  not REST polling. A background thread keeps an in-memory cache of the
  latest bid/offer/market-status per epic; the main loop reads from that
  cache every `poll_interval_seconds` (default 10s) rather than making a
  REST call each iteration. If no update has been received for an epic in
  over `max_staleness_seconds` (default 60s), `latest_snapshot()` raises
  rather than silently trading on stale data. Historical seeding, dealing
  rules, and order placement/closing still use REST via `IGClient`.
- **No cross-venue price multiplier.** Both legs trade on IG, so there's no
  need to normalize between a data venue and an execution venue.
- **No auto-flatten on shutdown.** A clean Ctrl-C / SIGTERM stops the loop
  without closing open positions — on the next startup, `reconcile_positions`
  resumes a balanced two-leg position, or force-closes an orphaned single
  leg if one is found (partial fill / crash recovery).
- **Position ownership is tracked locally, not inferred from IG.** IG has no
  concept of "which strategy opened this position" - the same demo account
  may run other strategies or hold manual positions on these very epics. So
  this app records its own open legs' `deal_id`s in `state/run_state.json`
  (gitignored) and only ever resumes or closes positions it recognizes by
  that id. A missing/empty state file means "we own nothing here", never
  "adopt whatever's currently open on Brent/gasoline".
- **Trading-hours gate uses IG's live `marketStatus`**, not a hardcoded
  venue-hours table — simpler and correct through holidays/unscheduled
  closures.

## Safety notes

- `acc_type` is hardcoded to `"demo"` in `oil_pair/ig_client.py` — there is
  no code path in this app that can place a live order, even if live
  credentials were added to `.env`.
- `.env` (real credentials) and `logs/` are gitignored.
- If two consecutive-error iterations exceed `MAX_CONSECUTIVE_ERRORS` (10),
  the app exits loudly rather than spinning silently — check the logs.
- **Only one instance may run at a time.** `oil_pair/instance_lock.py`
  refuses to start a second concurrent run (`state/instance.lock`) — two
  instances racing to write `state/run_state.json` with no coordination
  previously corrupted it in practice (a bare `{}`, discovered live). A
  stale lock from a crashed process (dead pid) is reclaimed automatically;
  a live second instance gets a clear error, not silent corruption. If you
  ever see `state/run_state.json` fail to load with `StateCorruptedError`,
  check IG's actual open positions before deciding what to do — don't
  assume it's safe to just delete the file.
