"""load_credentials() must default to IG's demo API and only ever read live
credentials when a caller explicitly asks for them - see main.py's --live
flag. The two credential namespaces (IG_DEMO_* / IG_LIVE_*) must stay fully
isolated from each other."""

import pytest

from oil_pair import settings
from oil_pair.settings import load_credentials

DEMO_VARS = {
    "IG_DEMO_USERNAME": "demo-user",
    "IG_DEMO_PASSWORD": "demo-pass",
    "IG_DEMO_API_KEY": "demo-key",
    "IG_DEMO_ACC_NUMBER": "demo-acc",
}

LIVE_VARS = {
    "IG_LIVE_USERNAME": "live-user",
    "IG_LIVE_PASSWORD": "live-pass",
    "IG_LIVE_API_KEY": "live-key",
    "IG_LIVE_ACC_NUMBER": "live-acc",
}


@pytest.fixture(autouse=True)
def clean_ig_env(monkeypatch):
    # The real .env (gitignored, filled in with actual demo credentials) must
    # not leak into these tests - they exercise load_credentials()'s env-var
    # selection logic, not the developer machine's own dotenv file.
    monkeypatch.setattr(settings, "load_dotenv", lambda *args, **kwargs: None)
    for name in (*DEMO_VARS, *LIVE_VARS):
        monkeypatch.delenv(name, raising=False)


def test_defaults_to_demo_credentials(monkeypatch):
    for name, value in DEMO_VARS.items():
        monkeypatch.setenv(name, value)

    creds = load_credentials()

    assert creds.acc_type == "demo"
    assert creds.username == "demo-user"
    assert creds.acc_number == "demo-acc"


def test_live_flag_reads_live_namespace(monkeypatch):
    for name, value in {**DEMO_VARS, **LIVE_VARS}.items():
        monkeypatch.setenv(name, value)

    creds = load_credentials(live=True)

    assert creds.acc_type == "live"
    assert creds.username == "live-user"
    assert creds.acc_number == "live-acc"


def test_live_flag_does_not_fall_back_to_demo_vars(monkeypatch):
    # Only demo vars are set - asking for live must fail loudly rather than
    # silently trading demo (or, worse, treating demo creds as live ones).
    for name, value in DEMO_VARS.items():
        monkeypatch.setenv(name, value)

    with pytest.raises(RuntimeError, match="IG_LIVE_USERNAME"):
        load_credentials(live=True)


def test_missing_demo_vars_raises_with_actionable_message(monkeypatch):
    with pytest.raises(RuntimeError, match="IG_DEMO_USERNAME"):
        load_credentials()


def test_missing_live_vars_raises_with_actionable_message(monkeypatch):
    with pytest.raises(RuntimeError, match="IG_LIVE_USERNAME"):
        load_credentials(live=True)


# --- central risk (config/risk.toml) -----------------------------------------


def test_no_risk_file_means_fixed_notionals(tmp_path):
    from oil_pair.settings import load_risk_config

    assert load_risk_config(tmp_path / "risk.toml") is None


def test_risk_file_loads(tmp_path):
    from oil_pair.settings import RiskConfig, load_risk_config

    path = tmp_path / "risk.toml"
    path.write_text("max_loss_per_trade = 250\nmax_notional_per_leg = 10000\n")

    assert load_risk_config(path) == RiskConfig(max_loss_per_trade=250, max_notional_per_leg=10000)


@pytest.mark.parametrize("body", [
    "max_loss_per_trade = -5\n",
    "max_loss_per_trade = 0\n",
    "max_loss_per_trade = '250'\n",
    "max_notional_per_leg = 10000\n",  # no max_loss_per_trade
    "max_loss_per_trade = 250\nmax_los_per_trade = 1\n",  # typo'd extra key
])
def test_a_bad_risk_file_is_refused_not_guessed(tmp_path, body):
    from oil_pair.settings import load_risk_config

    path = tmp_path / "risk.toml"
    path.write_text(body)

    with pytest.raises(RuntimeError):
        load_risk_config(path)


def test_the_shipped_risk_file_is_valid():
    from oil_pair.settings import load_risk_config

    risk = load_risk_config()
    assert risk is not None and risk.max_loss_per_trade > 0 and risk.max_notional_per_leg > 0


def test_a_pair_can_override_the_central_risk(tmp_path):
    from oil_pair.settings import load_pair_config

    path = tmp_path / "pair_config.toml"
    path.write_text(
        '[instrument_a]\nepic = "A"\nname = "A"\nexpiry = "DFB"\ncurrency_code = "GBP"\n'
        '[instrument_b]\nepic = "B"\nname = "B"\nexpiry = "DFB"\ncurrency_code = "GBP"\n'
        "[strategy]\nmax_loss_per_trade = 100\n"
    )

    assert load_pair_config(path).strategy.max_loss_per_trade == 100
