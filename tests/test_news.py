from datetime import UTC, datetime

import pytest

from sra import feeds
from sra.agent import news
from sra.agent.companies import Resolution
from sra.feeds import FeedError, FeedItem, parse_rss
from sra.ingest.announcements_run import _row, categorise
from sra.upstox.instruments import EquityInstrument

RSS = b"""<?xml version="1.0"?><rss><channel>
<item><title>TCS wins deal - Business Today</title><link>https://example.com/a</link>
<pubDate>Thu, 29 Oct 2026 10:00:00 GMT</pubDate><source>Business Today</source></item>
<item><title>Infosys results (500209)</title><link>https://bse.example/x.pdf</link>
<scripcode>500209</scripcode>
<description>Financial Results for the quarter</description>
<pubDate>29-Oct-2026 20:52:19</pubDate></item>
</channel></rss>"""


def test_parse_rss_reads_both_date_styles_and_extra_fields() -> None:
    first, second = parse_rss(RSS)
    assert first.published == datetime(2026, 10, 29, 10, 0, tzinfo=UTC)
    assert second.fields["scripcode"] == "500209"
    assert second.published is not None
    assert second.published.isoformat() == "2026-10-29T20:52:19+05:30"


@pytest.mark.parametrize(
    "body",
    [
        b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "x">]><rss/>',
        b"not xml at all",
    ],
)
def test_untrusted_or_broken_feeds_are_refused(body: bytes) -> None:
    with pytest.raises(FeedError):
        parse_rss(body)


def test_company_news_lists_headlines_once_per_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tcs = EquityInstrument("TCS", "NSE_EQ|TCS", "NSE_EQ", "TATA CONSULTANCY")
    monkeypatch.setattr(news, "resolve_companies", lambda q: Resolution([tcs], []))
    monkeypatch.setattr(news, "fetch_rss", lambda url: parse_rss(RSS)[:1])
    text = news.news("latest news on TCS").text
    assert "TATA CONSULTANCY (TCS): latest news" in text
    assert "29 Oct, 15:30 · Business Today: TCS wins deal https://example.com/a" in text


def test_news_outage_is_said_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(news, "resolve_companies", lambda q: Resolution([], []))

    def down(url: str) -> list[FeedItem]:
        raise FeedError("timeout")

    monkeypatch.setattr(news, "fetch_rss", down)
    assert "unavailable right now (timeout)" in news.news("market news").text


@pytest.mark.parametrize(
    ("description", "category"),
    [
        ("Disclosure under Regulation 7(2) of SEBI (PIT) Regulations", "insider"),
        ("Certificate under Reg. 74(5) of SEBI (DP) Regulations", "other"),
        ("Transcript of the Q2 Earnings Call", "transcript"),
        ("Investor Presentation", "presentation"),
        ("Financial Results for the quarter ended 30 Sep 2026", "results"),
        ("Intimation of Board Meeting", "board_meeting"),
        ("Record date for interim dividend", "corporate_action"),
    ],
)
def test_categorise(description: str, category: str) -> None:
    assert categorise(description) == category


def test_announcement_rows_map_scrip_codes_to_isins() -> None:
    item = parse_rss(RSS)[1]
    row = _row(item, {"500209": "INE009A01021"})
    assert row is not None
    assert row["company"] == "Infosys results"
    assert row["isin"] == "INE009A01021"
    assert row["category"] == "results"


def test_feed_urls_are_encoded() -> None:
    assert "q=M%26M+share+price" in feeds.google_news_url("M&M share price")
