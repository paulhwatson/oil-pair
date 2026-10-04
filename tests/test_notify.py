import smtplib
import threading

import pytest

from oil_pair import notify
from oil_pair.notify import EmailConfig, Notifier, load_email_config

_ENV_VARS = ("SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME", "SMTP_PASSWORD", "NOTIFY_EMAIL_TO", "NOTIFY_EMAIL_FROM")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def _configure(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_USERNAME", "me@example.com")
    monkeypatch.setenv("SMTP_PASSWORD", "secret")
    monkeypatch.setenv("NOTIFY_EMAIL_TO", "alerts@example.com")


def test_unconfigured_returns_none():
    assert load_email_config() is None


def test_partly_configured_returns_none(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    assert load_email_config() is None


def test_configured_defaults_port_and_sender(monkeypatch):
    _configure(monkeypatch)
    config = load_email_config()
    assert config.port == 587
    assert config.sender == "me@example.com"


class FakeSMTP:
    sent = []

    def __init__(self, host, port, timeout):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        pass

    def login(self, username, password):
        pass

    def send_message(self, message):
        FakeSMTP.sent.append(message)


def _config():
    return EmailConfig(
        host="smtp.example.com", port=587, username="me@example.com", password="secret",
        sender="me@example.com", recipient="alerts@example.com",
    )


def test_send_tags_subject_with_pair_name(monkeypatch):
    FakeSMTP.sent = []
    monkeypatch.setattr(notify.smtplib, "SMTP", FakeSMTP)

    Notifier("brent_wti", _config())._deliver("Opened LONG pair", "body")

    assert [m["Subject"] for m in FakeSMTP.sent] == ["[oil-pair brent_wti] Opened LONG pair"]
    assert FakeSMTP.sent[0]["To"] == "alerts@example.com"


def test_send_is_a_no_op_when_unconfigured(monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("should not connect")

    monkeypatch.setattr(notify.smtplib, "SMTP", explode)
    Notifier("brent_wti", None).send("Opened LONG pair", "body")


def test_send_failure_is_swallowed(monkeypatch):
    def refuse(*args, **kwargs):
        raise smtplib.SMTPConnectError(421, "nope")

    monkeypatch.setattr(notify.smtplib, "SMTP", refuse)
    Notifier("brent_wti", _config())._deliver("Opened LONG pair", "body")


def test_send_does_not_wait_for_delivery(monkeypatch):
    release = threading.Event()
    delivered = threading.Event()

    def slow_deliver(self, subject, body):
        release.wait(timeout=5)
        delivered.set()

    monkeypatch.setattr(Notifier, "_deliver", slow_deliver)
    Notifier("brent_wti", _config()).send("Opened LONG pair", "body")

    assert not delivered.is_set()  # send returned while delivery is still blocked
    release.set()
    assert delivered.wait(timeout=5)
