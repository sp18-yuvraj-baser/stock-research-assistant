from typing import Any

import psycopg

from sra.config import settings
from sra.ingest.chunks import ChunkIngestReport, index_filing
from sra.sec.client import SecClient

# Enough history for year-over-year comparisons without embedding a decade of
# filings on a laptop.
DEFAULT_ANNUAL_REPORTS = 2
DEFAULT_QUARTERLY_REPORTS = 4

SELECT_FILINGS = """
WITH ranked AS (
    SELECT c.ticker,
           f.cik,
           f.accession_no,
           f.form_type,
           f.period_of_report,
           f.primary_document,
           row_number() OVER (
               PARTITION BY f.cik, f.form_type
               ORDER BY f.period_of_report DESC
           ) AS recency
    FROM filings f
    JOIN companies c USING (cik)
    WHERE f.form_type = ANY(%(forms)s)
      AND f.primary_document ILIKE '%%.htm'
      AND f.period_of_report IS NOT NULL
      AND (%(tickers)s::text[] IS NULL OR c.ticker = ANY(%(tickers)s))
)
SELECT * FROM ranked
WHERE (form_type = '10-K' AND recency <= %(annual)s)
   OR (form_type = '10-Q' AND recency <= %(quarterly)s)
ORDER BY ticker, form_type, period_of_report DESC
"""


def index_narrative(
    tickers: list[str] | None = None,
    *,
    annual: int = DEFAULT_ANNUAL_REPORTS,
    quarterly: int = DEFAULT_QUARTERLY_REPORTS,
) -> list[ChunkIngestReport]:
    """Index the narrative path for the most recent filings. Safe to re-run."""
    params: dict[str, Any] = {
        "forms": ["10-K", "10-Q"],
        "tickers": [t.upper() for t in tickers] if tickers else None,
        "annual": annual,
        "quarterly": quarterly,
    }
    reports: list[ChunkIngestReport] = []
    with SecClient() as client:
        with psycopg.connect(settings().dsn, row_factory=psycopg.rows.dict_row) as conn:
            targets = conn.execute(SELECT_FILINGS, params).fetchall()
        for target in targets:
            # One transaction per filing so a failure part-way through a run
            # leaves the already-indexed filings intact.
            with psycopg.connect(
                settings().dsn, row_factory=psycopg.rows.dict_row
            ) as conn:
                report = index_filing(
                    conn,
                    client,
                    cik=str(target["cik"]),
                    ticker=str(target["ticker"]),
                    accession_no=str(target["accession_no"]),
                    form_type=str(target["form_type"]),
                    primary_document=str(target["primary_document"]),
                )
                conn.commit()
            reports.append(report)
    return reports
