"""Read RSS feeds: Google News, Economic Times, BSE announcements.

Parsed with the standard library's XML reader; any network or parse failure
becomes a FeedError so a caller can say "unavailable" instead of crashing.
"""

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

import httpx

from sra.figures import parse_day

# Some publishers refuse requests without a browser-like agent.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120 Safari/537.36"
)
TIMEOUT_SECONDS = 20.0
# BSE's day of announcements is about 3.5 MB; anything far larger is not a feed.
MAX_FEED_BYTES = 25 * 1024 * 1024
IST = ZoneInfo("Asia/Kolkata")

ET_MARKETS_FEED = (
    "https://economictimes.indiatimes.com/markets/stocks/rssfeeds/2146842.cms"
)
BSE_ANNOUNCEMENTS_FEED = "https://www.bseindia.com/data/xml/announcements.xml"


class FeedError(RuntimeError):
    pass


@dataclass(frozen=True)
class FeedItem:
    title: str
    link: str
    published: datetime | None
    source: str
    description: str
    fields: dict[str, str]  # every child element's text, for feed-specific tags


def google_news_url(query: str) -> str:
    return (
        f"https://news.google.com/rss/search?q={quote_plus(query)}"
        "&hl=en-IN&gl=IN&ceid=IN:en"
    )


def _published(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        return parsedate_to_datetime(text)
    except (TypeError, ValueError):
        pass
    # BSE: '29-Oct-2026 20:52:19', Indian time. Parsed without strptime's
    # %b, which depends on the process locale.
    match = re.match(
        r"^(\d{1,2})-([A-Z][a-z]{2})-(\d{4}) (\d{2}):(\d{2}):(\d{2})$", text.strip()
    )
    day = (
        parse_day(f"{match.group(1)} {match.group(2)} {match.group(3)}")
        if match
        else None
    )
    if match is None or day is None:
        return None
    hour, minute, second = (int(match.group(i)) for i in (4, 5, 6))
    return datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=IST)


def parse_rss(body: bytes) -> list[FeedItem]:
    # These feeds come from outside servers. RSS never needs a DTD, so any
    # DOCTYPE or entity declaration is refused before parsing: that rules out
    # entity-expansion ("billion laughs") and external-entity tricks without
    # depending on the parser's own limits.
    if b"<!DOCTYPE" in body or b"<!ENTITY" in body:
        raise FeedError("feed declares a DTD or entities; refused")
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise FeedError(f"feed was not valid XML: {exc}") from exc
    items = []
    for node in root.iter("item"):
        fields = {child.tag: (child.text or "").strip() for child in node}
        source = fields.get("source", "")
        items.append(
            FeedItem(
                title=fields.get("title", ""),
                link=fields.get("link", ""),
                published=_published(fields.get("pubDate")),
                source=source,
                description=fields.get("description", ""),
                fields=fields,
            )
        )
    return items


def fetch_rss(url: str) -> list[FeedItem]:
    try:
        response = httpx.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=TIMEOUT_SECONDS,
            follow_redirects=True,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise FeedError(f"could not fetch the feed: {exc}") from exc
    if len(response.content) > MAX_FEED_BYTES:
        raise FeedError("feed is too large")
    return parse_rss(response.content)


def newest_first(items: list[FeedItem]) -> list[FeedItem]:
    floor = datetime.min.replace(tzinfo=UTC)
    return sorted(items, key=lambda item: item.published or floor, reverse=True)
