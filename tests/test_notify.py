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
    assert parse_qs(urlparse(urls[0]).query)["text"][0] == \
        "*[oil-pair arabica_robusta] Opened LONG pair at -2.01 std*\n\u200b\n\u200bbody"


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


# --- long WhatsApp messages go as numbered parts (2026-10-12: digest cut off) ---

from oil_pair.notify import WHATSAPP_MAX_BYTES, split_for_whatsapp  # noqa: E402


def test_a_short_message_is_sent_whole():
    assert split_for_whatsapp("Opened LONG pair\nspread -2.01 std") == ["Opened LONG pair\nspread -2.01 std"]


def test_a_long_message_splits_at_line_breaks_into_numbered_parts():
    lines = [f"wti_heating_oil: WATCH - line {i:02d} " + "x" * 80 for i in range(30)]
    parts = split_for_whatsapp("\n".join(lines))

    assert len(parts) > 1
    assert all(len(p.encode("utf-8")) <= WHATSAPP_MAX_BYTES for p in parts)
    assert parts[0].startswith(f"(1/{len(parts)}) ") and parts[-1].startswith(f"({len(parts)}/{len(parts)}) ")
    rejoined = "\n".join(p.split(") ", 1)[1] for p in parts)
    assert rejoined == "\n".join(lines)  # nothing lost, no line broken


def test_one_over_long_line_splits_between_words():
    words = " ".join(f"word{i}" for i in range(400))
    parts = split_for_whatsapp(words)

    assert len(parts) > 1
    bodies = [p.split(") ", 1)[1] for p in parts]
    assert " ".join(bodies) == words
    assert all(b.split(" ")[-1].startswith("word") for b in bodies)  # no word cut in half


def test_multi_byte_characters_count_in_bytes_not_letters():
    text = "\n".join("£165 × 2 − 3 — “quoted” " * 6 for _ in range(12))
    assert all(len(p.encode("utf-8")) <= WHATSAPP_MAX_BYTES for p in split_for_whatsapp(text))


def test_parts_are_sent_in_order_with_a_gap(monkeypatch):
    sent, slept = [], []
    monkeypatch.setattr(notify.requests, "get", lambda url, timeout: sent.append(url) or FakeResponse())
    monkeypatch.setattr(notify.time, "sleep", lambda s: slept.append(s))
    body = "\n".join(f"line {i} " + "y" * 90 for i in range(30))

    Notifier("news", None, (PAUL,))._deliver_all("Daily news", body)

    texts = [parse_qs(urlparse(u).query)["text"][0] for u in sent]
    assert len(texts) > 1 and texts[0].startswith(f"(1/{len(texts)}) *[oil-pair news] Daily news*")
    assert [t.split(")", 1)[0] for t in texts] == [f"({i}" + f"/{len(texts)}" for i in range(1, len(texts) + 1)]
    assert slept == [notify.WHATSAPP_GAP_SECONDS] * (len(texts) - 1)


def test_a_failed_part_stops_the_rest(monkeypatch, caplog):
    calls = []

    def get(url, timeout):
        calls.append(url)
        return FakeResponse(203, "APIKey is invalid. Please create a new one.") if len(calls) == 2 else FakeResponse()

    monkeypatch.setattr(notify.requests, "get", get)
    monkeypatch.setattr(notify.time, "sleep", lambda s: None)
    body = "\n".join(f"line {i} " + "y" * 90 for i in range(30))

    Notifier("news", None, (PAUL,))._deliver_all("Daily news", body)

    assert len(calls) == 2
    assert "part 2/" in caplog.text and "APIKey is invalid" in caplog.text


def test_the_log_keeps_callmebots_verdict_even_when_the_message_says_note(monkeypatch, caplog):
    echo = "Message to: +447700900123 Text to send: FLAGS: 1%0ANote: Brent volatile. Message queued. You will receive it in a few seconds."
    monkeypatch.setattr(notify.requests, "get", lambda url, timeout: FakeResponse(200, echo))

    with caplog.at_level("INFO", logger="oil_pair.notify"):
        Notifier("news", None, (PAUL,))._deliver_all("Daily news", "FLAGS: 1\nNote: Brent volatile.")

    assert "(Message queued.)" in caplog.text
    assert "Brent volatile" not in caplog.text


def test_no_whatsapp_line_starts_with_a_bare_word_callmebot_could_block(monkeypatch):
    """2026-10-13: CallMeBot's server answered 403 to a report with a line
    starting "Net P&L:" - read as a Windows `net` command after a line break."""
    urls = []
    monkeypatch.setattr(notify.requests, "get", lambda url, timeout: urls.append(url) or FakeResponse())

    Notifier("status", None, (PAUL,))._deliver_all("Status", "Gross P&L: -£58.08\nNet P&L: -£57.25")

    text = parse_qs(urlparse(urls[0]).query)["text"][0]
    assert "\nNet" not in text and "\n\u200bNet P&L: -£57.25" in text
