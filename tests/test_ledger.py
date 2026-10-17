import pandas as pd
import pytest

from oil_pair import ledger
from oil_pair.costs import Holding
from oil_pair.ledger import LogEvent, PairMarkets

PAIRS = [
    PairMarkets("brent_wti", {"LCO": "Oil - Brent Crude", "CL": "Oil - US Crude"}),
    PairMarkets("brent_gasoline", {"LCO": "Oil - Brent Crude", "RB": "No Lead Gasoline"}),
    PairMarkets("wti_heating_oil", {"CL": "Oil - US Crude", "HO": "Heating Oil"}),
]
COLUMNS = ["transactionType", "instrumentName", "dateUtc", "openDateUtc", "size", "openLevel", "closeLevel", "profitAndLoss"]


def deal(name, opened, closed, size, open_level, close_level, pnl):
    return ("DEAL", name, closed, opened, size, open_level, close_level, pnl)


def fin(name, booked, amount):
    return ("WITH", f"Daily Financing Adjustment - Commodities Basis for 1 day {name}", booked, booked, "-", "-", "0", amount)


def tx(rows):
    return pd.DataFrame(rows, columns=COLUMNS)


def test_legs_opened_together_are_one_pair_trade():
    t = tx([deal("Oil - Brent Crude", "2026-10-05T11:22:09", "2026-10-13T16:21:34", "-0.10", "10145.8", "10428.8", "£-28.30"),
            deal("Oil - US Crude", "2026-10-05T11:22:09", "2026-10-13T16:21:34", "+0.11", "8939.3", "9316", "£41.44")])

    (trade,) = ledger.build_trades(t, PAIRS, "demo")

    assert trade.pair == "brent_wti"
    assert trade.side == "SHORT"            # the higher-priced leg, Brent, was sold
    assert trade.realised == pytest.approx(13.14)
    assert trade.opened == pd.Timestamp("2026-10-05T11:22:09", tz="UTC")


def test_each_refused_entry_is_its_own_lone_leg():
    """2026-10-08: ten entries 15 seconds apart, each gasoline-only because
    IG refused the Brent leg - ten trades, not one."""
    t = tx([deal("No Lead Gasoline", f"2026-10-08T06:1{i // 4}:{(i % 4) * 15:02d}", f"2026-10-08T06:1{i // 4}:{(i % 4) * 15 + 1:02d}",
                 "+0.06", "32237", "32197", "£-2.40") for i in range(10)])

    trades = ledger.build_trades(t, PAIRS, "demo")

    assert len(trades) == 10
    assert {tr.pair for tr in trades} == {"brent_gasoline"}   # the only pair trading gasoline
    assert trades[0].side == "" and "one leg only" in trades[0].note


def test_a_position_closed_in_two_parts_stays_one_trade():
    t = tx([deal("No Lead Gasoline", "2026-09-04T14:48:40", "2026-09-06T22:25:04", "-0.04", "31266", "31594", "£-13.12"),
            deal("Oil - Brent Crude", "2026-09-04T14:48:40", "2026-09-06T22:25:04", "+0.10", "9423.3", "9543", "£11.97"),
            deal("Oil - Brent Crude", "2026-09-04T14:48:40", "2026-09-08T08:20:26", "+0.01", "9423.3", "9743.5", "£3.20")])

    (trade,) = ledger.build_trades(t, PAIRS, "demo")

    assert trade.pair == "brent_gasoline" and len(trade.legs) == 3
    assert trade.closed == pd.Timestamp("2026-09-08T08:20:26", tz="UTC")


def test_a_lone_leg_goes_to_the_pair_already_trading_that_market():
    """A WTI leg on its own in September belongs to brent_wti: wti_heating_oil
    didn't trade until October."""
    t = tx([deal("Oil - Brent Crude", "2026-09-08T09:05:49", "2026-09-08T09:33:52", "+0.10", "9729.8", "9710.5", "£-1.93"),
            deal("Oil - US Crude", "2026-09-08T09:05:50", "2026-09-08T09:33:53", "-0.11", "9231.6", "9212.9", "£2.06"),
            deal("Oil - US Crude", "2026-09-09T02:47:50", "2026-09-16T12:08:32", "+0.11", "9239.8", "9948.3", "£77.94"),
            deal("Heating Oil", "2026-10-08T15:39:13", "2026-10-09T18:47:42", "-0.06", "48411", "46147", "£135.84"),
            deal("Oil - US Crude", "2026-10-08T15:39:14", "2026-10-09T18:47:41", "+0.33", "9219.5", "9049.4", "£-56.13")])

    trades = ledger.build_trades(t, PAIRS, "demo")

    assert [tr.pair for tr in trades] == ["brent_wti", "brent_wti", "wti_heating_oil"]


def test_the_pairs_logs_decide_when_the_markets_cant():
    """A lone Brent leg could be brent_wti's or brent_gasoline's; the log of
    the pair that opened it says which, and its exit."""
    t = tx([deal("Oil - Brent Crude", "2026-10-01T17:13:16", "2026-10-01T17:13:32", "-0.10", "10087", "10096.2", "£-0.92")])
    events = [LogEvent("brent_wti", pd.Timestamp("2026-10-01T17:13:16", tz="UTC"), "Oil - Brent Crude"),
              LogEvent("brent_wti", pd.Timestamp("2026-10-01T17:13:31", tz="UTC"), "stop loss")]

    (trade,) = ledger.build_trades(t, PAIRS, "demo", events)

    assert trade.pair == "brent_wti" and trade.exit == "stop loss"


def test_financing_stops_when_the_trade_closed():
    """Charges booked after a trade closed belong to whatever held the market then."""
    t = tx([deal("Oil - Brent Crude", "2026-10-05T11:22:09", "2026-10-13T16:21:34", "-0.10", "10145.8", "10428.8", "£-28.30"),
            deal("Oil - US Crude", "2026-10-05T11:22:09", "2026-10-13T16:21:34", "+0.11", "8939.3", "9316", "£41.44"),
            fin("Oil - Brent Crude", "2026-10-12T23:03:37", "£-3.50"),     # while held
            fin("Oil - Brent Crude", "2026-10-13T23:01:00", "£-3.60")])    # after it closed

    (trade,) = ledger.build_trades(t, PAIRS, "demo")

    assert trade.financing == pytest.approx(-3.50)
    assert trade.net == pytest.approx(13.14 - 3.50)


def test_open_positions_share_the_charges_of_closed_trades():
    t = tx([deal("Oil - Brent Crude", "2026-10-05T11:22:09", "2026-10-13T16:21:34", "-0.10", "10145.8", "10428.8", "£-28.30"),
            deal("Oil - US Crude", "2026-10-05T11:22:09", "2026-10-13T16:21:34", "+0.11", "8939.3", "9316", "£41.44"),
            fin("Oil - Brent Crude", "2026-10-12T23:03:37", "£-2.90")])
    still_open = [Holding("Oil - Brent Crude", 0.19, pd.Timestamp("2026-10-08T18:02:40", tz="UTC"))]

    (trade,) = ledger.build_trades(t, PAIRS, "demo", open_positions=still_open)

    assert trade.financing == pytest.approx(-2.90 * 0.10 / 0.29)


def test_banked_by_day_is_cumulative_after_costs():
    t = tx([deal("Oil - Brent Crude", "2026-10-05T11:22:09", "2026-10-13T16:21:34", "-0.10", "10145.8", "10428.8", "£-28.30"),
            deal("Oil - US Crude", "2026-10-05T11:22:09", "2026-10-13T16:21:34", "+0.11", "8939.3", "9316", "£41.44")])
    trades = ledger.build_trades(t, PAIRS, "demo")
    days = pd.date_range("2026-10-12", "2026-10-14", freq="D")

    banked = ledger.banked_by_day(trades, ["brent_wti", "brent_gasoline"], days)

    assert banked["brent_wti"].tolist() == pytest.approx([0.0, 13.14, 13.14])
    assert banked["brent_gasoline"].tolist() == [0.0, 0.0, 0.0]


def test_read_log_finds_legs_and_exits(tmp_path):
    log = tmp_path / "oil_pair.log"
    log.write_text(
        "2026-10-13 17:21:33,984 INFO __main__: decision: Action.EXIT_TAKE_PROFIT reason='spread reverted' spread=295.9\n"
        "2026-10-05 12:22:09,300 INFO oil_pair.ig_client: opening market position: SELL LCO size=0.1\n"
        "2026-10-05 12:22:09,400 INFO oil_pair.ig_client: opening market position: BUY OTHER size=0.1\n"
    )

    events = ledger.read_log(log, PAIRS[0])

    assert events == [
        LogEvent("brent_wti", pd.Timestamp("2026-10-13T16:21:33", tz="UTC"), "take profit"),       # BST
        LogEvent("brent_wti", pd.Timestamp("2026-10-05T11:22:09", tz="UTC"), "Oil - Brent Crude"),
    ]


def test_load_pairs_reads_each_pairs_two_markets(tmp_path):
    cfg = tmp_path / "brent_wti" / "pair_config.toml"
    cfg.parent.mkdir()
    cfg.write_text('[instrument_a]\nepic = "CC.D.LCO.USS.IP"\nname = "Oil - Brent Crude"\n'
                   '[instrument_b]\nepic = "CC.D.CL.USS.IP"\nname = "Oil - US Crude"\n')

    (pair,) = ledger.load_pairs(tmp_path)

    assert pair.pair == "brent_wti"
    assert pair.names == {"Oil - Brent Crude", "Oil - US Crude"}
