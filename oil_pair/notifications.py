"""Trade-close email notification, sent via iCloud Mail so it lands on the
operator's phone through the stock Mail app - no companion app needed.

Best-effort only: send_trade_summary_email is meant to be called from
main.py wrapped in a try/except that just logs on failure. A notification
is a convenience on top of trading, not part of it, so it must never be
allowed to interrupt or fail the trading loop itself.
"""

from __future__ import annotations

import logging
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage

import pandas as pd

from oil_pair.settings import EmailConfig
from oil_pair.strategy_logic import Action, PairSide

log = logging.getLogger(__name__)

SMTP_HOST = "smtp.mail.me.com"
SMTP_PORT = 587

_OUTCOME_LABELS = {
    Action.EXIT_TAKE_PROFIT: "take profit",
    Action.EXIT_STOP_LOSS: "stop loss",
}


@dataclass(frozen=True)
class LegSummary:
    epic: str
    direction: str  # "BUY" or "SELL", as opened
    size: float
    entry_mid: float
    exit_mid: float


@dataclass(frozen=True)
class TradeSummary:
    pair_name: str
    side: PairSide
    action: Action  # EXIT_TAKE_PROFIT or EXIT_STOP_LOSS
    reason: str
    entered_at: pd.Timestamp
    exited_at: pd.Timestamp
    entry_spread: float
    exit_spread: float
    std_spread: float
    currency_code: str
    legs: list[LegSummary]


def _leg_pnl(leg: LegSummary) -> float:
    sign = 1 if leg.direction == "BUY" else -1
    return sign * leg.size * (leg.exit_mid - leg.entry_mid)


def _format_duration(duration: pd.Timedelta) -> str:
    total_seconds = int(duration.total_seconds())
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m {seconds}s"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def _spread_line(label: str, at: pd.Timestamp, spread: float, std_spread: float) -> str:
    if std_spread:
        return f"{label} {at.isoformat()}  (spread={spread:.4f}, {spread / std_spread:+.2f}σ)"
    return f"{label} {at.isoformat()}  (spread={spread:.4f})"


def format_trade_summary(summary: TradeSummary) -> tuple[str, str]:
    """Returns (subject, body). Split out from send_trade_summary_email so
    the formatting is unit-testable without touching a network socket."""
    total_pnl = sum(_leg_pnl(leg) for leg in summary.legs)
    outcome = _OUTCOME_LABELS.get(summary.action, summary.action.value)

    subject = (
        f"[oil_pair] {summary.pair_name} {summary.side.value} closed ({outcome}) "
        f"- est. P&L {total_pnl:+.2f} {summary.currency_code}"
    )

    lines = [
        f"Pair: {summary.pair_name}",
        f"Side: {summary.side.value}",
        f"Reason: {summary.reason}",
        "",
        _spread_line("Entered:", summary.entered_at, summary.entry_spread, summary.std_spread),
        _spread_line("Exited: ", summary.exited_at, summary.exit_spread, summary.std_spread),
        f"Duration: {_format_duration(summary.exited_at - summary.entered_at)}",
        "",
        "Legs:",
    ]
    for leg in summary.legs:
        lines.append(
            f"  {leg.epic:<20} {leg.direction:<4} size={leg.size:<6.2f} "
            f"entry_mid={leg.entry_mid:<10.2f} exit_mid={leg.exit_mid:<10.2f} ~{_leg_pnl(leg):+.2f}"
        )
    lines += [
        "",
        f"Estimated P&L: {total_pnl:+.2f} {summary.currency_code} "
        f"(mid-price based - excludes actual fill slippage/spread cost)",
    ]
    return subject, "\n".join(lines)


def send_trade_summary_email(config: EmailConfig, summary: TradeSummary) -> None:
    subject, body = format_trade_summary(summary)

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = config.smtp_username
    message["To"] = config.to_address
    message.set_content(body)

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as smtp:
        smtp.starttls()
        smtp.login(config.smtp_username, config.smtp_app_password)
        smtp.send_message(message)
    log.info("trade summary emailed to %s", config.to_address)
