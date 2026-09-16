"""Covers a real incident: an EXIT decision's leg-closing loop had no
per-leg error isolation, so an IG-side error closing the first leg left the
second leg never even attempted, and main.py's `state` was never updated -
local tracking stayed frozen reporting both legs open while, in reality,
one was gone and the other sat open and unmonitored for a week."""

import pandas as pd
import pytest

from oil_pair.main import apply_decision
from oil_pair.settings import InstrumentConfig, PairConfig, StrategyConfig
from oil_pair.state_store import LegPosition, RunState
from oil_pair.strategy_logic import Action, Decision, Model, PairSide


@pytest.fixture
def pair_config():
    return PairConfig(
        instrument_a=InstrumentConfig(epic="EPIC.A", name="A", expiry="DFB", currency_code="GBP"),
        instrument_b=InstrumentConfig(epic="EPIC.B", name="B", expiry="DFB", currency_code="GBP"),
        strategy=StrategyConfig(notional_trade_size=1000),
    )


@pytest.fixture
def model():
    return Model(
        i1_key="EPIC.A", i2_key="EPIC.B",
        hedge_ratio=0.5, mean_spread=0.0, std_spread=1.0,
        entry_threshold=1.5, limit_threshold=1.0, stop_threshold=3.0,
        fitted_at=pd.Timestamp("2026-01-01", tz="UTC"), n_observations=100,
    )


@pytest.fixture
def rules():
    return {"EPIC.A": 0.04, "EPIC.B": 0.04}


@pytest.fixture
def mids():
    return {"EPIC.A": 100.0, "EPIC.B": 50.0}


class FakeClient:
    def __init__(self, fail_epics: set[str] = frozenset()):
        self._fail_epics = fail_epics
        self.open_calls = []
        self.close_calls = []

    def open_market_position(self, epic, expiry, direction, size, currency_code):
        self.open_calls.append(epic)
        if epic in self._fail_epics:
            raise RuntimeError(f"simulated failure opening {epic}")
        return {"dealId": f"DEAL-{epic}"}

    def close_market_position(self, deal_id, direction, size, epic=None):
        self.close_calls.append(epic)
        if epic in self._fail_epics:
            raise RuntimeError(f"simulated IG error closing {epic}")


def short_state():
    return RunState(
        side=PairSide.SHORT,
        stopped_out=False,
        leg_a=LegPosition(deal_id="DEAL-A", direction="SELL"),
        leg_b=LegPosition(deal_id="DEAL-B", direction="BUY"),
    )


def test_exit_closes_both_legs_and_returns_flat_when_both_succeed(pair_config, model, rules, mids):
    client = FakeClient()
    decision = Decision(action=Action.EXIT_TAKE_PROFIT, side=PairSide.FLAT, stopped_out=False, reason="test")

    result = apply_decision(client, decision, short_state(), pair_config, model, rules, mids)

    assert result == RunState(side=PairSide.FLAT, stopped_out=False)
    assert set(client.close_calls) == {"EPIC.A", "EPIC.B"}


def test_exit_still_attempts_leg_b_when_leg_a_close_fails(pair_config, model, rules, mids):
    client = FakeClient(fail_epics={"EPIC.A"})
    decision = Decision(action=Action.EXIT_STOP_LOSS, side=PairSide.FLAT, stopped_out=True, reason="test")

    result = apply_decision(client, decision, short_state(), pair_config, model, rules, mids)

    # leg_b MUST have been attempted despite leg_a's failure - this is the
    # exact regression: before the fix, the loop aborted after leg_a raised
    # and leg_b's close was never even attempted.
    assert "EPIC.B" in client.close_calls
    # leg_a is retained (not confirmed closed) so it's retried next time;
    # leg_b is cleared since it genuinely closed.
    assert result.side is PairSide.SHORT
    assert result.leg_a == LegPosition(deal_id="DEAL-A", direction="SELL")
    assert result.leg_b is None
    assert result.stopped_out is True


def test_exit_retains_leg_b_when_only_leg_b_close_fails(pair_config, model, rules, mids):
    client = FakeClient(fail_epics={"EPIC.B"})
    decision = Decision(action=Action.EXIT_STOP_LOSS, side=PairSide.FLAT, stopped_out=True, reason="test")

    result = apply_decision(client, decision, short_state(), pair_config, model, rules, mids)

    assert "EPIC.A" in client.close_calls
    assert result.side is PairSide.SHORT
    assert result.leg_a is None
    assert result.leg_b == LegPosition(deal_id="DEAL-B", direction="BUY")


def test_exit_retains_both_legs_when_both_closes_fail(pair_config, model, rules, mids):
    client = FakeClient(fail_epics={"EPIC.A", "EPIC.B"})
    decision = Decision(action=Action.EXIT_STOP_LOSS, side=PairSide.FLAT, stopped_out=True, reason="test")
    state = short_state()

    result = apply_decision(client, decision, state, pair_config, model, rules, mids)

    # neither leg confirmed closed - both retried next time; stopped_out
    # still reflects the latest decision even though nothing closed yet.
    assert result.side is state.side
    assert result.leg_a == state.leg_a
    assert result.leg_b == state.leg_b
    assert result.stopped_out is True


def test_enter_closes_first_leg_and_reraises_when_second_leg_fails_to_open(pair_config, model, rules, mids):
    client = FakeClient(fail_epics={"EPIC.B"})
    decision = Decision(action=Action.ENTER, side=PairSide.SHORT, stopped_out=False, reason="test")
    flat_state = RunState(side=PairSide.FLAT, stopped_out=False)

    with pytest.raises(RuntimeError):
        apply_decision(client, decision, flat_state, pair_config, model, rules, mids)

    assert client.open_calls == ["EPIC.A", "EPIC.B"]
    assert client.close_calls == ["EPIC.A"]  # corrective close of the leg that DID open
