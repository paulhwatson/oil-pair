"""load_credentials() must default to IG's demo API and only ever read live
credentials when a caller explicitly asks for them - see main.py's --live
flag. The two credential namespaces (IG_DEMO_* / IG_LIVE_*) must stay fully
isolated from each other."""

import pytest

from oil_pair import settings
from oil_pair.settings import load_credentials, load_email_config

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

EMAIL_VARS = ("ICLOUD_SMTP_USERNAME", "ICLOUD_SMTP_APP_PASSWORD", "TRADE_NOTIFY_TO")


@pytest.fixture(autouse=True)
def clean_ig_env(monkeypatch):
    # The real .env (gitignored, filled in with actual demo credentials) must
    # not leak into these tests - they exercise load_credentials()'s env-var
    # selection logic, not the developer machine's own dotenv file.
    monkeypatch.setattr(settings, "load_dotenv", lambda *args, **kwargs: None)
    for name in (*DEMO_VARS, *LIVE_VARS, *EMAIL_VARS):
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


def test_email_config_absent_when_unset(monkeypatch):
    # Trade-close notifications are optional - must not raise, must not
    # block startup, just come back as "disabled".
    assert load_email_config() is None


def test_email_config_absent_when_only_partially_set(monkeypatch):
    monkeypatch.setenv("ICLOUD_SMTP_USERNAME", "me@icloud.com")

    assert load_email_config() is None


def test_email_config_defaults_recipient_to_sender(monkeypatch):
    monkeypatch.setenv("ICLOUD_SMTP_USERNAME", "me@icloud.com")
    monkeypatch.setenv("ICLOUD_SMTP_APP_PASSWORD", "app-pw")

    config = load_email_config()

    assert config.smtp_username == "me@icloud.com"
    assert config.smtp_app_password == "app-pw"
    assert config.to_address == "me@icloud.com"


def test_email_config_uses_explicit_recipient_when_set(monkeypatch):
    monkeypatch.setenv("ICLOUD_SMTP_USERNAME", "me@icloud.com")
    monkeypatch.setenv("ICLOUD_SMTP_APP_PASSWORD", "app-pw")
    monkeypatch.setenv("TRADE_NOTIFY_TO", "phone@icloud.com")

    config = load_email_config()

    assert config.to_address == "phone@icloud.com"
