import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "dashboard"))

import figures  # noqa: E402
from oil_pair.ledger import ClosedLeg, Trade  # noqa: E402

UTC = "UTC"


def test_read_log_gets_the_spread_history_and_recent_errors(tmp_path):
    log = tmp_path / "oil_pair.log"
    log.write_text(
        "2026-10-14 11:20:40,924 INFO __main__: spread=223.6 (0.75 std) entry=+-594.4 limit=+-297.2 stop=+-2080.4 "
        "std_spread=297.2 side=PairSide.FLAT\n"
        "2026-10-14 11:21:40,000 INFO trading_ig.rest: GET '/markets/X', resp 200\n"
        "2026-10-14 11:22:00,000 ERROR __main__: IG API error this iteration: boom\n"
        "2026-10-14 11:22:40,924 INFO __main__: spread=-629.8 (-2.12 std) entry=+-371.4 limit=+-0.0 stop=+-1633.9 "
        "std_spread=297.1 side=PairSide.LONG\n"
    )

    view = figures.read_log(log, pd.Timestamp("2026-10-14T11:30", tz=UTC))

    assert view.spread["z"].tolist() == [0.75, -2.12]
    assert view.spread["side"].tolist() == ["FLAT", "LONG"]
    assert view.spread.index[0] == pd.Timestamp("2026-10-14T10:20:40", tz=UTC)    # BST
    assert view.last_line_at == pd.Timestamp("2026-10-14T10:22:40", tz=UTC)
    assert len(view.recent_errors) == 1 and "boom" in view.recent_errors[0]


def test_errors_older_than_a_day_are_left_out(tmp_path):
    log = tmp_path / "oil_pair.log"
    log.write_text("2026-10-12 09:00:00,000 ERROR __main__: old\n")
    assert figures.read_log(log, pd.Timestamp("2026-10-14T11:30", tz=UTC)).recent_errors == []


def test_read_mids_puts_both_legs_on_one_grid(tmp_path):
    ticks = tmp_path / "ticks.csv"
    ticks.write_text(
        "timestamp,epic,mid\n"
        "2026-10-14T10:00:05+00:00,A,100.0\n2026-10-14T10:00:05+00:00,B,50.0\n"
        "2026-10-14T10:20:05+00:00,A,101.0\n"
    )

    mids = figures.read_mids(ticks, ["A", "B"])

    assert mids.loc[pd.Timestamp("2026-10-14T10:15", tz=UTC)].tolist() == [101.0, 50.0]   # B carried forward


def leg(name, size, opened, closed, open_level, close_level, profit):
    return ClosedLeg(name, size, pd.Timestamp(opened, tz=UTC), pd.Timestamp(closed, tz=UTC), open_level, close_level, profit)


def test_running_pnl_marks_an_open_trade_then_steps_to_what_ig_banked():
    names = {"Brent": "LCO", "WTI": "CL"}
    trade = Trade("brent_wti", "demo", (
        leg("Brent", -0.10, "2026-10-14T10:00", "2026-10-14T11:00", 100.0, 98.0, 0.20),
        leg("WTI", 0.11, "2026-10-14T10:00", "2026-10-14T11:00", 90.0, 90.5, 0.055),
    ), financing=-0.05)
    grid = pd.date_range("2026-10-14T09:45", "2026-10-14T11:15", freq="15min", tz=UTC)
    mids = pd.DataFrame({"LCO": [100.0, 100.0, 99.0, 99.0, 98.5, 98.5, 98.0],
                         "CL": [90.0, 90.0, 90.0, 90.0, 90.0, 90.0, 90.5]}, index=grid)

    run = figures.running_pnl([trade], [], mids, names, grid[0], grid[-1])

    assert run[pd.Timestamp("2026-10-14T09:45", tz=UTC)] == 0.0                             # before it opened
    assert run[pd.Timestamp("2026-10-14T10:30", tz=UTC)] == pytest.approx(-0.10 * (99.0 - 100.0))
    assert run[pd.Timestamp("2026-10-14T11:00", tz=UTC)] == pytest.approx(0.20 + 0.055 - 0.05)   # IG's figure


def test_running_pnl_includes_the_position_open_now():
    grid = pd.date_range("2026-10-14T10:00", "2026-10-14T10:30", freq="15min", tz=UTC)
    mids = pd.DataFrame({"LCO": [100.0, 101.0, 102.0]}, index=grid)
    open_now = [figures.OpenLeg("LCO", -0.5, 100.0, pd.Timestamp("2026-10-14T10:10", tz=UTC))]

    run = figures.running_pnl([], open_now, mids, {}, grid[0], grid[-1])

    assert run.tolist() == pytest.approx([0.0, -0.5, -1.0])


def test_close_messages_are_read_once_each_with_their_entry_time(tmp_path):
    log = tmp_path / "oil_pair.log"
    log.write_text(
        "2026-10-14 09:16:15,275 INFO __main__: ENTER PairSide.SHORT: SELL A size=0.14, BUY B size=1.2\n"
        "2026-10-16 17:25:24,534 INFO oil_pair.notify: emailed 'Closed SHORT pair (stop loss): -£175.17 after costs' "
        "to x@example.com\n"
        "2026-10-16 17:25:27,468 INFO oil_pair.notify: sent WhatsApp 'Closed SHORT pair (stop loss): -£175.17 after "
        "costs' to …999 in 2 parts (Message queued.)\n"
        "2026-10-06 13:09:03,328 INFO oil_pair.notify: emailed 'Closed LONG pair (take profit): -£7.24' to x\n"
    )

    closes = figures.read_log(log, pd.Timestamp("2026-10-17T12:00", tz=UTC)).closes

    assert len(closes) == 2
    stop = [c for c in closes if c.exit == "stop loss"][0]
    assert stop.net == -175.17 and stop.after_costs and stop.side == "SHORT"
    assert stop.opened == pd.Timestamp("2026-10-14T08:16:15", tz=UTC)
    old = [c for c in closes if c.exit == "take profit"][0]
    assert old.net == -7.24 and not old.after_costs


def test_a_logged_close_counts_only_until_ig_records_it():
    close = figures.LoggedClose(pd.Timestamp("2026-10-14T08:16", tz=UTC), pd.Timestamp("2026-10-16T16:25:24", tz=UTC),
                                "SHORT", "stop loss", -175.17, True)
    since = pd.Timestamp("2026-10-05", tz=UTC)
    in_ig = Trade("arabica_robusta", "demo", (leg("A", -0.14, "2026-10-14T08:16", "2026-10-16T16:25:21", 1, 1, -100),))

    assert figures.not_yet_in_ig((close,), [], since) == [close]
    assert figures.not_yet_in_ig((close,), [in_ig], since) == []


def test_a_logged_close_of_a_trade_opened_before_the_cut_off_is_left_out():
    early = figures.LoggedClose(pd.Timestamp("2026-10-02T13:00", tz=UTC), pd.Timestamp("2026-10-06T12:09", tz=UTC),
                                "LONG", "take profit", -7.24, False)
    assert figures.not_yet_in_ig((early,), [], pd.Timestamp("2026-10-05", tz=UTC)) == []


# --- the earnings-miss shorts ---

def short_book():
    from earnings_short import book as book_store
    stld = book_store.Position(
        "STLD", "UD.D.STLD.DAILY.IP", "S1", 0.24, 24350.0, 5844.0, "2026-10-20T19:40:09+00:00", "2026-11-16",
        "2026-10-19", -9.1e-4, 0.7e-4, "H1", 0.76, 7748.36, "IX.D.SPTRD.MONTH1.IP", "DEC-26")
    return book_store.Book("demo", [stld], [{
        "symbol": "BAC", "opened": "2026-09-15T19:40:00+00:00", "closed": "2026-10-12T19:45:00+00:00",
        "share_pnl": 30.0, "hedge_pnl": -12.0, "funding": -0.5, "dividends": -1.0, "net": 16.5}])


def test_open_shorts_are_marked_at_mid_with_their_hedge_and_funding():
    positions = pd.DataFrame({
        "dealId": ["S1", "H1"], "epic": ["UD.D.STLD.DAILY.IP", "IX.D.SPTRD.MONTH1.IP"],
        "instrumentName": ["Steel Dynamics Inc (24 Hours)", "US 500"], "size": [0.24, 0.76],
        "level": [24350.0, 7748.36], "bid": [23989.0, 7774.73], "offer": [26711.0, 7775.73],
        "createdDateUTC": ["2026-10-20T19:40:09", "2026-10-20T19:40:09"]})
    history = pd.DataFrame([
        {"transactionType": "DEPO", "instrumentName": "Short Interest for US/Can shares 20/10/26",
         "dateUtc": "2026-10-21T04:30:56", "openDateUtc": "2026-10-21T04:30:56", "size": "-", "profitAndLoss": "£0.09"},
        {"transactionType": "WITH", "instrumentName": "Stock Borrowing for US/Can shares 20/10/26",
         "dateUtc": "2026-10-21T04:30:56", "openDateUtc": "2026-10-21T04:30:56", "size": "-", "profitAndLoss": "£-0.13"}])

    (row,) = figures.open_shorts(short_book(), positions, history, pd.Timestamp("2026-10-21T12:00", tz=UTC)).to_dict("records")

    assert row["Share P&L"] == pytest.approx(0.24 * (24350 - 25350))           # mid 25,350: -£240
    assert row["Hedge P&L"] == pytest.approx(0.76 * (7775.23 - 7748.36))
    assert row["Funding"] == pytest.approx(-0.04)
    assert row["Net"] == pytest.approx(row["Share P&L"] + row["Hedge P&L"] - 0.04)
    assert row["IG spread now"] == pytest.approx(2722 / 25350)
    assert row["On IG"] == "yes" and row["Closes"] == "Mon 16 Nov"


def test_a_short_missing_from_ig_is_flagged():
    (row,) = figures.open_shorts(short_book(), pd.DataFrame({"dealId": []}), None,
                                 pd.Timestamp("2026-10-21T12:00", tz=UTC)).to_dict("records")
    assert row["On IG"] == "MISSING" and row["Funding"] is None


def test_closed_shorts_and_the_last_run(tmp_path):
    (row,) = figures.closed_shorts(short_book()).to_dict("records")
    assert row["Share"] == "BAC" and row["Funding"] == pytest.approx(-1.5) and row["Net"] == pytest.approx(16.5)

    log = tmp_path / "oil_pair.log"
    log.write_text("2026-10-19 20:40:07,921 INFO earnings_short: 5 report(s) from Fri 16 Oct, 0 miss(es)\n"
                   "2026-10-20 20:40:11,323 INFO earnings_short: 3 report(s) from Mon 19 Oct, 1 miss(es); opened 1\n"
                   "2026-10-20 20:40:11,324 INFO earnings_short:   Shorted STLD\n")
    at, what = figures.last_short_run(log)
    assert at == pd.Timestamp("2026-10-20T19:40:11", tz=UTC) and what.endswith("opened 1")
