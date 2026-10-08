"""Covers a real incident: an EXIT decision's leg-closing loop had no
per-leg error isolation, so an IG-side error closing the first leg left the
second leg never even attempted, and main.py's `state` was never updated -
local tracking stayed frozen reporting both legs open while, in reality,
one was gone and the other sat open and unmonitored for a week."""

import pandas as pd
import pytest

from oil_pair.main import EntryFailedError, apply_decision
from oil_pair.settings import InstrumentConfig, PairConfig, StrategyConfig
from oil_pair.state_store import LegPosition, RunState
from oil_pair.strategy_logic import Action, Decision, Model, PairSide


def _positions_df(deal_ids: list[str]) -> pd.DataFrame:
    return pd.DataFrame({"dealId": deal_ids})


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
    def __init__(
        self,
        fail_epics: set[str] = frozenset(),
        open_deal_ids: set[str] | None = None,
        confirms: dict[str, dict] | None = None,
        held_sizes: dict[str, float] | None = None,
        rejected: set[str] = frozenset(),
    ):
        self._fail_epics = fail_epics
        # deal_id -> size IG holds; when given, it is what fetch_open_positions lists
        self._held_sizes = held_sizes
        # epics whose deals IG answers normally but refuses (dealStatus REJECTED)
        self._rejected = rejected
        self.close_sizes = {}
        # epic -> extra fields IG's deal confirmation carries (level, profit)
        self._confirms = confirms or {}
        # None means "can't confirm either way" (fetch_open_positions raises),
        # matching the old behavior of always retaining a leg whose close
        # failed. Tests that care about the reconciliation-against-IG path
        # pass an explicit set instead.
        self._open_deal_ids = open_deal_ids
        self.open_calls = []
        self.close_calls = []

    def open_market_position(self, epic, expiry, direction, size, currency_code):
        self.open_calls.append(epic)
        if epic in self._fail_epics:
            raise RuntimeError(f"simulated failure opening {epic}")
        if epic in self._rejected:
            return {"dealId": f"DEAL-{epic}", "dealStatus": "REJECTED", "reason": "MARKET_CLOSED_WITH_EDITS"}
        return {"dealId": f"DEAL-{epic}", **self._confirms.get(epic, {})}

    def close_market_position(self, deal_id, direction, size, epic=None):
        self.close_calls.append(epic)
        if epic in self._fail_epics:
            raise RuntimeError(f"simulated IG error closing {epic}")
        self.close_sizes[epic] = size
        if epic in self._rejected:
            return {"dealId": deal_id, "dealStatus": "REJECTED", "reason": "POSITION_NOT_AVAILABLE_TO_CLOSE"}
        return {"dealId": deal_id, **self._confirms.get(epic, {})}

    def fetch_open_positions(self):
        if self._held_sizes is not None:
            return pd.DataFrame({"dealId": list(self._held_sizes), "size": list(self._held_sizes.values())})
        if self._open_deal_ids is None:
            raise RuntimeError("simulated failure fetching open positions")
        return _positions_df(list(self._open_deal_ids))


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


def test_exit_clears_leg_when_close_fails_but_ig_shows_it_already_gone(pair_config, model, rules, mids):
    # Real incident: closing an already-closed DFB position returns an
    # unrelated-looking IG error rather than a clean "not found", so the
    # close call raises even though there's nothing left to close. Confirm
    # via fetch_open_positions instead of retrying forever.
    client = FakeClient(fail_epics={"EPIC.A"}, open_deal_ids=set())
    decision = Decision(action=Action.EXIT_STOP_LOSS, side=PairSide.FLAT, stopped_out=True, reason="test")

    result = apply_decision(client, decision, short_state(), pair_config, model, rules, mids)

    assert result == RunState(side=PairSide.FLAT, stopped_out=True)


def test_exit_retains_leg_when_close_fails_and_ig_confirms_still_open(pair_config, model, rules, mids):
    client = FakeClient(fail_epics={"EPIC.A"}, open_deal_ids={"DEAL-A", "DEAL-B"})
    decision = Decision(action=Action.EXIT_STOP_LOSS, side=PairSide.FLAT, stopped_out=True, reason="test")

    result = apply_decision(client, decision, short_state(), pair_config, model, rules, mids)

    assert result.leg_a == LegPosition(deal_id="DEAL-A", direction="SELL")
    assert result.leg_b is None


def test_enter_closes_first_leg_and_reraises_when_second_leg_fails_to_open(pair_config, model, rules, mids):
    client = FakeClient(fail_epics={"EPIC.B"})
    decision = Decision(action=Action.ENTER, side=PairSide.SHORT, stopped_out=False, reason="test")
    flat_state = RunState(side=PairSide.FLAT, stopped_out=False)

    with pytest.raises(EntryFailedError) as exc:
        apply_decision(client, decision, flat_state, pair_config, model, rules, mids)

    assert client.open_calls == ["EPIC.A", "EPIC.B"]
    assert client.close_calls == ["EPIC.A"]  # corrective close of the leg that DID open
    assert exc.value.failed_epic == "EPIC.B"
    assert exc.value.left_open is None


class FakeNotifier:
    def __init__(self):
        self.sent = []
        self.bodies = []

    def send(self, subject, body):
        self.sent.append(subject)
        self.bodies.append(body)


def test_enter_emails_once_when_both_legs_open(pair_config, model, rules, mids):
    notifier = FakeNotifier()
    decision = Decision(action=Action.ENTER, side=PairSide.SHORT, stopped_out=False, reason="test")
    flat_state = RunState(side=PairSide.FLAT, stopped_out=False)

    apply_decision(FakeClient(), decision, flat_state, pair_config, model, rules, mids, notifier)

    assert notifier.sent == ["Opened SHORT pair at +0.00 std"]


def test_enter_does_not_email_when_second_leg_fails_to_open(pair_config, model, rules, mids):
    notifier = FakeNotifier()
    decision = Decision(action=Action.ENTER, side=PairSide.SHORT, stopped_out=False, reason="test")
    flat_state = RunState(side=PairSide.FLAT, stopped_out=False)

    with pytest.raises(EntryFailedError):
        apply_decision(FakeClient(fail_epics={"EPIC.B"}), decision, flat_state, pair_config, model, rules, mids, notifier)

    assert notifier.sent == []


def test_exit_emails_once_when_both_legs_close(pair_config, model, rules, mids):
    notifier = FakeNotifier()
    decision = Decision(action=Action.EXIT_STOP_LOSS, side=PairSide.FLAT, stopped_out=True, reason="test")

    apply_decision(FakeClient(), decision, short_state(), pair_config, model, rules, mids, notifier)

    assert notifier.sent == ["Closed SHORT pair (stop loss)"]


def test_exit_does_not_email_until_the_failed_leg_closes(pair_config, model, rules, mids):
    notifier = FakeNotifier()
    decision = Decision(action=Action.EXIT_TAKE_PROFIT, side=PairSide.FLAT, stopped_out=False, reason="test")

    partial = apply_decision(
        FakeClient(fail_epics={"EPIC.A"}), decision, short_state(), pair_config, model, rules, mids, notifier
    )
    assert notifier.sent == []

    apply_decision(FakeClient(), decision, partial, pair_config, model, rules, mids, notifier)
    assert notifier.sent == ["Closed SHORT pair (take profit)"]


def test_enter_records_fills_and_entry_details_for_the_close_email(pair_config, model, rules, mids):
    client = FakeClient(confirms={"EPIC.A": {"level": 100.4}, "EPIC.B": {"level": 49.9}})
    decision = Decision(action=Action.ENTER, side=PairSide.SHORT, stopped_out=False, reason="test")

    state = apply_decision(client, decision, RunState(side=PairSide.FLAT, stopped_out=False), pair_config, model, rules, mids)

    assert state.leg_a == LegPosition(deal_id="DEAL-EPIC.A", direction="SELL", size=10.0, open_level=100.4)
    assert state.leg_b == LegPosition(deal_id="DEAL-EPIC.B", direction="BUY", size=20.0, open_level=49.9)
    assert state.opened_at is not None
    assert state.entry_spread == pytest.approx(0.5 * 100.0 - 50.0)
    assert state.entry_std_spread == model.std_spread


def test_enter_email_carries_levels_and_thresholds(pair_config, model, rules, mids):
    notifier = FakeNotifier()
    client = FakeClient(confirms={"EPIC.A": {"level": 100.4}, "EPIC.B": {"level": 49.9}})
    decision = Decision(action=Action.ENTER, side=PairSide.SHORT, stopped_out=False, reason="spread above entry threshold")

    apply_decision(client, decision, RunState(side=PairSide.FLAT, stopped_out=False), pair_config, model, rules, mids, notifier)

    body = notifier.bodies[0]
    assert "filled at 100.4" in body and "filled at 49.9" in body
    assert "Take profit: when the spread falls below +1.00 (+1.00 std)" in body
    assert "Stop loss: when the spread rises above +3.00 (+3.00 std)" in body
    assert "Entry band: ±1.50 (1.50 std)" in body


def test_exit_email_reports_igs_realised_pnl(pair_config, model, rules, mids):
    notifier = FakeNotifier()
    client = FakeClient(confirms={"EPIC.A": {"level": 99.0, "profit": 14.0}, "EPIC.B": {"level": 50.2, "profit": -4.5}})
    decision = Decision(action=Action.EXIT_TAKE_PROFIT, side=PairSide.FLAT, stopped_out=False, reason="test")
    state = RunState(
        side=PairSide.SHORT, stopped_out=False,
        leg_a=LegPosition(deal_id="DEAL-A", direction="SELL", size=10.0, open_level=100.4),
        leg_b=LegPosition(deal_id="DEAL-B", direction="BUY", size=20.0, open_level=49.9),
        opened_at=pd.Timestamp.now(tz="UTC") - pd.Timedelta(hours=3), entry_spread=1.6, entry_std_spread=1.0,
    )

    apply_decision(client, decision, state, pair_config, model, rules, mids, notifier)

    assert notifier.sent == ["Closed SHORT pair (take profit): +£9.50"]
    assert "Total: +£9.50 (IG's realised P&L)" in notifier.bodies[0]
    assert "At entry: +1.60" in notifier.bodies[0]


def test_a_broken_email_never_costs_the_trade_its_state(pair_config, model, rules, mids, monkeypatch):
    """The email is built after IG has dealt and before the new state is
    returned to be saved, so a formatting bug must not lose real positions."""
    from oil_pair import trade_report

    def boom(*args, **kwargs):
        raise ValueError("formatting bug")

    monkeypatch.setattr(trade_report, "opened", boom)
    notifier = FakeNotifier()
    decision = Decision(action=Action.ENTER, side=PairSide.SHORT, stopped_out=False, reason="test")

    state = apply_decision(FakeClient(), decision, RunState(side=PairSide.FLAT, stopped_out=False), pair_config, model, rules, mids, notifier)

    assert state.leg_a is not None and state.leg_b is not None
    assert notifier.sent == []


def sized_short_state():
    return RunState(
        side=PairSide.SHORT,
        stopped_out=False,
        leg_a=LegPosition(deal_id="DEAL-A", direction="SELL", size=10.0, open_level=100.0),
        leg_b=LegPosition(deal_id="DEAL-B", direction="BUY", size=20.0, open_level=50.0),
    )


def test_close_uses_the_size_ig_holds_not_one_worked_out_from_the_moved_price(pair_config, model, rules):
    """The real risk: B opened at £20/pt when it was at 50; at 52 the old code
    worked out 1000/52 = £19.23/pt and would have left £0.77/pt open untracked."""
    client = FakeClient(held_sizes={"DEAL-A": 10.0, "DEAL-B": 20.0})
    decision = Decision(action=Action.EXIT_TAKE_PROFIT, side=PairSide.FLAT, stopped_out=False, reason="test")

    result = apply_decision(client, decision, sized_short_state(), pair_config, model, rules, {"EPIC.A": 104.0, "EPIC.B": 52.0})

    assert client.close_sizes == {"EPIC.A": 10.0, "EPIC.B": 20.0}
    assert result == RunState(side=PairSide.FLAT, stopped_out=False)


def test_close_falls_back_to_the_size_recorded_at_entry_when_ig_cant_be_asked(pair_config, model, rules):
    client = FakeClient(open_deal_ids=None)  # fetch_open_positions raises
    decision = Decision(action=Action.EXIT_TAKE_PROFIT, side=PairSide.FLAT, stopped_out=False, reason="test")

    apply_decision(client, decision, sized_short_state(), pair_config, model, rules, {"EPIC.A": 104.0, "EPIC.B": 52.0})

    assert client.close_sizes == {"EPIC.A": 10.0, "EPIC.B": 20.0}


def test_a_close_still_goes_ahead_when_ig_does_not_list_the_deal(pair_config, model, rules):
    """IG's list only sizes the close; a deal missing from it is still closed
    (falling back to the recorded size), so a bad listing can't skip a real close."""
    client = FakeClient(held_sizes={})
    decision = Decision(action=Action.EXIT_TAKE_PROFIT, side=PairSide.FLAT, stopped_out=False, reason="test")

    apply_decision(client, decision, sized_short_state(), pair_config, model, rules, {"EPIC.A": 104.0, "EPIC.B": 52.0})

    assert client.close_sizes == {"EPIC.A": 10.0, "EPIC.B": 20.0}


def test_a_rejected_close_is_not_taken_as_closed(pair_config, model, rules, mids):
    notifier = FakeNotifier()
    client = FakeClient(held_sizes={"DEAL-A": 10.0, "DEAL-B": 20.0}, rejected={"EPIC.A"})
    decision = Decision(action=Action.EXIT_STOP_LOSS, side=PairSide.FLAT, stopped_out=True, reason="test")

    result = apply_decision(client, decision, sized_short_state(), pair_config, model, rules, mids, notifier)

    assert result.side is PairSide.SHORT
    assert result.leg_a == sized_short_state().leg_a  # kept, and retried next iteration
    assert result.leg_b is None
    assert notifier.sent == []


def test_a_rejected_second_leg_unwinds_the_first(pair_config, model, rules, mids):
    from oil_pair.ig_client import DealRejectedError

    client = FakeClient(rejected={"EPIC.B"})
    decision = Decision(action=Action.ENTER, side=PairSide.SHORT, stopped_out=False, reason="test")

    with pytest.raises(EntryFailedError) as exc:
        apply_decision(client, decision, RunState(side=PairSide.FLAT, stopped_out=False), pair_config, model, rules, mids)

    assert client.open_calls == ["EPIC.A", "EPIC.B"]
    assert client.close_calls == ["EPIC.A"]
    assert isinstance(exc.value.__cause__, DealRejectedError)
    assert "MARKET_CLOSED_WITH_EDITS" in exc.value.detail


def test_a_rejected_first_leg_opens_nothing_else(pair_config, model, rules, mids):
    client = FakeClient(rejected={"EPIC.A"})
    decision = Decision(action=Action.ENTER, side=PairSide.SHORT, stopped_out=False, reason="test")

    with pytest.raises(EntryFailedError):
        apply_decision(client, decision, RunState(side=PairSide.FLAT, stopped_out=False), pair_config, model, rules, mids)

    assert client.open_calls == ["EPIC.A"]
    assert client.close_calls == []


def test_enter_records_which_account_holds_the_position(pair_config, model, rules, mids):
    from types import SimpleNamespace

    client = FakeClient()
    client.credentials = SimpleNamespace(acc_type="live")
    decision = Decision(action=Action.ENTER, side=PairSide.SHORT, stopped_out=False, reason="test")

    state = apply_decision(client, decision, RunState(side=PairSide.FLAT, stopped_out=False), pair_config, model, rules, mids)

    assert state.account == "live"


# --- after a refused entry (2026-10-08: ten Brent refusals in 2.5 minutes) ----


def test_the_refused_leg_goes_first_so_a_repeat_refusal_opens_nothing(pair_config, model, rules, mids):
    client = FakeClient(rejected={"EPIC.B"})
    decision = Decision(action=Action.ENTER, side=PairSide.SHORT, stopped_out=False, reason="test")

    with pytest.raises(EntryFailedError):
        apply_decision(client, decision, RunState(side=PairSide.FLAT, stopped_out=False), pair_config, model, rules,
                       mids, open_first="EPIC.B")

    assert client.open_calls == ["EPIC.B"]
    assert client.close_calls == []


def test_opening_in_reverse_order_still_records_each_leg_against_its_instrument(pair_config, model, rules, mids):
    client = FakeClient(confirms={"EPIC.A": {"level": 100.4}, "EPIC.B": {"level": 49.9}})
    decision = Decision(action=Action.ENTER, side=PairSide.SHORT, stopped_out=False, reason="test")

    state = apply_decision(client, decision, RunState(side=PairSide.FLAT, stopped_out=False), pair_config, model,
                           rules, mids, open_first="EPIC.B")

    assert client.open_calls == ["EPIC.B", "EPIC.A"]
    assert state.leg_a == LegPosition(deal_id="DEAL-EPIC.A", direction="SELL", size=10.0, open_level=100.4)
    assert state.leg_b == LegPosition(deal_id="DEAL-EPIC.B", direction="BUY", size=20.0, open_level=49.9)


def test_a_first_leg_ig_wont_close_again_is_reported_as_left_open(pair_config, model, rules, mids):
    class Client(FakeClient):
        def close_market_position(self, deal_id, direction, size, epic=None):
            self.close_calls.append(epic)
            return {"dealId": deal_id, "dealStatus": "REJECTED", "reason": "MARKET_CLOSED_WITH_EDITS"}

    client = Client(rejected={"EPIC.B"})
    decision = Decision(action=Action.ENTER, side=PairSide.SHORT, stopped_out=False, reason="test")

    with pytest.raises(EntryFailedError) as exc:
        apply_decision(client, decision, RunState(side=PairSide.FLAT, stopped_out=False), pair_config, model, rules, mids)

    assert exc.value.left_open == "DEAL-EPIC.A"


def test_entries_pause_for_30_minutes_doubling_to_8_hours_and_reset_on_success():
    from oil_pair.main import EntryGuard

    guard = EntryGuard()
    now = pd.Timestamp("2026-10-08T06:10:00Z")
    assert not guard.paused(now)

    waits = []
    for _ in range(6):
        until = guard.record_failure("EPIC.B", now)
        waits.append(until - now)
        assert guard.paused(until - pd.Timedelta(seconds=1)) and not guard.paused(until)

    assert waits == [pd.Timedelta(minutes=m) for m in (30, 60, 120, 240, 480, 480)]
    assert guard.open_first == "EPIC.B"

    guard.record_success()
    assert not guard.paused(now) and guard.record_failure("EPIC.B", now) - now == pd.Timedelta(minutes=30)
    assert guard.open_first == "EPIC.B"  # still opened first: harmless, and it was the one refused


def test_the_entry_failure_email_says_what_was_tried_what_is_open_and_when_it_retries(pair_config):
    from oil_pair.main import _entry_failure_email

    exc = EntryFailedError(
        "EPIC.B", {"EPIC.A": ("BUY", 0.06), "EPIC.B": ("SELL", 0.2)},
        "IG rejected the open of EPIC.B: dealStatus=REJECTED reason=MINIMUM_ORDER_SIZE_ERROR",
    )
    retry_at = pd.Timestamp("2026-10-08T06:40:00Z")

    subject, body = _entry_failure_email(exc, PairSide.LONG, pair_config, ["EPIC.B: minDealSize 0.04"], retry_at, 1)

    assert subject == "Entry refused: B leg"
    assert "MINIMUM_ORDER_SIZE_ERROR" in body
    assert "Tried: SELL £0.20/pt B (EPIC.B)" in body
    assert "Nothing is left open" in body
    assert "Thu 08 Oct 07:40 UK" in body
    assert "EPIC.B: minDealSize 0.04" in body

    exc.left_open = "DEAL-A"
    subject, body = _entry_failure_email(exc, PairSide.LONG, pair_config, [], retry_at, 1)
    assert subject.startswith("ACTION NEEDED")
    assert "OPEN AND UNTRACKED (deal DEAL-A)" in body


def test_market_terms_never_raise():
    from oil_pair.main import _market_terms

    class Client:
        def fetch_market(self, epic):
            if epic == "BAD":
                raise RuntimeError("timeout")
            return {"dealingRules": {"minDealSize": {"value": 0.5}}, "snapshot": {"marketStatus": "TRADEABLE"}}

    lines = _market_terms(Client(), ["GOOD", "BAD"])

    assert lines[0].startswith("GOOD: minDealSize 0.5, status TRADEABLE")
    assert lines[1] == "BAD: could not read IG's market details (timeout)"
