"""Email notifications for pair opens and closes.

Configured from .env (see .env.example). With no SMTP settings it does
nothing, and a send that fails is logged and dropped: a notification is never
worth interrupting the trading loop for, least of all midway through closing
a pair. Sends run on a background thread for the same reason - the caller
still has to save state for the legs it just opened or closed, and a slow
SMTP server must not stand between the trade and that save.
"""

from __future__ import annotations

import logging
import os
import smtplib
import threading
from dataclasses import dataclass
from email.message import EmailMessage

log = logging.getLogger(__name__)

SMTP_TIMEOUT_SECONDS = 15

_REQUIRED_ENV_VARS = ("SMTP_HOST", "SMTP_USERNAME", "SMTP_PASSWORD", "NOTIFY_EMAIL_TO")


@dataclass(frozen=True)
class EmailConfig:
    host: str
    port: int
    username: str
    password: str
    sender: str
    recipient: str


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
    )


class Notifier:
    def __init__(self, pair_name: str, config: EmailConfig | None):
        self._pair_name = pair_name
        self._config = config

    def send(self, subject: str, body: str) -> None:
        if self._config is None:
            return
        threading.Thread(target=self._deliver, args=(subject, body), name="email", daemon=True).start()

    def _deliver(self, subject: str, body: str) -> None:
        assert self._config is not None
        message = EmailMessage()
        message["Subject"] = f"[oil-pair {self._pair_name}] {subject}"
        message["From"] = self._config.sender
        message["To"] = self._config.recipient
        message.set_content(body)
        try:
            with smtplib.SMTP(self._config.host, self._config.port, timeout=SMTP_TIMEOUT_SECONDS) as smtp:
                smtp.starttls()
                smtp.login(self._config.username, self._config.password)
                smtp.send_message(message)
            log.info("emailed %r to %s", subject, self._config.recipient)
        except Exception:
            log.exception("failed to send email %r", subject)
