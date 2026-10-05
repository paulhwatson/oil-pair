"""scripts/close_positions.py must refuse to run on the wrong account before
it touches IG: there, every tracked deal looks already closed, and the
script would reset local state to FLAT with the real position still open."""

import importlib.util
from pathlib import Path

import pytest

from oil_pair.paths import PairPaths
from oil_pair.state_store import LegPosition, RunState, load_state, save_state
from oil_pair.strategy_logic import PairSide

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "close_positions.py"


@pytest.fixture
def script():
    spec = importlib.util.spec_from_file_location("close_positions", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def paths(tmp_path):
    return PairPaths(
        pair_name="test_pair",
        pair_config=tmp_path / "pair_config.toml",
        state=tmp_path / "run_state.json",
        instance_lock=tmp_path / "instance.lock",
        price_log=tmp_path / "ticks.csv",
        log_dir=tmp_path / "logs",
    )


def _open_pair(account):
    return RunState(
        side=PairSide.LONG, stopped_out=False,
        leg_a=LegPosition(deal_id="DEAL-A", direction="BUY"),
        leg_b=LegPosition(deal_id="DEAL-B", direction="SELL"),
        account=account,
    )


@pytest.mark.parametrize("account, live", [("live", False), ("demo", True)])
def test_refuses_the_other_account_without_logging_in(script, paths, monkeypatch, capsys, account, live):
    save_state(_open_pair(account), paths.state)

    def no_ig(*args, **kwargs):
        raise AssertionError("must refuse before touching IG")

    monkeypatch.setattr(script, "load_credentials", no_ig)
    monkeypatch.setattr(script, "IGClient", no_ig)

    with pytest.raises(SystemExit) as exc:
        script._close_tracked_positions(paths, live=live)

    assert exc.value.code == 1
    assert "Refusing to run" in capsys.readouterr().out
    assert load_state(paths.state) == _open_pair(account)  # untouched


def test_live_flag_logs_in_with_the_live_credentials(script, paths, monkeypatch):
    save_state(_open_pair("live"), paths.state)
    asked = []

    def credentials(live=False):
        asked.append(live)
        raise RuntimeError("stop here")

    monkeypatch.setattr(script, "load_pair_config", lambda _: None)
    monkeypatch.setattr(script, "load_credentials", credentials)

    with pytest.raises(RuntimeError, match="stop here"):
        script._close_tracked_positions(paths, live=True)

    assert asked == [True]
