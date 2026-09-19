import pandas as pd
import pytest

from oil_pair import notifications
from oil_pair.notifications import LegSummary, TradeSummary, format_trade_summary, send_trade_summary_email
from oil_pair.settings import EmailConfig
from oil_pair.strategy_logic import Action, PairSide


def make_summary(**overrides) -> TradeSummary:
    defaults = dict(
        pair_name="brent_wti",
        side=PairSide.SHORT,
        action=Action.EXIT_TAKE_PROFIT,
        reason="spread reverted past limit threshold",
        entered_at=pd.Timestamp("2026-09-18T21:19:13Z"),
        exited_at=pd.Timestamp("2026-09-18T21:28:29Z"),
        entry_spread=7.86,
        exit_spread=1.23,
        std_spread=4.73,
        currency_code="GBP",
        legs=[
            LegSummary(epic="CC.D.LCO.USS.IP", direction="SELL", size=0.1, entry_mid=10123.4, exit_mid=10098.1),
            LegSummary(epic="CC.D.CL.USS.IP", direction="BUY", size=0.1, entry_mid=9678.2, exit_mid=9654.0),
        ],
    )
    defaults.update(overrides)
    return TradeSummary(**defaults)


def test_subject_includes_pair_side_outcome_and_pnl():
    subject, _ = format_trade_summary(make_summary())

    assert "brent_wti" in subject
    assert "SHORT" in subject
    assert "take profit" in subject
    assert "GBP" in subject


def test_stop_loss_outcome_labeled_correctly():
    subject, _ = format_trade_summary(make_summary(action=Action.EXIT_STOP_LOSS, reason="spread breached stop threshold"))

    assert "stop loss" in subject
    assert "take profit" not in subject


def test_pnl_reflects_direction_of_each_leg():
    # SELL leg (Brent) fell 25.3 -> profit of 0.1*25.3 = 2.53
    # BUY leg (WTI) fell 24.2 -> loss of 0.1*24.2 = 2.42
    # net ~= +0.11
    subject, body = format_trade_summary(make_summary())

    assert "+0.11 GBP" in subject
    assert "+0.11 GBP" in body


def test_body_includes_both_legs_and_duration():
    _, body = format_trade_summary(make_summary())

    assert "CC.D.LCO.USS.IP" in body
    assert "CC.D.CL.USS.IP" in body
    assert "9m 16s" in body  # 21:19:13 -> 21:28:29


def test_duration_over_an_hour_includes_hours():
    _, body = format_trade_summary(make_summary(
        entered_at=pd.Timestamp("2026-09-18T20:00:00Z"),
        exited_at=pd.Timestamp("2026-09-18T21:28:29Z"),
    ))

    assert "1h 28m 29s" in body


def test_zero_std_spread_omits_sigma_without_dividing_by_zero():
    _, body = format_trade_summary(make_summary(std_spread=0.0))

    assert "σ" not in body
    assert "spread=7.8600" in body


class FakeSMTP:
    last_instance: "FakeSMTP | None" = None

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.started_tls = False
        self.login_args = None
        self.sent = []
        FakeSMTP.last_instance = self

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def starttls(self):
        self.started_tls = True

    def login(self, username, password):
        self.login_args = (username, password)

    def send_message(self, message):
        self.sent.append(message)


def test_send_trade_summary_email_logs_in_and_sends_via_starttls(monkeypatch):
    monkeypatch.setattr(notifications.smtplib, "SMTP", FakeSMTP)
    config = EmailConfig(smtp_username="me@icloud.com", smtp_app_password="app-pw", to_address="me@icloud.com")

    send_trade_summary_email(config, make_summary())

    smtp = FakeSMTP.last_instance
    assert smtp.host == "smtp.mail.me.com"
    assert smtp.started_tls is True
    assert smtp.login_args == ("me@icloud.com", "app-pw")
    assert len(smtp.sent) == 1
    assert smtp.sent[0]["To"] == "me@icloud.com"
