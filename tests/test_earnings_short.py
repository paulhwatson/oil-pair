import numpy as np
import pandas as pd
import pytest

from earnings_short import book as book_store
from earnings_short import ib
from earnings_short import main as es
from earnings_short import sessions, signals

NY = "America/New_York"


# --- trading days ---

@pytest.mark.parametrize("day, open_", [
    ("2026-11-26", False),   # Thanksgiving
    ("2026-11-27", True),    # the early-close Friday after
    ("2027-03-26", False),   # Good Friday
    ("2026-07-03", False),   # 4 July on a Saturday: the Friday before
    ("2027-12-31", True),    # New Year's Day 2028 is a Saturday: no Friday closure
    ("2026-10-19", True),
    ("2026-10-18", False),   # Sunday
])
def test_nyse_sessions(day, open_):
    assert sessions.is_session(pd.Timestamp(day)) is open_


def test_session_arithmetic_skips_weekends_and_holidays():
    assert sessions.previous_session(pd.Timestamp("2026-10-19")) == pd.Timestamp("2026-10-16")
    assert sessions.first_session_on_or_after(pd.Timestamp("2026-10-17")) == pd.Timestamp("2026-10-19")
    assert sessions.add_sessions(pd.Timestamp("2026-11-24"), 2) == pd.Timestamp("2026-11-27")   # over Thanksgiving


# --- the rule ---

COLUMNS = ["symbol", "report_date", "eps_actual", "eps_estimated", "prev_close", "revenue_actual", "revenue_estimated"]


def reports(rows):
    """Rows of (symbol, date, eps actual, eps estimate, prev close[, revenue actual, revenue estimate])."""
    return pd.DataFrame([tuple(r) + (None,) * (len(COLUMNS) - len(r)) for r in rows], columns=COLUMNS)


def test_the_threshold_is_the_quantile_of_the_past_years_surprises():
    hist = reports([("X", "2026-01-01", i / 100, 0.0, 100.0) for i in range(-100, 150)])   # 250 surprises
    cut = signals.threshold(hist, 0.2)
    assert cut == pytest.approx(pd.Series([i / 1e4 for i in range(-100, 150)]).quantile(0.2))
    assert signals.threshold(hist.head(150), 0.2) is None                                   # too little history


def test_candidates_are_the_misses_at_or_below_the_cut_worst_first():
    r = reports([("BAC", "2026-10-14", 1.08, 1.10, 53.945), ("STT", "2026-10-14", 3.69, 3.68, 173.96),
                 ("PGR", "2026-10-14", 4.66, 4.35, 222.85), ("NOP", "2026-10-14", 1.0, 1.1, None)])
    picked = signals.candidates(r, cut=0.7e-4)
    assert [c.symbol for c in picked] == ["BAC", "STT"]
    assert picked[0].surprise == pytest.approx((1.08 - 1.10) / 53.945)


def test_what_backs_up_a_miss():
    hist = reports([("X", "2026-01-01", 1.0, 1.0, 100.0, 100 + i / 5, 100.0) for i in range(-25, 25)] * 5)  # -5%..+5%
    rev_cut = signals.revenue_threshold(hist, 0.2)
    assert rev_cut == pytest.approx(pd.Series([i / 500 for i in range(-25, 25)] * 5).quantile(0.2))
    assert signals.revenue_threshold(hist.head(150), 0.2) is None

    earlier = reports([("BAC", "2026-07-15", 0.80, 0.85, 45.0), ("STT", "2026-01-10", 2.0, 2.5, 100.0),
                       ("STT", "2026-07-15", 2.6, 2.5, 100.0)])
    earlier["report_date"] = pd.to_datetime(earlier.report_date)
    now = reports([("BAC", "2026-10-14", 1.08, 1.10, 53.945, 26.0e9, 27.5e9),     # revenue -5.5%
                   ("STT", "2026-10-14", 3.60, 3.68, 173.96, 3.5e9, 3.4e9),       # revenue beat
                   ("XYZ", "2026-10-14", 1.00, 1.10, 50.0)])                      # no revenue, no history
    now["report_date"] = pd.to_datetime(now.report_date)
    bac, stt, xyz = sorted(signals.candidates(now, 0.7e-4, rev_cut, earlier), key=lambda c: c.symbol)
    assert bac.previous_missed is True and stt.previous_missed is False and xyz.previous_missed is None

    assert signals.confirmations(bac, None, -0.037) == ["revenue -5.5% on consensus", "last report missed too"]
    assert signals.confirmations(stt, None, -0.037) == []
    down = signals.reaction(48.0, 50.0, 101.0, 100.0)                            # -4.1% less +1.0%
    assert down == pytest.approx(np.log(0.96) - np.log(1.01))
    assert signals.confirmations(stt, down, -0.037) == ["shares -5.0% on the market"]
    assert signals.confirmations(stt, signals.reaction(49.0, 50.0, 100.0, 100.0), -0.037) == []   # -2% isn't enough
    assert signals.reaction(4900.0, 50.0, 100.0, 100.0) is None                  # cents against dollars
    assert signals.describe(stt, None) == "revenue +2.9% (cut -3.0%), move n/a, last report beat or met"
    assert signals.describe(xyz, None).startswith("revenue n/a")


def test_stake_meets_igs_minimum_or_skips_a_share_too_dear_for_it():
    assert signals.stake(3000, 5394.5, 0.24, 2.0) == pytest.approx(0.56)      # ~£3,000
    assert signals.stake(3000, 17396, 0.24, 2.0) == 0.24                      # minimum: £4,175, under 2x
    assert signals.stake(3000, 33652, 0.24, 2.0) is None                      # minimum would be £8,076


# --- the book ---

def test_the_book_round_trips_and_refuses_the_other_account(tmp_path):
    path = tmp_path / "book.json"
    b = book_store.Book(account="demo", positions=[book_store.Position(
        "BAC", "EPIC.BAC", "D1", 0.59, 5390.0, 3180.0, "2026-10-15T19:45:00+00:00", "2026-11-11", "2026-10-14",
        -3.7e-4, 0.7e-4, "H1", 0.41, 7700.0)])
    book_store.save(b, path)
    assert book_store.load(path, "demo") == b
    with pytest.raises(RuntimeError, match="demo account"):
        book_store.load(path, "live")


# --- a run against a fake IG ---

class FakeIG:
    def __init__(self, open_ids=(), refuse=()):
        self.open_ids, self.refuse = set(open_ids), set(refuse)
        self.opened, self.closed = [], []
        self.n = 0

    def login(self):
        pass

    def fetch_open_positions(self):
        ids = sorted(self.open_ids)
        return pd.DataFrame({"dealId": ids, "epic": ["UC.D.BAC.DAILY.IP"] * len(ids),
                             "instrumentName": ["Bank of America Corp"] * len(ids), "size": [0.56] * len(ids),
                             "level": [5394.0] * len(ids), "createdDateUTC": ["2026-10-15T19:45:00"] * len(ids)})

    contracts = {"IX.D.SPTRD.MONTH1.IP": ("DEC-26", "2026-12-18T13:30"), "IX.D.SPTRD.MONTH2.IP": ("MAR-27", "2027-03-19T13:30"),
                 "IX.D.SPTRD.DAILY.IP": ("DFB", "2029-04-06T20:00")}

    def fetch_market(self, epic):
        mid = 7700.0 if epic.startswith("IX") else 5394.5
        expiry, last = self.contracts.get(epic, ("DFB", None))
        return {"snapshot": {"marketStatus": "TRADEABLE", "bid": mid - 1, "offer": mid + 1},
                "instrument": {"name": "US 500" if epic.startswith("IX") else "Bank of America Corp",
                               "expiry": expiry, "expiryDetails": {"lastDealingDate": last} if last else None},
                "dealingRules": {"minDealSize": {"value": 0.04 if epic.startswith("IX") else 0.24}}}

    def open_market_position(self, epic, expiry, direction, size, currency_code):
        self.n += 1
        self.opened.append((epic, direction, size))
        self.expiries = getattr(self, "expiries", []) + [expiry]
        if epic in self.refuse:
            return {"dealStatus": "REJECTED", "reason": "INSUFFICIENT_FUNDS"}
        deal = f"D{self.n}"
        self.open_ids.add(deal)
        return {"dealStatus": "ACCEPTED", "dealId": deal, "level": 5394.0}

    def close_market_position(self, deal_id, direction, size, epic=None):
        self.closed.append((deal_id, direction, size))
        self.open_ids.discard(deal_id)
        return {"dealStatus": "ACCEPTED", "dealId": deal_id, "profit": 25.0 if direction == "BUY" else -10.0}


@pytest.fixture
def harness(tmp_path, monkeypatch):
    monkeypatch.setattr(es, "STATE", tmp_path / "book.json")
    monkeypatch.setattr(es, "PAUSE_BETWEEN_DEALS", 0)
    # 250 past surprises from -1 to +1.5 bp of price: the 20% cut is about -0.5 bp; revenue surprises from
    # -5% to +5%: its 20% cut is -3%
    hist = reports([("H", "2026-01-01", i / 10000, 0.0, 100.0, 100 + (i % 50 - 25) / 5, 100.0) for i in range(-100, 150)])
    hist["report_date"] = pd.to_datetime(hist.report_date)
    # BAC missed on EPS, and on revenue by 5.5% - which backs up the miss
    today_reports = reports([("BAC", "2026-10-14", 1.08, 1.10, 53.945, 26.0e9, 27.5e9),
                             ("PGR", "2026-10-14", 4.66, 4.35, 222.85, 20e9, 19e9)])
    today_reports["report_date"] = pd.to_datetime(today_reports.report_date)
    today_reports["prev_day"] = pd.Timestamp("2026-10-13")

    def fake_reports(start, end):
        return today_reports if end > pd.Timestamp("2026-10-14") else hist

    monkeypatch.setattr(es.data, "reports", fake_reports)
    monkeypatch.setattr(es.data, "closes", lambda symbol, days: pd.Series({pd.Timestamp("2026-10-13"): 53.945}))
    monkeypatch.setattr(es.data, "ig_terms", lambda symbols: pd.DataFrame(
        {"symbol": ["BAC"], "epic": ["UC.D.BAC.DAILY.IP"], "unborrowable": [False]}))
    sent = []
    monkeypatch.setattr(es.Notifier, "_deliver_all", lambda self, subject, body: sent.append((subject, body)))
    fake = FakeIG()
    monkeypatch.setattr(es, "IGClient", lambda creds: fake)
    monkeypatch.setattr(es, "load_credentials", lambda live=False: None)
    return fake, sent, tmp_path / "book.json"


WED_1545 = pd.Timestamp("2026-10-15 15:45", tz=NY).tz_convert("UTC")


def test_a_miss_is_shorted_and_hedged_and_booked(harness):
    fake, sent, path = harness
    es.run(now=WED_1545)

    # £0.56/pt x 5,394.5 = £3,021 short; the hedge the same exposure in the US 500: 3,021 / 7,700 = £0.39/pt
    # hedged on the December contract (it lasts past the 11 Nov exit) - no overnight funding
    assert fake.opened == [("UC.D.BAC.DAILY.IP", "SELL", 0.56), ("IX.D.SPTRD.MONTH1.IP", "BUY", 0.39)]
    assert fake.expiries == ["DFB", "DEC-26"]
    b = book_store.load(path, "demo")
    (p,) = b.positions
    assert p.symbol == "BAC" and p.deal_id == "D1" and p.hedge_deal_id == "D2" and p.hedge_expiry == "DEC-26"
    assert p.exit_session == "2026-11-11"                     # 19 sessions on
    assert b.last_session == "2026-10-15"
    assert sent and sent[0][0].startswith("Earnings shorts: 1 opened")
    assert p.confirmations == ["revenue -5.5% on consensus"]
    assert "backed by revenue -5.5% on consensus" in sent[0][1]


def test_a_miss_nothing_backs_up_is_skipped(harness, monkeypatch):
    fake, sent, path = harness
    today = es.data.reports(None, pd.Timestamp("2026-10-15"))
    today.loc[today.symbol == "BAC", "revenue_actual"] = 27.6e9               # revenue beat
    es.run(now=WED_1545)                                                      # and SPY flat, as is BAC
    assert not [o for o in fake.opened if o[1] == "SELL"]
    assert "BAC: skipped - nothing backs up the miss (revenue +0.4% (cut -3.0%), shares +0.0% on the market, " \
           "no recent report)" in sent[0][1]


def test_a_miss_the_market_has_marked_down_is_shorted(harness, monkeypatch):
    fake, sent, path = harness
    today = es.data.reports(None, pd.Timestamp("2026-10-15"))
    today.loc[today.symbol == "BAC", "revenue_actual"] = 27.6e9               # revenue beat, but...
    monkeypatch.setattr(es.data, "closes", lambda symbol, days: pd.Series({pd.Timestamp("2026-10-13"): 50.0}))
    es.run(now=WED_1545)                                                      # SPY +7.9% while BAC is flat
    (p,) = book_store.load(path, "demo").positions
    assert p.symbol == "BAC" and p.confirmations == ["shares -7.3% on the market"]
    assert p.reaction == pytest.approx(-np.log(53.945 / 50.0))


def test_outside_the_window_or_after_todays_run_nothing_happens(harness):
    fake, sent, _ = harness
    es.run(now=pd.Timestamp("2026-10-15 11:00", tz=NY).tz_convert("UTC"))
    assert fake.opened == []
    es.run(now=WED_1545)
    es.run(now=WED_1545 + pd.Timedelta(minutes=5))           # second firing the same session
    assert len([o for o in fake.opened if o[1] == "SELL"]) == 1


def test_a_refused_short_is_reported_and_not_booked(harness):
    fake, sent, path = harness
    fake.refuse = {"UC.D.BAC.DAILY.IP"}
    es.run(now=WED_1545)
    assert book_store.load(path, "demo").positions == []
    assert "REFUSED" in sent[0][1]


def test_a_short_is_closed_with_its_hedge_when_its_session_comes(harness):
    fake, sent, path = harness
    es.run(now=WED_1545)
    exit_day = pd.Timestamp("2026-11-11 15:45", tz=NY).tz_convert("UTC")
    es.run(now=exit_day)
    assert fake.closed == [("D1", "BUY", 0.56), ("D2", "SELL", 0.39)]
    b = book_store.load(path, "demo")
    assert all(p.deal_id != "D1" for p in b.positions)
    assert b.closed[-1]["net"] == pytest.approx(15.0)
    assert "Closed BAC short" in sent[-1][1]


def test_a_close_is_reported_after_the_shorts_funding_and_dividends(harness, monkeypatch):
    fake, sent, path = harness
    es.run(now=WED_1545)                                      # short BAC 15 Oct, ≈ £3,021

    def row(name, booked, amount):
        return {"transactionType": "WITH" if amount.startswith("£-") else "DEPO", "instrumentName": name,
                "dateUtc": booked, "openDateUtc": booked, "size": "-", "profitAndLoss": amount}

    history = pd.DataFrame([
        row("Short Interest for US/Can shares 14/10/26", "2026-10-15T04:30:00", "£0.40"),   # before it opened
        row("Short Interest for US/Can shares 15/10/26", "2026-10-16T04:30:00", "£0.05"),
        row("Stock Borrowing for US/Can shares 15/10/26", "2026-10-16T04:30:00", "£-0.07"),
        row("Stock Borrowing for US/Can shares 10/11/26", "2026-11-11T04:30:00", "£-0.06"),
        row("Dividend Adjustment Bank of America Corp", "2026-11-05T06:00:00", "£-1.50"),
        row("Daily Admin Fee - Commodities Interest for 1 day Oil - Brent Crude", "2026-11-10T23:00:00", "£-0.40"),
    ])
    monkeypatch.setattr(es.costs, "fetch_transactions", lambda client, since: history)
    es.run(now=pd.Timestamp("2026-11-11 15:45", tz=NY).tz_convert("UTC"))

    # IG's P&L: share +£25, hedge -£10; funding -£0.08, the dividend -£1.50
    closed = book_store.load(path, "demo").closed[-1]
    assert closed["funding"] == pytest.approx(-0.08) and closed["dividends"] == pytest.approx(-1.50)
    assert closed["net"] == pytest.approx(15.0 - 0.08 - 1.50)
    assert ("Closed BAC short (15 Oct to 11 Nov): share +£25.00, hedge -£10.00, funding -£0.08, "
            "dividends -£1.50, net +£13.42 after costs") in sent[-1][1]
    assert sent[-1][0].endswith("(+£13.42)")

    # the mids at both ends are kept, for the dashboard's IB estimate
    assert (closed["entry_mid"], closed["exit_mid"]) == (5394.5, 5394.5)
    assert (closed["hedge_entry_mid"], closed["hedge_exit_mid"]) == (7700.0, 7700.0)
    assert closed["size"] == 0.56 and closed["hedge_size"] == 0.39


def test_the_ib_estimate():
    """£1/pt short of a $100 share that falls to $90 over a year, hedged with £1/pt of the US 500 rising
    5,000 -> 5,100, paying a £2 dividend."""
    costs = ib.IBCosts(commission_per_share=0.005, min_commission=0.40, borrow_rate=0.003, interest_rate=0.039)
    got = ib.estimate(costs, size=1.0, entry_mid=10_000, exit_mid=9_000, opened=pd.Timestamp("2026-01-01", tz="UTC"),
                      until=pd.Timestamp("2027-01-01", tz="UTC"), hedge_size=1.0, hedge_entry_mid=5_000,
                      hedge_exit_mid=5_100, dividends=-2.0)
    share = 1_000 - 0.003 * 10_000 - 2 * max(0.40, 0.005 * 10_000 / 100)       # 100 shares: £0.50 an order
    hedge = 100 + 0.039 * 5_000 - 2 * 0.40                                     # SPY at $500: the minimum
    assert got == pytest.approx(share + hedge - 2.0)
    unhedged = ib.estimate(costs, size=1.0, entry_mid=10_000, exit_mid=9_000, opened=pd.Timestamp("2026-01-01", tz="UTC"),
                           until=pd.Timestamp("2027-01-01", tz="UTC"))
    assert unhedged == pytest.approx(share)


def test_the_ib_rates_come_from_the_config():
    assert es.load_config().ib.borrow_rate == pytest.approx(0.003)


def test_a_close_without_igs_history_says_its_before_funding(harness):
    fake, sent, path = harness                                # FakeIG has no transaction history
    es.run(now=WED_1545)
    es.run(now=pd.Timestamp("2026-11-11 15:45", tz=NY).tz_convert("UTC"))
    assert "funding n/a (IG's history unavailable), net +£15.00 before funding" in sent[-1][1]


def test_a_position_closed_outside_the_app_is_dropped(harness):
    fake, sent, path = harness
    es.run(now=WED_1545)
    fake.open_ids.clear()                                     # closed by hand on IG
    es.run(now=pd.Timestamp("2026-10-16 15:45", tz=NY).tz_convert("UTC"))
    assert all(p.symbol != "BAC" for p in book_store.load(path, "demo").positions)
    assert "closed outside the app" in sent[-1][1]


def test_a_short_running_past_the_quarterly_expiry_hedges_on_the_next_contract():
    hm = [es.HedgeMarket("M1", "DEC-26", pd.Timestamp("2026-12-18T13:30", tz="UTC"), 7745.0, 0.04),
          es.HedgeMarket("M2", "MAR-27", pd.Timestamp("2027-03-19T13:30", tz="UTC"), 7790.0, 0.04),
          es.HedgeMarket("DFB", "DFB", None, 7704.0, 0.04)]
    assert es.pick_hedge(hm, pd.Timestamp("2026-11-11")).epic == "M1"
    assert es.pick_hedge(hm, pd.Timestamp("2026-12-18")).epic == "M2"      # December stops dealing that day
    assert es.pick_hedge(hm[2:], pd.Timestamp("2026-12-18")).epic == "DFB"   # neither quarterly open
