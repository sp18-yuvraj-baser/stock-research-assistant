"""News headlines and BSE announcements, rendered in code.

Headlines come straight from Google News (per company) or the Economic Times
markets feed, newest first, with source and link. They are listed, never
summarised: a model paraphrasing a headline it has not read is how a rumour
becomes a "fact". Announcements are BSE filings stored by
`sra ingest announcements`.
"""

import re
from datetime import UTC, datetime, timedelta

import psycopg

from sra.agent.companies import resolve_companies
from sra.agent.loop import PathResult
from sra.db import connect
from sra.feeds import (
    ET_MARKETS_FEED,
    IST,
    FeedError,
    FeedItem,
    fetch_rss,
    google_news_url,
    newest_first,
)

HEADLINES_SHOWN = 8
ANNOUNCEMENTS_SHOWN = 12

NO_ANNOUNCEMENTS = (
    "No BSE announcements are stored yet. Run `sra migrate`, then "
    "`sra ingest announcements` hourly (e.g. from cron) to collect them; BSE's "
    "feed only covers the current day."
)


def _when(moment: datetime | None) -> str:
    return moment.astimezone(IST).strftime("%d %b, %H:%M") if moment else "undated"


def _headline(item: FeedItem, default_source: str) -> str:
    title, source = item.title, item.source
    # Google News titles end in " - Publisher"; show the publisher once.
    if " - " in title:
        head, tail = title.rsplit(" - ", 1)
        if not source or tail.strip() == source:
            title, source = head, source or tail
    return (
        f"  {_when(item.published)} · {source or default_source}: {title} {item.link}"
    )


def news(question: str) -> PathResult:
    found = resolve_companies(question)
    company = found.companies[0] if len(found.companies) == 1 else None
    if company is not None:
        title = f"{company.name} ({company.ticker}): latest news"
        url, source = google_news_url(f"{company.ticker} share price"), "Google News"
    else:
        title, url, source = "Market news", ET_MARKETS_FEED, "Economic Times"
    try:
        items = newest_first(fetch_rss(url))[:HEADLINES_SHOWN]
    except FeedError as exc:
        return PathResult(text=f"{title}\nNews is unavailable right now ({exc}).")
    if not items:
        return PathResult(text=f"{title}\nNo recent headlines found.")
    return PathResult(
        text="\n\n".join(
            [
                f"{title}\nHeadlines from {source}, newest first, in Indian time.",
                "Headlines\n" + "\n".join(_headline(item, source) for item in items),
                "Headlines are listed as published, not checked or summarised "
                "here; open the source before acting on one.",
            ]
        )
    )


# Which filings a question asks about: (pattern, category, days back, title).
_MODES = (
    (r"\binsider", "insider", 30, "Insider and promoter disclosures"),
    (r"\b(upcoming|calendar|board meetings?|this week'?s? results)\b",
     "board_meeting", 14, "Board meeting intimations"),
    (r"\bresults?\b", "results", 2, "Results filed"),
    (r"\b(transcripts?|earnings calls?|conference calls?)\b", "transcript", 120,
     "Earnings call filings"),
    (r"\bannual reports?\b", "annual_report", 400, "Annual reports"),
)  # fmt: skip

ANNOUNCEMENTS_SQL = """
SELECT a.published_at, a.company, a.description, a.category, a.pdf_url
FROM announcements a
WHERE (%(isin)s::text IS NULL OR a.isin = %(isin)s)
  AND (%(category)s::text IS NULL OR a.category = %(category)s)
  AND a.published_at >= %(since)s
ORDER BY a.published_at DESC
LIMIT %(limit)s
"""


def announcements(question: str, *, now: datetime | None = None) -> PathResult:
    now = now or datetime.now(UTC)
    found = resolve_companies(question)
    company = found.companies[0] if len(found.companies) == 1 else None
    category, days, title = None, 30, "Latest BSE announcements"
    for pattern, mode, back, label in _MODES:
        if re.search(pattern, question, re.IGNORECASE):
            category, days, title = mode, back, label
            break
    if company is not None:
        title = (
            f"{company.ticker}: {title.lower() if category else 'BSE announcements'}"
        )
        days = max(days, 120)
    params = {
        "isin": company.instrument_key.split("|", 1)[1] if company else None,
        "category": category,
        "since": now - timedelta(days=days),
        "limit": ANNOUNCEMENTS_SHOWN,
    }
    try:
        with connect(readonly=True) as conn:
            rows = conn.execute(ANNOUNCEMENTS_SQL, params).fetchall()
            first = conn.execute("SELECT min(published_at) AS first FROM announcements")
            coverage = first.fetchone()
    except psycopg.errors.UndefinedTable:
        return PathResult(text=NO_ANNOUNCEMENTS)
    since = coverage["first"] if coverage else None
    if since is None:
        return PathResult(text=NO_ANNOUNCEMENTS)
    note = f"Stored from {_when(since)} onwards; filings before that are not here."
    if not rows:
        return PathResult(
            text=f"{title}\nNothing matching in the last {days} days. {note}"
        )
    lines = [
        f"  {_when(r['published_at'])} · {r['company']}: {r['description'][:160]} "
        f"{r['pdf_url']}"
        for r in rows
    ]
    return PathResult(
        text="\n\n".join(
            [
                f"{title}\nBSE filings, newest first. {note}",
                "Filings\n" + "\n".join(lines),
            ]
        )
    )
