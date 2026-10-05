import pandas as pd
import pytest

from oil_pair import trade_report
from oil_pair.strategy_logic import Model, PairSide
from oil_pair.trade_report import LegFill

NAMES = {"EPIC.A": "Arabica", "EPIC.B": "Robusta"}
NOW = pd.Timestamp("2026-10-05T12:20:00", tz="UTC")


@pytest.fixture
def model():
    return Model(
        i1_key="EPIC.A", i2_key="EPIC.B",
        hedge_ratio=0.5, mean_spread=0.0, std_spread=1.0,
        entry_threshold=1.5, limit_threshold=1.0, stop_threshold=3.0,
        fitted_at=pd.Timestamp("2026-10-05T08:00:00", tz="UTC"), n_observations=175,
    )


def legs_opened(direction_a="SELL", direction_b="BUY"):
    return [
        LegFill("Arabica", "EPIC.A", direction_a, size=4.0, open_level=104.3, deal_id="DA"),
        LegFill("Robusta", "EPIC.B", direction_b, size=10.0, open_level=49.95, deal_id="DB"),
    ]


def test_opened_short_gives_both_exits_with_distance_and_estimate(model):
    mids = {"EPIC.A": 104.2, "EPIC.B": 50.0}  # spread = 52.1 - 50 = +2.10

    subject, body = trade_report.opened(PairSide.SHORT, "spread above entry threshold", legs_opened(), model, mids, NAMES, NOW)

    assert subject == "Opened SHORT pair at +2.10 std"
    assert body.startswith("Spread above entry threshold.")
    assert "SELL £4.00/pt Arabica: filled at 104.3 (mid 104.2), deal DA" in body
    # a short profits as the spread falls: Robusta size 10 x (2.10 - 1.00)
    assert "Take profit: when the spread falls below +1.00 (+1.00 std), 1.10 away, est. +£11.00" in body
    assert "Stop loss: when the spread rises above +3.00 (+3.00 std), 0.90 away, est. -£9.00" in body
    assert "spread = 0.500000 × Arabica − Robusta" in body
    assert "fitted Mon 05 Oct 09:00 UK on 175 hourly points" in body  # BST


def test_opened_long_mirrors_the_exits(model):
    mids = {"EPIC.A": 95.8, "EPIC.B": 50.0}  # spread = 47.9 - 50 = -2.10

    _, body = trade_report.opened(PairSide.LONG, "spread below -entry threshold", legs_opened("BUY", "SELL"), model, mids, NAMES, NOW)

    assert "Take profit: when the spread rises above -1.00 (-1.00 std), 1.10 away, est. +£11.00" in body
    assert "Stop loss: when the spread falls below -3.00 (-3.00 std), 0.90 away, est. -£9.00" in body


def test_a_zero_limit_never_prints_as_minus_zero(model):
    zero_limit = Model(**{**model.__dict__, "limit_threshold": 0.0})
    mids = {"EPIC.A": 95.8, "EPIC.B": 50.0}

    _, body = trade_report.opened(PairSide.LONG, "x", legs_opened("BUY", "SELL"), zero_limit, mids, NAMES, NOW)

    assert "rises above +0.00 (+0.00 std)" in body
    assert "-0.00" not in body


def test_closed_with_igs_profit_totals_it_in_the_subject(model):
    mids = {"EPIC.A": 102.0, "EPIC.B": 50.0}  # spread +1.00
    legs = [
        LegFill("Arabica", "EPIC.A", "SELL", 4.0, 104.3, "DA", close_level=102.1, profit=8.8, profit_from_ig=True),
        LegFill("Robusta", "EPIC.B", "BUY", 10.0, 49.95, "DB", close_level=49.9, profit=-0.5, profit_from_ig=True),
    ]

    subject, body = trade_report.closed(
        PairSide.SHORT, "take profit", "spread reverted past limit threshold", legs, model, mids, NAMES,
        now=NOW, opened_at=NOW - pd.Timedelta(hours=26, minutes=5), entry_spread=2.1, entry_std_spread=1.0,
    )

    assert subject == "Closed SHORT pair (take profit): +£8.30"
    assert "Arabica: SELL £4.00/pt, opened 104.3, closed 102.1, +£8.80" in body
    assert "Robusta: BUY £10.00/pt, opened 49.95, closed 49.9, -£0.50" in body
    assert "Total: +£8.30 (IG's realised P&L)" in body
    assert "Held 1d 2h 5m: opened Sun 04 Oct 11:15 UK, closed Mon 05 Oct 13:20 UK" in body
    assert "At entry: +2.10 (+2.10 std then)" in body
    assert "At exit: +1.00 (+1.00 std)" in body
    assert "Moved 1.10 in the pair's favour" in body


def test_closed_without_igs_profit_falls_back_to_the_fill_levels(model):
    mids = {"EPIC.A": 102.0, "EPIC.B": 50.0}
    legs = [
        LegFill("Arabica", "EPIC.A", "SELL", 4.0, 104.3, close_level=102.1,
                profit=trade_report.leg_profit("SELL", 4.0, 104.3, 102.1)),
        LegFill("Robusta", "EPIC.B", "BUY", 10.0, 49.95, close_level=49.9,
                profit=trade_report.leg_profit("BUY", 10.0, 49.95, 49.9)),
    ]

    subject, body = trade_report.closed(PairSide.SHORT, "take profit", "x", legs, model, mids, NAMES, now=NOW)

    assert subject == "Closed SHORT pair (take profit): +£8.30"
    assert "Total: +£8.30 (from the fill levels)" in body


def test_a_stop_against_the_pair_says_so(model):
    mids = {"EPIC.A": 106.2, "EPIC.B": 50.0}  # spread +3.10, past the stop

    _, body = trade_report.closed(
        PairSide.SHORT, "stop loss", "spread breached stop threshold", legs_opened(), model, mids, NAMES,
        now=NOW, entry_spread=2.1, entry_std_spread=1.0,
    )

    assert "Moved 1.00 against the pair" in body


def test_a_position_opened_before_entry_details_were_kept_still_emails(model):
    """State saved by an older version has no sizes, levels or entry time."""
    mids = {"EPIC.A": 102.0, "EPIC.B": 50.0}
    legs = [LegFill("Arabica", "EPIC.A", "SELL"), LegFill("Robusta", "EPIC.B", "BUY")]

    subject, body = trade_report.closed(PairSide.SHORT, "take profit", "x", legs, model, mids, NAMES, now=NOW)

    assert subject == "Closed SHORT pair (take profit)"
    assert "Arabica: SELL (size not recorded), opening level not recorded, closed near 102, P&L n/a" in body
    assert "Total: not available" in body
    assert "Closed Mon 05 Oct 13:20 UK" in body
    assert "At entry" not in body


def test_a_leg_closed_on_an_earlier_attempt_is_named_not_guessed(model):
    mids = {"EPIC.A": 102.0, "EPIC.B": 50.0}
    legs = [
        LegFill("Arabica", "EPIC.A", "", closed_earlier=True),
        LegFill("Robusta", "EPIC.B", "BUY", 10.0, 49.95, close_level=49.9, profit=-0.5, profit_from_ig=True),
    ]

    subject, body = trade_report.closed(PairSide.SHORT, "take profit", "x", legs, model, mids, NAMES, now=NOW)

    assert subject == "Closed SHORT pair (take profit)"
    assert "Arabica: closed on an earlier attempt - see IG for its result" in body
    assert "Total: not available" in body
