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
- `wti_heating_oil` — US Crude (WTI) vs. Heating Oil
- `arabica_robusta` — Coffee Arabica vs. Coffee Robusta (demo; the two
  markets overlap only ~8 hours a day, and the app trades only while both
  are open)

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

### How much data a fit uses

Two settings bound it from opposite ends:

- `warmup_minutes` (default 1440, i.e. one day) — the **minimum** span of
  cached prices before the first fit happens at all. Only bites on a cold
  start: a restart against a cache that already spans a day or more fits
  immediately.
- `max_fit_lookback_days` (default 30) — the **maximum** span any fit uses,
  initial or refit. `price_log/<pair_name>/ticks.csv` grows without bound
  across runs, and without this an old regime would eventually carry the same
  weight as this week.

So: wait for a day, then fit on up to a month, whichever is available.

Both the initial fit and the refits read the price log and window it. Refits
used to fit only on an in-memory buffer of ticks since the previous fit — one
refit interval, about a week — which is less than the lookback they are meant
to use.

Every fit then samples its window at **one price per hour** (the last in each
hour), so each hour carries equal weight however densely it was recorded. The
price log mixes densities — the live 15s feed adds ~240 prices an hour,
backfill from `../ig-prices` one, an outage none — and fitting raw rows let
whichever few days were streamed live decide the whole fit, leaving
`std_spread` (and with it every entry/limit/stop threshold) about 2× off.
Live entry/exit decisions still run on every 15s price; only the fit is
hourly. The cost: a cold start needs `MIN_FIT_OBSERVATIONS` (30) distinct
hours of prices, not just `warmup_minutes` of span.

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

## Notifications: email and WhatsApp

Optional. The app emails on a successful open, once both legs of an exit
have closed, and when startup closes a leg orphaned while the app was down
(see `oil_pair/notify.py`) — so it shows up on your phone through your
normal mail app with no extra software needed. Sending is best-effort: a
failed or unconfigured send is only logged and never affects trading, and
runs on a background thread so a slow SMTP server can't delay saving state
for the legs just traded.

Each email is written to be read on a phone (`oil_pair/trade_report.py`):

- **On open:**
  - each leg's stake, IG fill level, mid price and deal ID;
  - the spread now, in points and std;
  - the take-profit and stop levels, how far away each is, and a rough £
    estimate at each;
  - the entry band and the model's hedge ratio, std and fit time.
- **On close:**
  - each leg's opening and closing levels and IG's realised P&L, plus the
    total, which also goes in the subject;
  - how long the pair was held;
  - the spread at entry and exit, and how far it moved for or against the
    pair;
  - the levels in force at exit.

To make the close summary possible, the entry details (stakes, fill levels,
entry time and spread) are saved in `run_state.json`. Positions saved before
these fields existed still load, but their close emails show only IG's P&L.

To enable it, set in `.env` (see `.env.example`):

```
SMTP_HOST=smtp.mail.me.com
SMTP_PORT=587
SMTP_USERNAME=you@icloud.com
SMTP_PASSWORD=...
NOTIFY_EMAIL_TO=you@icloud.com
```

For iCloud, `SMTP_PASSWORD` is **not** your Apple ID password — generate a
dedicated app-specific password at
[appleid.apple.com](https://appleid.apple.com) under "Sign-In and Security"
→ "App-Specific Passwords". Gmail: `smtp.gmail.com`, port 587, with an app
password. The sender defaults to `SMTP_USERNAME` unless `NOTIFY_EMAIL_FROM`
is also set, and `NOTIFY_EMAIL_CC` (comma-separated) copies every
notification to additional addresses. Leaving `SMTP_HOST`/`SMTP_USERNAME`/`SMTP_PASSWORD`/
`NOTIFY_EMAIL_TO` unset disables the feature entirely — nothing changes
about how the app trades either way.

### WhatsApp

The same notifications can also go to WhatsApp, through CallMeBot's free
personal API. Email stays on: WhatsApp is the quick look, email is the record.

1. Each person who wants the alerts adds CallMeBot's number, +34 644 20 47 56
   (check [their page](https://www.callmebot.com/blog/free-api-whatsapp-messages/)
   in case it has changed), to their phone contacts.
2. Each person WhatsApps it "I allow callmebot to send me messages". The
   reply contains their API key. A key can only message the phone that
   registered it, so everyone needs their own.
3. List everyone in `.env`, as comma-separated `+<country code><number>:<apikey>`
   pairs:

   ```
   NOTIFY_WHATSAPP=+447700900123:123456,+447700900456:654321
   ```

4. Check it works with `./.venv/bin/python -m oil_pair.notify`. This sends
   a test message on every configured channel and logs each result.
5. Restart the pairs so they pick it up.

CallMeBot is unofficial and best-effort. A failed message is logged and
never affects trading, the same as a failed email. The API key never
appears in the logs.

## Manually closing a position

The app deliberately doesn't auto-flatten on shutdown (see below), so if you
need to close out now rather than wait for the strategy's own exit signal:

```bash
./.venv/bin/python scripts/close_positions.py <pair_name>          # demo account
./.venv/bin/python scripts/close_positions.py <pair_name> --live   # live account
```

As with the app, the demo account is used unless `--live` is passed. The
state file records which account a position was opened on, and the script
refuses to run against the other one, before logging in to IG. On the wrong
account every tracked deal would look already closed, and the script would
reset to FLAT with the real position still open.

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
  `market_status`; `latest_snapshot()` raises if any epic's last update is
  older than `max_staleness_seconds` (default 60s) - used only once the
  market is already confirmed tradeable, so a real stream problem during
  trading hours still fails loudly instead of silently trading on frozen
  data. Checking tradeability via `latest_snapshot()` directly used to make
  every weekend close look like a stream failure and trip
  `MAX_CONSECUTIVE_ERRORS` within minutes.
- **Prices come from `CHART:<epic>:TICK`, market status from REST.** This
  used to subscribe to `MARKET:<epic>`, which carried both. As of 2026-09-21
  IG rejects that item group outright (Lightstreamer error 21, "Invalid
  group") for every epic, on both demo account types and both session
  versions, while `CHART:...:TICK` on the same epics streams normally. Only
  the MARKET group carries `MARKET_STATE`, so status is now re-read over
  REST every 60s and cached. That status is what tells a closed market apart
  from a broken stream, so it could not simply be dropped. A failed refresh
  keeps the last known status rather than reading as "closed".
- **A quiet leg is priced from that same REST call.** `CHART:TICK` only
  pushes changes, so a leg whose price isn't moving sends nothing at all.
  The 60s status call also returns the current bid/offer, and that refreshes
  any leg the stream hasn't updated, at no extra API cost. Before this, a
  quiet leg aged past `max_staleness_seconds` and every iteration counted as
  an error. On 2026-10-06, heating oil went untraded for a few minutes after
  the 23:00 US reopen, and ten errors in a row stopped `wti_heating_oil`
  while the stream itself was fine. A staleness error now means neither
  source has produced a price, which is a real fault. Because REST can now
  stand in for a stream that died without IG saying so, a stream silent on
  every leg for 15 minutes while all markets are open is logged as a
  warning.
- **The subscription itself can also be torn down entirely**, separately
  from a market just going quiet - observed as `onUnsubscription` +
  `onSubscriptionError` with no automatic client-side reconnect, which once
  left a process silently dead for 13 days through several market opens and
  closes. `PriceStreamer.subscription_is_broken()`/`resubscribe()` exist for
  this: the main loop polls the former and calls the latter every iteration
  until it clears, which only happens once a real price arrives - re-issuing
  the subscribe call is not itself treated as evidence of recovery.
- **No historical-data REST call at all.** Every fit is seeded from
  `price_log/<pair_name>/ticks.csv` (built by this app's own past runs) plus
  freshly streamed prices - see `try_build_initial_model` and `refit_model`
  in `oil_pair/main.py`. This sidesteps IG's historical-data allowance, which
  is weekly and does not reset on retry (see `oil_pair/ig_client.py`'s
  history around this). Dealing rules and order placement/closing still use
  REST via `IGClient`.
- **The warmup checks span, not coverage.** `warmup_minutes` compares the
  oldest and newest point in the window; it does not check for holes between
  them. `MIN_FIT_OBSERVATIONS` (30, in `strategy_logic.py`) is the only guard
  on density, counted in distinct hours since fits sample hourly. A cache
  holding thirty scattered hours from three weeks ago would therefore satisfy
  a one-day warmup. In practice the log is written continuously while the app
  runs, so this only matters after a long outage.
- **The price log is never pruned.** It only ever grows, and each refit reads
  the whole file to window the last `max_fit_lookback_days`. At a 15s poll
  that is roughly 350k rows per month per pair - fine to read every few days,
  but it will not stay that way indefinitely.
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
- **A position is tied to the account it was opened on.** Demo and live
  runs of a pair share one `state/<pair_name>/run_state.json`, so it records
  `account` (`demo`/`live`) at entry. Starting the app, or
  `scripts/close_positions.py`, on the other account refuses to run
  (`AccountMismatchError`) rather than finding no deals there and resetting
  a still-open position to FLAT. Positions saved before this field existed
  are tagged the first time startup finds both legs open.
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
- **A deal IG refuses is treated as a failure, not a success.** IG answers a
  refused open or close normally, with `dealStatus: REJECTED` in the
  confirmation, and trading_ig doesn't raise on it. Every open and close
  checks it (`DealRejectedError` in `oil_pair/ig_client.py`):
  - A refused second leg unwinds the first.
  - A refused close keeps the leg tracked, so it is retried on the next
    iteration.
  - A refused orphan close at startup stops the app.
  - `scripts/close_positions.py` keeps any leg IG wouldn't close in the
    state file.
- **A failed entry pauses entries instead of retrying every iteration.** On
  2026-10-08 IG refused brent_gasoline's Brent leg at 07:10
  (`MINIMUM_ORDER_SIZE_ERROR`, on a £0.20/pt stake it had accepted many
  times). The loop retried every 15s: ten opens and closes of the gasoline
  leg in 2.5 minutes, each paying the spread, until `MAX_CONSECUTIVE_ERRORS`
  stopped the pair. Now (`EntryGuard` in `oil_pair/main.py`):
  - Entries pause for 30 minutes, doubling with each failure in a row up to
    8 hours. A successful entry resets the pause, and exits are never paused.
  - The refused leg is opened first next time, so a repeat refusal opens
    nothing.
  - IG's terms for both markets at that moment (advertised minimum size,
    status, quote) are logged and emailed, so the actual rule behind a
    refusal can be seen.
  - The email warns loudly if a leg IG refused to close again is left open.
- **Closes use the size IG holds**, read from IG's open positions. If IG
  can't be asked, the size recorded at entry is used instead. A size worked
  out from the current price drifts off the opened size whenever the price
  crosses a rounding boundary: Robusta at £0.85/pt becomes £0.86/pt below
  about 3,509. IG then refuses the close or closes only part of the
  position.
