"""Notifications for pair opens and closes: email, and WhatsApp via CallMeBot.

Configured from .env (see .env.example). Each channel is optional and
independent; with neither configured this does nothing. A send that fails is
logged and dropped: a notification is never worth interrupting the trading
loop for, least of all midway through closing a pair. Sends run on a
background thread for the same reason - the caller still has to save state
for the legs it just opened or closed, and a slow SMTP server or HTTP API
must not stand between the trade and that save.

WhatsApp goes through CallMeBot's free personal API
(https://www.callmebot.com/blog/free-api-whatsapp-messages/). Its key only
ever messages the phone that registered it, so each recipient registers once
from their own phone and is configured as their own phone:key pair. It is
unofficial and best-effort - which is why email stays on as the record.

Run `python -m oil_pair.notify` to send a test message on every configured
channel and wait for the result.
"""

from __future__ import annotations

import logging
import os
import re
import smtplib
import threading
import urllib.parse
from dataclasses import dataclass
from email.message import EmailMessage

import requests

log = logging.getLogger(__name__)

SMTP_TIMEOUT_SECONDS = 15
WHATSAPP_URL = "https://api.callmebot.com/whatsapp.php"
WHATSAPP_TIMEOUT_SECONDS = 20
WHATSAPP_ENV_VAR = "NOTIFY_WHATSAPP"

_REQUIRED_ENV_VARS = ("SMTP_HOST", "SMTP_USERNAME", "SMTP_PASSWORD", "NOTIFY_EMAIL_TO")


@dataclass(frozen=True)
class EmailConfig:
    host: str
    port: int
    username: str
    password: str
    sender: str
    recipient: str
    cc: tuple[str, ...] = ()


def _parse_addresses(raw: str | None) -> tuple[str, ...]:
    return tuple(a.strip() for a in (raw or "").split(",") if a.strip())


def load_email_config() -> EmailConfig | None:
    """None when notifications aren't configured. Call after .env is loaded
    (settings.load_credentials does that)."""
    missing = [name for name in _REQUIRED_ENV_VARS if not os.environ.get(name)]
    if len(missing) == len(_REQUIRED_ENV_VARS):
        log.info("email notifications off - no SMTP settings in .env")
        return None
    if missing:
        log.warning("email notifications off - missing .env variables: %s", ", ".join(missing))
        return None
    return EmailConfig(
        host=os.environ["SMTP_HOST"],
        port=int(os.environ.get("SMTP_PORT", "587")),
        username=os.environ["SMTP_USERNAME"],
        password=os.environ["SMTP_PASSWORD"],
        sender=os.environ.get("NOTIFY_EMAIL_FROM") or os.environ["SMTP_USERNAME"],
        recipient=os.environ["NOTIFY_EMAIL_TO"],
        cc=_parse_addresses(os.environ.get("NOTIFY_EMAIL_CC")),
    )


@dataclass(frozen=True)
class WhatsAppRecipient:
    phone: str  # international format, e.g. +447700900123
    apikey: str

    @property
    def masked(self) -> str:
        """For logs: enough to tell recipients apart, not the whole number."""
        return f"…{self.phone[-3:]}"


def load_whatsapp_recipients() -> tuple[WhatsAppRecipient, ...]:
    """Recipients from NOTIFY_WHATSAPP, a comma-separated list of phone:apikey
    pairs (e.g. "+447700900123:123456,+447700900456:654321"). Empty when
    unset. A malformed entry is skipped with a warning that names its
    position, never its contents - it holds a key."""
    raw = os.environ.get(WHATSAPP_ENV_VAR, "")
    recipients = []
    for position, entry in enumerate((e.strip() for e in raw.split(",")), start=1):
        if not entry:
            continue
        phone, _, apikey = entry.rpartition(":")
        phone = re.sub(r"[\s-]", "", phone)
        if not re.fullmatch(r"\+\d{7,15}", phone) or not apikey.strip():
            log.warning(
                "WhatsApp recipient %d in %s is not +<country code><number>:<apikey> - skipped",
                position, WHATSAPP_ENV_VAR,
            )
            continue
        recipients.append(WhatsAppRecipient(phone=phone, apikey=apikey.strip()))
    if raw.strip() and not recipients:
        log.warning("WhatsApp notifications off - no usable recipients in %s", WHATSAPP_ENV_VAR)
    return tuple(recipients)


def _scrub(text: str, recipient: WhatsAppRecipient) -> str:
    """Remove the key and the full phone number from anything about to be
    logged - request errors quote the URL, and CallMeBot's reply quotes the
    number. The number is also matched without its +, as it may appear
    either way."""
    for secret, stand_in in (
        (recipient.apikey, "***"), (recipient.phone, recipient.masked), (recipient.phone.lstrip("+"), recipient.masked),
    ):
        text = text.replace(secret, stand_in)
    return text


def whatsapp_url(recipient: WhatsAppRecipient, text: str) -> str:
    # %20 for spaces and %0A for new lines, as CallMeBot documents - not the
    # "+" for spaces that form-encoding (and requests' params=) would use.
    query = urllib.parse.urlencode(
        {"phone": recipient.phone, "text": text, "apikey": recipient.apikey}, quote_via=urllib.parse.quote
    )
    return f"{WHATSAPP_URL}?{query}"


class Notifier:
    def __init__(
        self, pair_name: str, config: EmailConfig | None,
        whatsapp: tuple[WhatsAppRecipient, ...] = (),
    ):
        self._pair_name = pair_name
        self._config = config
        self._whatsapp = tuple(whatsapp)

    def send(self, subject: str, body: str) -> None:
        if self._config is None and not self._whatsapp:
            return
        threading.Thread(target=self._deliver_all, args=(subject, body), name="notify", daemon=True).start()

    def _deliver_all(self, subject: str, body: str) -> None:
        """Every configured channel, each failing on its own."""
        if self._config is not None:
            self._deliver(subject, body)
        for recipient in self._whatsapp:
            self._deliver_whatsapp(recipient, subject, body)

    def _deliver_whatsapp(self, recipient: WhatsAppRecipient, subject: str, body: str) -> None:
        text = f"*[oil-pair {self._pair_name}] {subject}*\n\n{body}"  # *...* is bold in WhatsApp
        try:
            response = requests.get(whatsapp_url(recipient, text), timeout=WHATSAPP_TIMEOUT_SECONDS)
        except Exception as exc:  # noqa: BLE001 - see module docstring
            # A requests error message quotes the URL, and the URL carries the key.
            log.error("failed to send WhatsApp %r to %s: %s", subject, recipient.masked, _scrub(str(exc), recipient))
            return
        reply = _scrub(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", response.text or "")), recipient)
        # CallMeBot echoes the whole message back before its verdict, so keep
        # the tail, where the verdict is, rather than the head.
        reply = re.sub(r"Text to send: .*?(?=(Message queued|ERROR|Error|error|APIKey|Note|$))", "", reply).strip()[-200:]
        if response.status_code != 200:
            log.error("failed to send WhatsApp %r to %s: HTTP %s %s", subject, recipient.masked,
                      response.status_code, reply)
            return
        log.info("sent WhatsApp %r to %s (%s)", subject, recipient.masked, reply)

    def _deliver(self, subject: str, body: str) -> None:
        assert self._config is not None
        message = EmailMessage()
        message["Subject"] = f"[oil-pair {self._pair_name}] {subject}"
        message["From"] = self._config.sender
        message["To"] = self._config.recipient
        if self._config.cc:
            # send_message() delivers to Cc recipients too, read from this header.
            message["Cc"] = ", ".join(self._config.cc)
        message.set_content(body)
        try:
            with smtplib.SMTP(self._config.host, self._config.port, timeout=SMTP_TIMEOUT_SECONDS) as smtp:
                smtp.starttls()
                smtp.login(self._config.username, self._config.password)
                smtp.send_message(message)
            log.info(
                "emailed %r to %s%s", subject, self._config.recipient,
                f" (cc {', '.join(self._config.cc)})" if self._config.cc else "",
            )
        except Exception:
            log.exception("failed to send email %r", subject)


if __name__ == "__main__":
    # Sends one test message on every configured channel and waits for it.
    from dotenv import load_dotenv

    from oil_pair.settings import REPO_ROOT

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    load_dotenv(REPO_ROOT / ".env")
    notifier = Notifier("test", load_email_config(), load_whatsapp_recipients())
    if notifier._config is None and not notifier._whatsapp:
        raise SystemExit("nothing configured - set the SMTP_* and/or NOTIFY_WHATSAPP variables in .env")
    notifier._deliver_all(
        "Test notification",
        "If you can read this, oil-pair can reach you here.\nOpen and close alerts will arrive the same way.",
    )
