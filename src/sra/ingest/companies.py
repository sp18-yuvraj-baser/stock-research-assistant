from typing import Any

import psycopg

UPSERT = """
INSERT INTO companies (cik, ticker, name, fiscal_year_end)
VALUES (%(cik)s, %(ticker)s, %(name)s, %(fiscal_year_end)s)
ON CONFLICT (cik) DO UPDATE
SET ticker = EXCLUDED.ticker,
    name = EXCLUDED.name,
    fiscal_year_end = EXCLUDED.fiscal_year_end
"""


def upsert_company(
    conn: psycopg.Connection,
    *,
    cik: str,
    ticker: str,
    submissions: dict[str, Any],
) -> None:
    # EDGAR's fiscalYearEnd is a nominal MMDD. Filers on a 52/53-week calendar
    # end on a weekday near it, not on it, so this is stored for reference only
    # and never used to derive a fact's fiscal period.
    conn.execute(
        UPSERT,
        {
            "cik": cik,
            "ticker": ticker,
            "name": submissions.get("name") or ticker,
            "fiscal_year_end": submissions.get("fiscalYearEnd"),
        },
    )
