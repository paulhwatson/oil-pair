"""Covers the bug where reconcile_positions used to adopt/close ANY open
position on the traded epics, even ones belonging to another strategy or
opened manually. It must only ever act on deal_ids this app itself recorded
in local state."""

import pandas as pd
import pytest

from oil_pair.main import reconcile_positions
from oil_pair.settings import InstrumentConfig, PairConfig, StrategyConfig
from oil_pair.state_store import LegPosition, RunState
from oil_pair.strategy_logic import PairSide


@pytest.fixture
def pair_config():
    return PairConfig(
        instrument_a=InstrumentConfig(epic="EPIC.A", name="A", expiry="DFB", currency_code="GBP"),
        instrument_b=InstrumentConfig(epic="EPIC.B", name="B", expiry="DFB", currency_code="GBP"),
        strategy=StrategyConfig(),
    )


class FakeClient:
    def __init__(self, open_positions: pd.DataFrame):
        self._open_positions = open_positions
        self.close_calls = []

    def fetch_open_positions(self):
        return self._open_positions

    def close_market_position(self, deal_id, direction, size, epic=None):
        self.close_calls.append(dict(deal_id=deal_id, direction=direction, epic=epic, size=size))


def positions_df(rows):
    columns = ["dealId", "direction", "size", "expiry", "epic"]
    return pd.DataFrame(rows, columns=columns)


def test_no_saved_state_starts_flat_without_querying_ig(monkeypatch, pair_config):
    monkeypatch.setattr("oil_pair.main.load_state", lambda: None)
    client = FakeClient(positions_df([]))

    state = reconcile_positions(client, pair_config)

    assert state == RunState(side=PairSide.FLAT, stopped_out=False)
    assert client.close_calls == []


def test_no_saved_state_ignores_unrelated_open_position_on_same_epic(monkeypatch, pair_config):
    # A position exists on EPIC.A (e.g. from another strategy or manual
    # trading), but we have no record of ever opening it - must not touch it.
    monkeypatch.setattr("oil_pair.main.load_state", lambda: None)
    client = FakeClient(positions_df([
        {"dealId": "SOMEONE-ELSES-DEAL", "direction": "BUY", "size": 5, "expiry": "DFB", "epic": "EPIC.A"},
    ]))

    state = reconcile_positions(client, pair_config)

    assert state == RunState(side=PairSide.FLAT, stopped_out=False)
    assert client.close_calls == []


def test_saved_flat_state_returned_as_is(monkeypatch, pair_config):
    monkeypatch.setattr("oil_pair.main.load_state", lambda: RunState(side=PairSide.FLAT, stopped_out=True))
    client = FakeClient(positions_df([]))

    state = reconcile_positions(client, pair_config)

    assert state == RunState(side=PairSide.FLAT, stopped_out=True)
    assert client.close_calls == []


def test_resumes_when_both_tracked_legs_still_open(monkeypatch, pair_config):
    saved = RunState(
        side=PairSide.SHORT,
        stopped_out=False,
        leg_a=LegPosition(deal_id="DEAL-A", direction="SELL"),
        leg_b=LegPosition(deal_id="DEAL-B", direction="BUY"),
    )
    monkeypatch.setattr("oil_pair.main.load_state", lambda: saved)
    client = FakeClient(positions_df([
        {"dealId": "DEAL-A", "direction": "SELL", "size": 1, "expiry": "DFB", "epic": "EPIC.A"},
        {"dealId": "DEAL-B", "direction": "BUY", "size": 1, "expiry": "DFB", "epic": "EPIC.B"},
    ]))

    state = reconcile_positions(client, pair_config)

    assert state == saved
    assert client.close_calls == []


def test_closes_remaining_leg_when_tracked_pair_leg_is_gone(monkeypatch, pair_config):
    saved = RunState(
        side=PairSide.SHORT,
        stopped_out=False,
        leg_a=LegPosition(deal_id="DEAL-A", direction="SELL"),
        leg_b=LegPosition(deal_id="DEAL-B", direction="BUY"),
    )
    monkeypatch.setattr("oil_pair.main.load_state", lambda: saved)
    # leg_b (DEAL-B) is gone (e.g. IG's own risk control closed it); an
    # unrelated position also happens to sit on EPIC.B - must be ignored.
    client = FakeClient(positions_df([
        {"dealId": "DEAL-A", "direction": "SELL", "size": 3, "expiry": "DFB", "epic": "EPIC.A"},
        {"dealId": "SOMEONE-ELSES-DEAL", "direction": "BUY", "size": 99, "expiry": "DFB", "epic": "EPIC.B"},
    ]))

    state = reconcile_positions(client, pair_config)

    assert state == RunState(side=PairSide.FLAT, stopped_out=False)
    assert client.close_calls == [
        {"deal_id": "DEAL-A", "direction": "BUY", "epic": "EPIC.A", "size": 3}
    ]


def test_resets_to_flat_when_both_tracked_legs_already_closed(monkeypatch, pair_config):
    saved = RunState(
        side=PairSide.LONG,
        stopped_out=False,
        leg_a=LegPosition(deal_id="DEAL-A", direction="BUY"),
        leg_b=LegPosition(deal_id="DEAL-B", direction="SELL"),
    )
    monkeypatch.setattr("oil_pair.main.load_state", lambda: saved)
    client = FakeClient(positions_df([]))

    state = reconcile_positions(client, pair_config)

    assert state == RunState(side=PairSide.FLAT, stopped_out=False)
    assert client.close_calls == []
