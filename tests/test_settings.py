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
