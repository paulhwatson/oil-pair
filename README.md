# oil_pair — multi-pair pairs trader (IG demo/live)

A standalone pairs-trading app using IG Markets for both price data and
execution. Self-contained — no dependency on any other repo. **Trades IG's
demo API by default; real-money trading requires explicitly passing `--live`
on the command line (see "Safety notes").**

Runs one pair per process, each identified by a `<pair_name>` (e.g.
`brent_gasoline`, `brent_wti`) that namespaces its own config, state,
price cache, and logs - see "Running multiple pairs" below. Currently
configured pairs:

- `brent_gasoline` — Brent Crude vs. unleaded gasoline
- `brent_wti` — Brent Crude vs. US Crude (WTI)

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
./.venv/bin/pip install -e .
cp .env.example .env   # fill in your IG_DEMO_* credentials
```

If using VSCode, `.vscode/settings.json` already points the Python
extension at `.venv` - reload the window (or "Python: Select Interpreter")
if it's still resolving imports against a different environment.

## Adding a pair

```bash
./.venv/bin/python scripts/discover_epics.py <pair_name> "<search terms for A>" -- "<search terms for B>"
# e.g.:
./.venv/bin/python scripts/discover_epics.py brent_gasoline "Brent Crude" -- "Unleaded" "Gasoline" "RBOB" "Gas Oil"
```

Interactively searches IG for each side (multiple search terms per side are
tried and merged, since the exact instrument name isn't always obvious),
lets you pick the right result, and writes
`config/<pair_name>/pair_config.toml` with the chosen epics/expiries/
currencies plus default strategy thresholds. **Review that `[strategy]`
block before running live** — the defaults are generic, not tuned for this
pair.

Re-run `discover_epics.py` for the same `<pair_name>` (after deleting
`config/<pair_name>/pair_config.toml`) whenever the chosen futures contract
rolls to a new expiry.

## Running

```bash
./.venv/bin/python -m oil_pair.main <pair_name>
# e.g.:
./.venv/bin/python -m oil_pair.main brent_gasoline
```

Trades IG's demo API by default. Pass `--live` to trade the real account
instead (see "Safety notes"):

```bash
./.venv/bin/python -m oil_pair.main brent_gasoline --live
```

Logs to `logs/<pair_name>/oil_pair.log` (rotating) and stdout.

## Running multiple pairs

Each pair is a **separate process** — there is no single process that trades
several pairs. Just start another one:

```bash
./.venv/bin/python -m oil_pair.main brent_gasoline &
./.venv/bin/python -m oil_pair.main brent_wti &
```

Each `<pair_name>` gets its own config/state/price-cache/log subtree
(`config/<pair_name>/`, `state/<pair_name>/`, `price_log/<pair_name>/`,
`logs/<pair_name>/` — see `oil_pair/paths.py`), and its own `instance_lock`,
so two *different* pairs never interfere with each other. Starting the
*same* `<pair_name>` twice is still refused (see "Safety notes").

**Running the same pair on two different machines is not protected** — the
instance lock is a local file, so it only stops two processes on the same
machine. Never run the same `<pair_name>` on two machines against the same
IG account at once.

## Trade-close email notifications

Optional. When a pair trade fully closes (both legs), the app can email a
plain-text summary — side, entry/exit spread, per-leg entry/exit mid price,
duration, and an estimated P&L — via iCloud Mail, so it shows up on your
phone through the stock Mail app with no extra software needed. Sending is
best-effort: a failed or unconfigured email never affects trading itself,
it's purely a notification layered on top (see `oil_pair/notifications.py`).

To enable it, set in `.env` (see `.env.example`):

```
ICLOUD_SMTP_USERNAME=you@icloud.com
ICLOUD_SMTP_APP_PASSWORD=...
```

`ICLOUD_SMTP_APP_PASSWORD` is **not** your Apple ID password — generate a
dedicated app-specific password at
[appleid.apple.com](https://appleid.apple.com) under "Sign-In and Security"
→ "App-Specific Passwords". Notifications go to `ICLOUD_SMTP_USERNAME`
itself unless `TRADE_NOTIFY_TO` is also set to a different address. Leaving
both unset disables the feature entirely — nothing changes about how the
app trades either way.

The summary only covers trades entered and closed within the same process
run — entry details aren't persisted to `run_state.json`, so a position
that was already open before a restart (and later closes) won't get an
email, same as the "no auto-flatten on shutdown" limitation below.

## Manually closing a position

The app deliberately doesn't auto-flatten on shutdown (see below), so if you
need to close out now rather than wait for the strategy's own exit signal:

```bash
./.venv/bin/python scripts/close_positions.py <pair_name>
```

This closes exactly the leg(s) recorded in `state/<pair_name>/run_state.json`
- it will **not** touch any other position that happens to exist on these
epics (including another pair's), for the same ownership reasons
`reconcile_positions` won't adopt one on startup. It refuses to run while
`oil_pair.main <pair_name>` is live for that same pair (same instance lock),
since both would write the same state file. If it reports nothing to close
but you believe a position is open, check IG directly rather than assuming
the local state is right - see `StateCorruptedError` below.

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
  REST call each iteration. Two ways to read it: `peek_snapshot()` returns
  whatever's cached, however stale, and never raises - used only to check
  `market_status` (IG pushes a status update when a market closes, e.g. for
  the weekend, even though prices stop moving); `latest_snapshot()` raises
  if any epic's last update is older than `max_staleness_seconds` (default
  60s) - used only once the market is already confirmed tradeable, so a
  real stream problem during trading hours still fails loudly instead of
  silently trading on frozen data. Checking tradeability via
  `latest_snapshot()` directly used to make every weekend close look like a
  stream failure and trip `MAX_CONSECUTIVE_ERRORS` within minutes.
- **No historical-data REST call at all.** The initial hedge-ratio fit is
  seeded from `price_log/<pair_name>/ticks.csv` (built by this app's own
  past runs) plus freshly streamed prices - see `try_build_initial_model` in
  `oil_pair/main.py`. This sidesteps IG's historical-data allowance, which
  is weekly and does not reset on retry (see `oil_pair/ig_client.py`'s
  history around this). Dealing rules and order placement/closing still use
  REST via `IGClient`.
- **No cross-venue price multiplier.** Both legs trade on IG, so there's no
  need to normalize between a data venue and an execution venue.
- **No auto-flatten on shutdown.** A clean Ctrl-C / SIGTERM stops the loop
  without closing open positions — on the next startup, `reconcile_positions`
  resumes a balanced two-leg position, or force-closes an orphaned single
  leg if one is found (partial fill / crash recovery).
- **Position ownership is tracked locally, not inferred from IG.** IG has no
  concept of "which strategy opened this position" - the same demo account
  may run other strategies (including other pairs run by this same app) or
  hold manual positions on these very epics. So this app records its own
  open legs' `deal_id`s in `state/<pair_name>/run_state.json` (gitignored)
  and only ever resumes or closes positions it recognizes by that id. A
  missing/empty state file means "we own nothing here", never "adopt
  whatever's currently open on these epics".
- **Trading-hours gate uses IG's live `marketStatus`**, not a hardcoded
  venue-hours table — simpler and correct through holidays/unscheduled
  closures.

## Safety notes

- **Demo is the default; live requires an explicit `--live` flag.** Without
  it, `load_credentials()` only ever reads the `IG_DEMO_*` vars, and
  `IGClient` logs in against IG's demo API. There is no env var that alone
  switches this - the flag has to be passed on the command line every time,
  so a real account can't get traded by an env var left over from a
  previous session.
- **Demo and live credentials live in separate `.env` namespaces**
  (`IG_DEMO_*` vs `IG_LIVE_*`, see `.env.example`) - `--live` only ever
  reads `IG_LIVE_*`, so a typo can't point one account's credentials at the
  other.
- `.env` (real credentials) and `logs/` are gitignored.
- If two consecutive-error iterations exceed `MAX_CONSECUTIVE_ERRORS` (10),
  the app exits loudly rather than spinning silently — check the logs.
- **Only one instance of the same pair may run at a time.**
  `oil_pair/instance_lock.py` refuses to start a second concurrent run of
  the same `<pair_name>` (`state/<pair_name>/instance.lock`) — two instances
  racing to write the same `run_state.json` with no coordination previously
  corrupted it in practice (a bare `{}`, discovered live). A stale lock from
  a crashed process (dead pid) is reclaimed automatically; a live second
  instance gets a clear error, not silent corruption. Different pairs have
  independent locks and don't affect each other. If you ever see a
  `run_state.json` fail to load with `StateCorruptedError`, check IG's
  actual open positions before deciding what to do — don't assume it's safe
  to just delete the file.
