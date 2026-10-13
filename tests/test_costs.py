import pandas as pd
import pytest

from oil_pair import costs
from oil_pair.costs import Holding


def held(name="Oil - Brent Crude", size=0.19, opened="2026-10-08T18:02:40"):
    return Holding(name, size, pd.Timestamp(opened, tz="UTC"))


def tx(rows):
    return pd.DataFrame(rows, columns=["transactionType", "instrumentName", "dateUtc", "openDateUtc", "size", "profitAndLoss"])


def fin(name, booked, amount, days=1):
    return ("WITH", f"Daily Financing Adjustment - Commodities Basis for {days} day {name}", booked, booked, "-", amount)


def fee(name, booked, amount, days=1):
    return ("WITH", f"Daily Admin Fee - Commodities Interest for {days} day {name}", booked, booked, "-", amount)


def deal(name, closed, opened, size, pnl="£0.00"):
    return ("DEAL", name, closed, opened, size, pnl)


def test_a_charge_is_shared_by_stake_among_positions_held_at_the_cutoff():
    gas = held()                                                    # 0.19, opened Thu 19:02 UK
    wti = held(size=0.10, opened="2026-10-05T11:22:09")             # 0.10 short Brent too
    t = tx([fin("Oil - Brent Crude", "2026-10-12T23:03:37", "£-2.90")])

    assert costs.overnight_share(t, gas, [wti]) == pytest.approx(-2.90 * 0.19 / 0.29)
    assert costs.overnight_share(t, wti, [gas]) == pytest.approx(-2.90 * 0.10 / 0.29)


def test_the_admin_fee_counts_as_well_as_the_financing():
    """IG books a "Daily Admin Fee" beside each financing charge - about a
    twelfth of it on Brent - and it's a cost like any other."""
    t = tx([fin("Oil - Brent Crude", "2026-10-12T23:03:37", "£-3.50"),
            fee("Oil - Brent Crude", "2026-10-12T23:07:33", "£-0.29")])

    assert costs.overnight_share(t, held(), []) == pytest.approx(-3.79)


def test_other_markets_charges_are_ignored():
    t = tx([fin("Oil - US Crude", "2026-10-12T23:03:37", "£1.00")])
    assert costs.overnight_share(t, held(), []) == 0


def test_deals_opened_and_closed_within_the_day_take_no_share():
    """2026-10-08: ten refused entries opened and closed gasoline within
    seconds that morning - they held nothing at 22:00 and mustn't dilute the
    position that did."""
    gas = held(name="No Lead Gasoline", size=0.06)
    blips = [deal("No Lead Gasoline", "2026-10-08T06:12:44", "2026-10-08T06:12:44", "+0.06", "£-2.40")] * 10
    t = tx([fin("No Lead Gasoline", "2026-10-08T23:12:59", "£3.41")] + blips)

    assert costs.overnight_share(t, gas, []) == pytest.approx(3.41)


def test_a_closed_deal_held_over_the_cutoff_does_share():
    gas = held(name="No Lead Gasoline", size=0.06)
    held_over = [deal("No Lead Gasoline", "2026-10-09T08:00:00", "2026-10-07T10:00:00", "+0.06", "£5.00")]
    t = tx([fin("No Lead Gasoline", "2026-10-08T23:12:59", "£3.00")] + held_over)

    assert costs.overnight_share(t, gas, []) == pytest.approx(1.50)


def test_a_just_closed_position_isnt_counted_twice():
    """At close, the position's own DEAL row is already in the history - it
    mustn't share the charges with itself. The recorded opening time is the
    app's, a second or two after IG's fill."""
    wti = held(size=0.10, opened="2026-10-05T11:22:11")
    t = tx([fin("Oil - Brent Crude", "2026-10-12T23:03:37", "£-3.50"),
            deal("Oil - Brent Crude", "2026-10-13T16:21:34", "2026-10-05T11:22:09", "-0.10", "£-28.30")])

    assert costs.overnight_share(t, wti, []) == pytest.approx(-3.50)


def test_charges_before_the_position_opened_are_ignored():
    t = tx([fin("Oil - Brent Crude", "2026-10-07T23:00:00", "£-5.00")])
    assert costs.overnight_share(t, held(), []) == 0


def test_the_weekend_charge_belongs_to_sunday_nights_cutoff():
    assert costs.financing_cutoff(pd.Timestamp("2026-10-11T23:20:31", tz="UTC")) == pd.Timestamp("2026-10-11T21:00", tz="UTC")
    assert costs.financing_cutoff(pd.Timestamp("2026-10-09T00:05:52", tz="UTC")) == pd.Timestamp("2026-10-08T21:00", tz="UTC")


def test_a_charge_not_yet_booked_is_flagged():
    """Held over Monday's 22:00 UK; at 22:40 UK IG hasn't booked it yet, by 00:10 it has."""
    t = tx([fin("Oil - Brent Crude", "2026-10-12T23:03:37", "£-3.50")])
    before = tx([])

    assert costs.unbooked(before, held(), pd.Timestamp("2026-10-12T21:40", tz="UTC"))
    assert not costs.unbooked(t, held(), pd.Timestamp("2026-10-12T23:10", tz="UTC"))
    # opened after the cutoff: nothing due
    assert not costs.unbooked(before, held(opened="2026-10-12T21:30"), pd.Timestamp("2026-10-12T21:40", tz="UTC"))


def test_holdings_reads_igs_open_positions():
    positions = pd.DataFrame({"instrumentName": ["Oil - Brent Crude"], "size": [0.19], "createdDateUTC": ["2026-10-08T18:02:40"]})
    assert costs.holdings(positions) == [held()]
