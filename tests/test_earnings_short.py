import pandas as pd
import pytest

from earnings_short import book as book_store
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

def reports(rows):
    return pd.DataFrame(rows, columns=["symbol", "report_date", "eps_actual", "eps_estimated", "prev_close"])


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
    # 250 past surprises from -1 to +1.5 bp of price: the 20% cut is about -0.5 bp
    hist = reports([("H", "2026-01-01", i / 10000, 0.0, 100.0) for i in range(-100, 150)])
    today_reports = reports([("BAC", "2026-10-14", 1.08, 1.10, 53.945), ("PGR", "2026-10-14", 4.66, 4.35, 222.85)])
    today_reports["report_date"] = pd.to_datetime(today_reports.report_date)

    def fake_reports(start, end):
        return today_reports if end > pd.Timestamp("2026-10-14") else hist

    monkeypatch.setattr(es.data, "reports", fake_reports)
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
