import pytest

from oil_pair.state_store import LegPosition, RunState, StateCorruptedError, load_state, save_state
from oil_pair.strategy_logic import PairSide


def test_load_state_returns_none_when_no_file_exists(tmp_path):
    assert load_state(tmp_path / "nonexistent" / "run_state.json") is None


def test_save_then_load_roundtrips_flat_state(tmp_path):
    path = tmp_path / "run_state.json"
    save_state(RunState(side=PairSide.FLAT, stopped_out=False), path)

    loaded = load_state(path)

    assert loaded == RunState(side=PairSide.FLAT, stopped_out=False)


def test_save_then_load_roundtrips_open_pair_position(tmp_path):
    path = tmp_path / "run_state.json"
    state = RunState(
        side=PairSide.SHORT,
        stopped_out=False,
        leg_a=LegPosition(deal_id="DEAL-A", direction="SELL"),
        leg_b=LegPosition(deal_id="DEAL-B", direction="BUY"),
    )
    save_state(state, path)

    loaded = load_state(path)

    assert loaded == state


def test_save_overwrites_previous_state(tmp_path):
    path = tmp_path / "run_state.json"
    save_state(RunState(side=PairSide.LONG, stopped_out=True), path)
    save_state(RunState(side=PairSide.FLAT, stopped_out=False), path)

    assert load_state(path) == RunState(side=PairSide.FLAT, stopped_out=False)


def test_load_state_raises_clear_error_on_empty_object(tmp_path):
    # This is the exact corruption seen in a real incident: two concurrent
    # processes writing the same tmp file path interleaved their writes and
    # left behind a bare "{}". Must not surface as a raw KeyError.
    path = tmp_path / "run_state.json"
    path.write_text("{}")

    with pytest.raises(StateCorruptedError):
        load_state(path)


def test_load_state_raises_clear_error_on_invalid_json(tmp_path):
    path = tmp_path / "run_state.json"
    path.write_text("{not valid json")

    with pytest.raises(StateCorruptedError):
        load_state(path)


def test_load_state_raises_clear_error_on_invalid_side_value(tmp_path):
    path = tmp_path / "run_state.json"
    path.write_text('{"side": "SIDEWAYS", "stopped_out": false}')

    with pytest.raises(StateCorruptedError):
        load_state(path)


def test_concurrent_saves_from_different_pids_do_not_corrupt_each_other(tmp_path, monkeypatch):
    # Regression test for the real incident: save_state used to write to a
    # single shared `run_state.tmp` regardless of caller, so two processes
    # racing there could interleave/truncate each other's write. Simulating
    # two different pids here confirms each gets its own tmp path.
    import oil_pair.state_store as state_store

    path = tmp_path / "run_state.json"

    monkeypatch.setattr(state_store.os, "getpid", lambda: 111)
    save_state(RunState(side=PairSide.LONG, stopped_out=False), path)
    assert not (tmp_path / "run_state.111.tmp").exists()  # renamed away already

    monkeypatch.setattr(state_store.os, "getpid", lambda: 222)
    save_state(RunState(side=PairSide.SHORT, stopped_out=True), path)

    assert load_state(path) == RunState(side=PairSide.SHORT, stopped_out=True)
