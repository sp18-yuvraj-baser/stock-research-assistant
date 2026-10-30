"""Store every mutual-fund scheme's latest NAV from AMFI's daily file.

AMFI publishes one semicolon-separated file of all schemes, with a line
naming the category ("Open Ended Schemes(Equity Scheme - Flexi Cap Fund)")
and one naming the fund house above each block. Safe to re-run daily.
"""

import logging
import re
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation

import httpx

from sra.db import connect
from sra.feeds import USER_AGENT, FeedError
from sra.figures import parse_day

log = logging.getLogger(__name__)

AMFI_NAV_URL = "https://portal.amfiindia.com/spages/NAVAll.txt"
HEADER = "Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;Scheme Name"

UPSERT_FUND = """
INSERT INTO funds
  (scheme_code, name, fund_house, category, plan, option, isin, nav, nav_date,
   fetched_at)
VALUES
  (%(scheme_code)s, %(name)s, %(fund_house)s, %(category)s, %(plan)s,
   %(option)s, %(isin)s, %(nav)s, %(nav_date)s, %(fetched_at)s)
ON CONFLICT (scheme_code) DO UPDATE
SET name = EXCLUDED.name, fund_house = EXCLUDED.fund_house,
    category = EXCLUDED.category, plan = EXCLUDED.plan,
    option = EXCLUDED.option, isin = EXCLUDED.isin, nav = EXCLUDED.nav,
    nav_date = EXCLUDED.nav_date, fetched_at = EXCLUDED.fetched_at
"""


def _nav(text: str) -> Decimal | None:
    try:
        value = Decimal(text)
    except InvalidOperation:
        return None
    return value if value.is_finite() and value > 0 else None


def _day(text: str) -> date | None:
    # AMFI writes '29-Oct-2026'.
    return parse_day(text.replace("-", " "))


def parse_nav_file(body: str, fetched_at: datetime) -> list[dict[str, object]]:
    if not body.lstrip("﻿").startswith(HEADER):
        raise FeedError("the AMFI NAV file did not have the expected header")
    rows: list[dict[str, object]] = []
    category: str | None = None
    fund_house: str | None = None
    for line in body.splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        if ";" not in line:
            match = re.match(r"^.*Schemes?\s*\((.+)\)\s*$", line)
            if match:
                category = match.group(1).strip()
            else:
                fund_house = line
            continue
        fields = [f.strip() for f in line.split(";")]
        if len(fields) != 8 or not fields[0].isdigit():
            continue
        code, isin_growth, isin_reinvest, name, plan, option, nav, day = fields
        isin = next((i for i in (isin_growth, isin_reinvest) if i and i != "-"), None)
        rows.append(
            {
                "scheme_code": code,
                "name": name,
                "fund_house": fund_house,
                "category": category,
                "plan": plan or None,
                "option": option or None,
                "isin": isin,
                "nav": _nav(nav),
                "nav_date": _day(day),
                "fetched_at": fetched_at,
            }
        )
    return rows


def ingest_funds() -> int:
    """Upsert every scheme; returns how many were stored."""
    try:
        response = httpx.get(
            AMFI_NAV_URL,
            headers={"User-Agent": USER_AGENT},
            timeout=60.0,
            follow_redirects=True,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise FeedError(f"could not fetch AMFI NAVs: {exc}") from exc
    rows = parse_nav_file(response.text, datetime.now(UTC))
    with connect() as conn:
        with conn.cursor() as cursor:
            cursor.executemany(UPSERT_FUND, rows)
        conn.commit()
    log.info("funds: stored %d schemes", len(rows))
    return len(rows)
