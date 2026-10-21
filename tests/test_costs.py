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


def test_paging_reads_every_page_of_the_history():
    pages = {1: [{"instrumentName": "a"}] * 2, 2: [{"instrumentName": "b"}]}
    seen = []

    class Response:
        def __init__(self, page):
            self.page = page

        def raise_for_status(self):
            pass

        def json(self):
            return {"transactions": pages[self.page], "metadata": {"pageData": {"pageNumber": self.page, "totalPages": 2}}}

    class Service:
        def _req(self, method, path, params, session, version):
            seen.append(params["pageNumber"])
            return Response(params["pageNumber"])

    client = type("Client", (), {"service": Service()})()

    t = costs.fetch_transactions(client, pd.Timestamp("2026-09-01", tz="UTC"))

    assert seen == [1, 2] and t.instrumentName.tolist() == ["a", "a", "b"]


# --- shares: one funding row a night for all of a market's shares ---

def share(name, exposure, opened, group=costs.US_SHARES, closed=None, size=0.24):
    return Holding(name, size, pd.Timestamp(opened, tz="UTC"), group, exposure,
                   pd.Timestamp(closed, tz="UTC") if closed else None)


def funding_row(what, day, amount, group="US/Can shares ", booked=None):
    kind = "DEPO" if not amount.startswith("£-") else "WITH"
    booked = booked or pd.Timestamp(pd.to_datetime(day, format="%d/%m/%y") + pd.Timedelta(hours=28, minutes=30)).isoformat()
    return (kind, f"{what} for {group}{day}", booked, booked, "-", amount)


@pytest.mark.parametrize("epic, group", [
    ("UD.D.STLD.DAILY.IP", costs.US_SHARES), ("UA.D.AAPL.DAILY.IP", costs.US_SHARES),
    ("KA.D.BP.DAILY.IP", costs.UK_SHARES), ("KC.D.ASCL.DAILY.IP", costs.UK_SHARES),
    ("CC.D.LCO.USS.IP", None), ("IX.D.SPTRD.MONTH1.IP", None),
])
def test_a_markets_share_group_comes_from_its_epic(epic, group):
    assert costs.share_group(epic) == group


def test_a_nights_share_funding_is_split_by_exposure_among_the_shares_held_that_night():
    """2026-10-20: the STLD short (opened 19:40 UTC) was the only US share held that night - it gets
    all of IG's two rows. Had a second short of twice the exposure been held too, it would get a third."""
    stld = share("Steel Dynamics Inc", 5851.2, "2026-10-20T19:40:09")
    t = tx([funding_row("Short Interest", "20/10/26", "£0.09"), funding_row("Stock Borrowing", "20/10/26", "£-0.13")])

    assert costs.overnight_share(t, stld, []) == pytest.approx(-0.04)
    other = share("Other Inc", 11702.4, "2026-10-15T19:40:00")
    assert costs.overnight_share(t, stld, [other]) == pytest.approx(-0.04 / 3)


def test_share_funding_counts_only_nights_held_and_only_its_own_market():
    stld = share("Steel Dynamics Inc", 5851.2, "2026-10-20T19:40:09")
    t = tx([funding_row("Short Interest", "19/10/26", "£0.09"),              # before it opened
            funding_row("Short Interest", "21/10/26", "£0.09"),
            funding_row("Short Interest", "22/10/26", "£0.09"),              # after it closed
            funding_row("Stock Borrowing", "21/10/26", "£-0.06", group="")])  # UK shares': BP's
    closed = pd.Timestamp("2026-10-22T19:45", tz="UTC")

    assert costs.overnight_share(t, stld, [], until=closed) == pytest.approx(0.09)
    bp = share("BP PLC", 2487.17, "2026-10-19T07:00:47", group=costs.UK_SHARES, size=4.02)
    assert costs.overnight_share(t, bp, []) == pytest.approx(-0.06)


def test_a_share_since_closed_shares_the_nights_it_was_held():
    stld = share("Steel Dynamics Inc", 6000, "2026-10-20T19:40:09")
    gone = share("Gone Inc", 6000, "2026-10-01T19:40:00", closed="2026-10-21T19:45:00")
    t = tx([funding_row("Stock Borrowing", "20/10/26", "£-0.20"), funding_row("Stock Borrowing", "21/10/26", "£-0.10")])

    assert costs.overnight_share(t, stld, [gone]) == pytest.approx(-0.10 - 0.10)


def test_a_dividend_adjustment_naming_the_share_counts_against_it():
    bp = share("BP PLC", 2487.17, "2026-11-10T07:00:00", group=costs.UK_SHARES, size=4.02)
    t = tx([("WITH", "Dividend Adjustment BP PLC", "2026-11-12T06:10:00", "2026-11-12T06:10:00", "-", "£-24.12"),
            ("WITH", "Dividend Adjustment Shell PLC", "2026-11-12T06:10:00", "2026-11-12T06:10:00", "-", "£-9.00")])

    assert costs.overnight_share(t, bp, []) == pytest.approx(-24.12)
    stld = share("Steel Dynamics Inc (24 Hours)", 5851.2, "2026-10-20T19:40:09")      # IG's name for it
    t = tx([("WITH", "Dividend Adjustment Steel Dynamics Inc", "2026-12-31T06:10:00", "2026-12-31T06:10:00", "-", "£-1.20")])
    assert costs.overnight_share(t, stld, []) == pytest.approx(-1.20)


def test_a_shares_last_night_unbooked_until_its_row_arrives():
    stld = share("Steel Dynamics Inc", 5851.2, "2026-10-20T19:40:09")
    morning = pd.Timestamp("2026-10-21T03:00", tz="UTC")                     # IG books it about 04:30 UTC
    assert costs.unbooked(tx([]), stld, morning)
    assert not costs.unbooked(tx([funding_row("Short Interest", "20/10/26", "£0.09")]), stld, morning)
    sunday = pd.Timestamp("2026-10-25T12:00", tz="UTC")                      # the last weekday night: Friday's
    assert not costs.unbooked(tx([funding_row("Short Interest", "23/10/26", "£0.09")]), stld, sunday)
