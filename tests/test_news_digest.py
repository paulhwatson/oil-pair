import importlib.util
import sys
from pathlib import Path

import pandas as pd
import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "news_digest.py"


@pytest.fixture(scope="module")
def nd():
    spec = importlib.util.spec_from_file_location("news_digest", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["news_digest"] = module  # dataclasses look their module up by name
    spec.loader.exec_module(module)
    return module


RSS = """<?xml version="1.0"?><rss><channel>
<item><title>Refinery fire cuts diesel output - Reuters</title><source>Reuters</source>
<pubDate>Sun, 11 Oct 2026 06:00:00 GMT</pubDate></item>
<item><title>Brent steadies ahead of OPEC meeting - FT</title><source>FT</source>
<pubDate>not a date</pubDate></item>
</channel></rss>"""


def test_rss_titles_lose_the_appended_source(nd):
    items = nd.parse_rss(RSS)

    assert [h.title for h in items] == ["Refinery fire cuts diesel output", "Brent steadies ahead of OPEC meeting"]
    assert items[0].source == "Reuters" and items[0].published is not None
    assert items[1].published is None  # a bad date doesn't drop the headline


def test_rss_respects_the_limit(nd):
    assert len(nd.parse_rss(RSS, limit=1)) == 1


def test_wednesday_has_the_eia_report_in_uk_time(nd):
    events = nd.scheduled_today(pd.Timestamp("2026-10-21"))  # Wed, BST, no holiday that week
    assert events == ["15:30 US oil inventories (EIA) - oil pairs"]


def test_a_us_holiday_moves_the_eia_report_to_thursday(nd):
    # Labor Day Monday 7 Sep 2026: no Wednesday report, Thursday at 11:00 New York instead
    assert nd.scheduled_today(pd.Timestamp("2026-09-09")) == []
    assert nd.scheduled_today(pd.Timestamp("2026-09-10")) == [
        "16:00 US oil inventories (EIA, holiday-delayed) - oil pairs"]


def test_wasde_and_crop_progress_days(nd):
    assert "17:00 USDA WASDE supply & demand report - grain pairs" in nd.scheduled_today(pd.Timestamp("2026-10-09"))
    assert nd.scheduled_today(pd.Timestamp("2026-10-19")) == ["21:00 USDA crop progress - grain pairs"]  # Monday
    assert nd.scheduled_today(pd.Timestamp("2026-12-07")) == []  # crop progress runs April-November only


def test_every_configured_pair_is_covered(nd):
    pairs = nd.load_pairs()
    assert {p.name for p in pairs} >= {"brent_wti", "soybeans_soymeal"}
    assert all(len(p.legs) == 2 for p in pairs)


def _inputs(nd):
    pairs = [nd.Pair("brent_heating_oil", (("CC.D.LCO.USS.IP", "Oil - Brent Crude"), ("CC.D.HO.USS.IP", "Heating Oil")))]
    headlines = {"diesel prices": [nd.Headline("Refinery fire cuts diesel output", "Reuters", None)]}
    return pairs, headlines, ["15:30 US oil inventories (EIA) - oil pairs"]


def test_without_an_api_key_it_sends_the_headlines(nd):
    subject, body = nd.build(*_inputs(nd), api_key=None, model="m")

    assert subject == "Daily news: 1 headlines (unscored)"
    assert "Refinery fire cuts diesel output (Reuters)" in body
    assert "15:30 US oil inventories" in body


def test_with_a_key_claudes_flags_go_in_the_subject(nd, monkeypatch):
    sent = {}

    class Reply:
        status_code = 200

        def json(self):
            return {"content": [{"type": "text", "text": "FLAGS: 1\nbrent_heating_oil: WATCH - refinery fire hits diesel only"}]}

    def post(url, timeout, headers, json):
        sent.update(prompt=json["messages"][0]["content"], model=json["model"])
        return Reply()

    monkeypatch.setattr(nd.requests, "post", post)
    subject, body = nd.build(*_inputs(nd), api_key="sk-test", model="claude-sonnet-5-5")

    assert subject == "Daily news: 1 pair(s) flagged"
    assert body.startswith("FLAGS: 1\nbrent_heating_oil: WATCH")
    assert "Refinery fire cuts diesel output" in sent["prompt"] and "untrusted data" in sent["prompt"]
    assert sent["model"] == "claude-sonnet-5-5"


def test_an_api_failure_falls_back_and_never_logs_the_key(nd, monkeypatch, caplog):
    class Reply:
        status_code = 401
        text = "invalid x-api-key sk-secret-123"

    monkeypatch.setattr(nd.requests, "post", lambda *a, **k: Reply())
    subject, body = nd.build(*_inputs(nd), api_key="sk-secret-123", model="m")

    assert subject.endswith("(unscored)")
    assert "sk-secret-123" not in caplog.text and "HTTP 401" in caplog.text


def test_columbus_day_moves_both_crop_progress_and_the_eia_report(nd):
    """Monday 12 Oct 2026 is a US federal holiday: crop progress slips to
    Tuesday, and that week's EIA report from Wednesday to Thursday."""
    assert nd.scheduled_today(pd.Timestamp("2026-10-12")) == []
    assert nd.scheduled_today(pd.Timestamp("2026-10-13")) == ["21:00 USDA crop progress - grain pairs"]
    assert nd.scheduled_today(pd.Timestamp("2026-10-14")) == []
    assert nd.scheduled_today(pd.Timestamp("2026-10-15")) == ["16:00 US oil inventories (EIA, holiday-delayed) - oil pairs"]
