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


def test_the_report_totals_net_and_lists_flat_pairs(ps):
    status = ps.PairStatus("brent_gasoline", "demo", "LONG", pd.Timestamp("2026-10-08T18:02", tz="UTC"),
                           (leg(ps),), financing=2.5, spread_std="-2.34 std (take profit +0.00, stop -5.50)")
    subject, body = ps.format_report([status], ["wti_heating_oil"], pd.Timestamp("2026-10-13T16:11", tz="UTC"))

    assert subject.startswith("Status: 1 open pair(s), net ")
    assert "brent_gasoline (DEMO) - LONG since Thu 08 Oct 19:02" in body
    assert "financing received +£2.50" in body
    assert "isn't booked" not in body
    assert "Flat: wti_heating_oil" in body


def test_the_report_says_when_the_latest_charge_isnt_booked_yet(ps):
    status = ps.PairStatus("brent_gasoline", "demo", "LONG", pd.Timestamp("2026-10-08T18:02", tz="UTC"),
                           (leg(ps),), financing=-1.0, spread_std="", unbooked=True)
    _, body = ps.format_report([status], [], pd.Timestamp("2026-10-13T21:30", tz="UTC"))

    assert "financing paid £1.00; IG hasn't booked the latest 22:00 UK charge yet" in body
