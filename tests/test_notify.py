import smtplib
import threading

import pytest

from oil_pair import notify
from oil_pair.notify import EmailConfig, Notifier, load_email_config

_ENV_VARS = (
    "SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME", "SMTP_PASSWORD",
    "NOTIFY_EMAIL_TO", "NOTIFY_EMAIL_FROM", "NOTIFY_EMAIL_CC", "NOTIFY_WHATSAPP",
)


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
    assert config.cc == ()


def test_cc_is_a_comma_separated_list(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setenv("NOTIFY_EMAIL_CC", " one@example.com, ,two@example.com ")

    assert load_email_config().cc == ("one@example.com", "two@example.com")


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
    assert FakeSMTP.sent[0]["Cc"] is None


def test_send_adds_cc_header_when_configured(monkeypatch):
    FakeSMTP.sent = []
    monkeypatch.setattr(notify.smtplib, "SMTP", FakeSMTP)
    config = EmailConfig(
        host="smtp.example.com", port=587, username="me@example.com", password="secret",
        sender="me@example.com", recipient="alerts@example.com",
        cc=("one@example.com", "two@example.com"),
    )

    Notifier("brent_wti", config)._deliver("Opened LONG pair", "body")

    assert FakeSMTP.sent[0]["Cc"] == "one@example.com, two@example.com"


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


# --- WhatsApp via CallMeBot -----------------------------------------------

from urllib.parse import parse_qs, urlparse  # noqa: E402

from oil_pair.notify import WhatsAppRecipient, load_whatsapp_recipients, whatsapp_url  # noqa: E402

PAUL = WhatsAppRecipient(phone="+447700900123", apikey="111111")
JAKE = WhatsAppRecipient(phone="+447700900456", apikey="222222")


def test_whatsapp_is_off_when_unset():
    assert load_whatsapp_recipients() == ()


def test_whatsapp_recipients_are_phone_key_pairs(monkeypatch):
    monkeypatch.setenv("NOTIFY_WHATSAPP", " +44 7700 900123:111111 , +447700-900456:222222 ")

    assert load_whatsapp_recipients() == (PAUL, JAKE)


def test_a_malformed_recipient_is_skipped_without_logging_its_key(monkeypatch, caplog):
    monkeypatch.setenv("NOTIFY_WHATSAPP", "07700900123:SECRETKEY,+447700900456:222222,+447700900789:")

    assert load_whatsapp_recipients() == (JAKE,)
    assert "SECRETKEY" not in caplog.text
    assert "recipient 1" in caplog.text and "recipient 3" in caplog.text


def test_the_url_encodes_spaces_and_new_lines_as_callmebot_documents():
    url = whatsapp_url(PAUL, "Opened LONG pair\nspread -1.26 std, £0.19/pt")

    assert "%20" in url and "%0A" in url and "+" not in url.split("?", 1)[1]
    query = parse_qs(urlparse(url).query)
    assert query == {"phone": ["+447700900123"], "text": ["Opened LONG pair\nspread -1.26 std, £0.19/pt"],
                     "apikey": ["111111"]}


class FakeResponse:
    def __init__(self, status_code=200, text="<p>Message queued. You will receive it in a few seconds.</p>"):
        self.status_code = status_code
        self.text = text


def test_every_recipient_gets_the_message_with_the_pair_in_bold(monkeypatch):
    urls = []
    monkeypatch.setattr(notify.requests, "get", lambda url, timeout: urls.append(url) or FakeResponse())

    Notifier("arabica_robusta", None, (PAUL, JAKE))._deliver_all("Opened LONG pair at -2.01 std", "body")

    assert [parse_qs(urlparse(u).query)["phone"][0] for u in urls] == [PAUL.phone, JAKE.phone]
    assert parse_qs(urlparse(urls[0]).query)["text"][0] == "*[oil-pair arabica_robusta] Opened LONG pair at -2.01 std*\n\nbody"


def test_email_still_goes_when_whatsapp_is_configured_too(monkeypatch):
    FakeSMTP.sent = []
    monkeypatch.setattr(notify.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(notify.requests, "get", lambda url, timeout: FakeResponse())

    Notifier("brent_wti", _config(), (PAUL,))._deliver_all("Opened LONG pair", "body")

    assert len(FakeSMTP.sent) == 1


def test_a_failed_whatsapp_never_raises_or_logs_the_key(monkeypatch, caplog):
    def boom(url, timeout):
        # requests' own errors quote the URL - which carries the key
        raise notify.requests.ConnectionError(f"Max retries exceeded with url: {url}")

    monkeypatch.setattr(notify.requests, "get", boom)

    Notifier("brent_wti", None, (PAUL,))._deliver_all("Opened LONG pair", "body")  # must not raise

    assert "failed to send WhatsApp" in caplog.text
    assert PAUL.apikey not in caplog.text


def test_an_error_reply_is_logged_with_its_verdict_but_not_the_key_or_number(monkeypatch, caplog):
    """CallMeBot answers a bad key with HTTP 203 and a reply that echoes the
    full phone number and the whole message before its verdict (2026-10-09)."""
    reply = (f"<p>Message to: {PAUL.phone}</p><p>Text to send: *[oil-pair brent_wti] Opened LONG pair*%0A%0A"
             f"{'body ' * 60}</p><p>APIKey is invalid. Please create a new one or contact support if you lost it.</p>")
    monkeypatch.setattr(notify.requests, "get", lambda url, timeout: FakeResponse(203, reply))

    Notifier("brent_wti", None, (PAUL,))._deliver_all("Opened LONG pair", "body")

    assert "HTTP 203" in caplog.text and "APIKey is invalid" in caplog.text
    assert PAUL.apikey not in caplog.text
    assert PAUL.phone not in caplog.text and PAUL.phone.lstrip("+") not in caplog.text
    assert "…123" in caplog.text


def test_whatsapp_alone_is_enough_to_send(monkeypatch):
    sent = threading.Event()
    monkeypatch.setattr(notify.requests, "get", lambda url, timeout: sent.set() or FakeResponse())

    Notifier("brent_wti", None, (PAUL,)).send("Opened LONG pair", "body")

    assert sent.wait(timeout=5)
