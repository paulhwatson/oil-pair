import importlib.util
import sys
from pathlib import Path

import pandas as pd
import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "pair_status.py"


@pytest.fixture(scope="module")
def ps():
    spec = importlib.util.spec_from_file_location("pair_status", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["pair_status"] = module
    spec.loader.exec_module(module)
    return module


def leg(ps, name="Oil - Brent Crude", deal="D1", direction="SELL", size=0.19, level=10336.4, opened="2026-10-08T18:02:40",
        bid=10418.6, offer=10423.8):
    return ps.Leg(name, "EPIC", deal, direction, size, level, pd.Timestamp(opened, tz="UTC"), bid, offer)


def test_gross_is_the_move_at_mid_and_costs_are_the_spread(ps):
    short = leg(ps)                                   # sold at the bid, half a spread below the mid
    assert short.open_mid == pytest.approx(10336.4 + 2.6)
    assert short.gross() == pytest.approx(-0.19 * (10421.2 - 10339.0))
    assert short.spread_cost() == pytest.approx(0.19 * 5.2)
    buy = leg(ps, name="No Lead Gasoline", direction="BUY", size=0.06, level=32662.0, bid=31927.0, offer=31947.0)
    assert buy.gross() == pytest.approx(0.06 * (31937.0 - 32652.0))


def tx(rows):
    return pd.DataFrame(rows, columns=["transactionType", "instrumentName", "dateUtc", "openDateUtc", "size", "profitAndLoss"])


def fin(name, booked, amount, days=1):
    return ("WITH", f"Daily Financing Adjustment - Commodities Basis for {days} day {name}", booked, None, "-", amount)


def test_a_charge_is_shared_by_stake_among_positions_held_at_the_cutoff(ps):
    gas = leg(ps)                                                       # 0.19, opened Thu 19:02 UK
    wti = leg(ps, deal="D2", size=0.10, opened="2026-10-05T11:22:09")   # 0.10 short Brent too
    t = tx([fin("Oil - Brent Crude", "2026-10-12T23:03:37", "£-2.90")])

    assert ps.financing_share(t, gas, [gas, wti]) == pytest.approx(-2.90 * 0.19 / 0.29)
    assert ps.financing_share(t, wti, [gas, wti]) == pytest.approx(-2.90 * 0.10 / 0.29)


def test_deals_opened_and_closed_within_the_day_take_no_share(ps):
    """2026-10-08: ten refused entries opened and closed gasoline within
    seconds that morning - they held nothing at 22:00 and mustn't dilute the
    position that did."""
    gas = leg(ps, name="No Lead Gasoline", direction="BUY", size=0.06, level=32662.0, bid=31927.0, offer=31947.0)
    blips = [("DEAL", "No Lead Gasoline", "2026-10-08T06:12:44", "2026-10-08T06:12:44", "+0.06", "£-2.40")] * 10
    t = tx([fin("No Lead Gasoline", "2026-10-08T23:12:59", "£3.41")] + blips)

    assert ps.financing_share(t, gas, [gas]) == pytest.approx(3.41)


def test_a_closed_deal_held_over_the_cutoff_does_share(ps):
    gas = leg(ps, name="No Lead Gasoline", direction="BUY", size=0.06, opened="2026-10-08T18:02:40", bid=1, offer=1)
    held_over = [("DEAL", "No Lead Gasoline", "2026-10-09T08:00:00", "2026-10-07T10:00:00", "+0.06", "£5.00")]
    t = tx([fin("No Lead Gasoline", "2026-10-08T23:12:59", "£3.00")] + held_over)

    assert ps.financing_share(t, gas, [gas]) == pytest.approx(1.50)


def test_charges_before_the_position_opened_are_ignored(ps):
    gas = leg(ps)
    t = tx([fin("Oil - Brent Crude", "2026-10-07T23:00:00", "£-5.00")])
    assert ps.financing_share(t, gas, [gas]) == 0


def test_the_weekend_charge_belongs_to_sunday_nights_cutoff(ps):
    assert ps.financing_cutoff(pd.Timestamp("2026-10-11T23:20:31", tz="UTC")) == pd.Timestamp("2026-10-11T21:00", tz="UTC")
    assert ps.financing_cutoff(pd.Timestamp("2026-10-09T00:05:52", tz="UTC")) == pd.Timestamp("2026-10-08T21:00", tz="UTC")


def test_the_report_totals_net_and_lists_flat_pairs(ps):
    status = ps.PairStatus("brent_gasoline", "demo", "LONG", pd.Timestamp("2026-10-08T18:02", tz="UTC"),
                           (leg(ps),), financing=2.5, spread_std="-2.34 std (take profit +0.00, stop -5.50)")
    subject, body = ps.format_report([status], ["wti_heating_oil"], pd.Timestamp("2026-10-13T16:11", tz="UTC"))

    assert subject.startswith("Status: 1 open pair(s), net ")
    assert "brent_gasoline (DEMO) - LONG since Thu 08 Oct 19:02" in body
    assert "financing received +£2.50" in body
    assert "Flat: wti_heating_oil" in body
