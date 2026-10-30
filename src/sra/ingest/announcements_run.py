"""Store BSE's latest corporate announcements, mapped to companies by ISIN.

BSE's public feed carries the current day's filings (it starts over after
midnight) with a BSE scrip code; the
Upstox instrument file pairs each code with an ISIN, which is how a filing
is matched to a listed company. Re-running is safe: a filing is keyed by its
PDF link. Run hourly (cron) to catch every filing of the day.
"""

import logging
import re
from dataclasses import dataclass

from sra.db import connect
from sra.feeds import BSE_ANNOUNCEMENTS_FEED, FeedItem, fetch_rss
from sra.upstox.instruments import fetch_instrument_master

log = logging.getLogger(__name__)

INSERT_ANNOUNCEMENT = """
INSERT INTO announcements
  (bse_code, isin, company, description, category, pdf_url, published_at)
VALUES
  (%(bse_code)s, %(isin)s, %(company)s, %(description)s, %(category)s,
   %(pdf_url)s, %(published_at)s)
ON CONFLICT (pdf_url) DO NOTHING
"""

# First match wins, so the specific kinds come before the general ones.
CATEGORIES = (
    ("insider", r"insider trading|regulation 7\b|reg\.? ?7\b|\bsast\b|regulation 29\b"
                r"|regulation 31\b|pledge"),
    ("transcript", r"transcript|earnings call|conference call|analyst.{0,10}call"),
    ("annual_report", r"annual report"),
    ("presentation", r"investor presentation|presentation"),
    ("shareholding", r"shareholding pattern"),
    ("results", r"financial results?|results for the (quarter|half|year)"
                r"|audited results|unaudited results"),
    ("board_meeting", r"board meeting"),
    ("corporate_action", r"record date|dividend|bonus|split|buy.?back|rights issue"),
)  # fmt: skip


def categorise(description: str) -> str:
    text = description.lower()
    for category, pattern in CATEGORIES:
        if re.search(pattern, text):
            return category
    return "other"


def bse_isins() -> dict[str, str]:
    """BSE scrip code -> ISIN for every BSE equity in the instrument file."""
    return {
        str(r["exchange_token"]): str(r["isin"])
        for r in fetch_instrument_master()
        if r.get("segment") == "BSE_EQ" and r.get("exchange_token") and r.get("isin")
    }


@dataclass(frozen=True)
class AnnouncementIngestReport:
    seen: int
    stored: int
    unmapped: int  # filings whose scrip code had no ISIN in the instrument file


def _row(item: FeedItem, isins: dict[str, str]) -> dict[str, object] | None:
    code = item.fields.get("scripcode", "").strip()
    if not code or not item.link or item.published is None:
        return None
    company = re.sub(r"\s*\(\d+\)\s*$", "", item.title).strip() or code
    return {
        "bse_code": code,
        "isin": isins.get(code),
        "company": company,
        "description": item.description,
        "category": categorise(item.description),
        "pdf_url": item.link,
        "published_at": item.published,
    }


def ingest_announcements() -> AnnouncementIngestReport:
    items = fetch_rss(BSE_ANNOUNCEMENTS_FEED)
    isins = bse_isins()
    rows = [row for item in items if (row := _row(item, isins)) is not None]
    with connect() as conn:
        before = conn.execute("SELECT count(*) AS n FROM announcements").fetchone()
        with conn.cursor() as cursor:
            cursor.executemany(INSERT_ANNOUNCEMENT, rows)
        after = conn.execute("SELECT count(*) AS n FROM announcements").fetchone()
        conn.commit()
    stored = int(after["n"]) - int(before["n"]) if before and after else 0
    unmapped = sum(1 for row in rows if row["isin"] is None)
    log.info(
        "announcements: %d in feed, %d new, %d unmapped", len(rows), stored, unmapped
    )
    return AnnouncementIngestReport(len(rows), stored, unmapped)
