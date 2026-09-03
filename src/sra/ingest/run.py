from dataclasses import dataclass

import psycopg

from sra.config import settings
from sra.ingest.companies import upsert_company
from sra.ingest.facts import ingest_company_facts
from sra.ingest.filings import upsert_filings
from sra.ingest.fiscal import derive_fiscal_labels
from sra.sec import endpoints
from sra.sec.client import SecClient
from sra.sec.tickers import resolve_tickers

DEFAULT_TICKERS = ("NVDA", "AAPL", "MSFT", "COST", "WMT")


@dataclass(frozen=True)
class CompanyIngestReport:
    ticker: str
    cik: str
    filings: int
    facts_staged: int
    facts_skipped: int
    stub_filings: int
    fiscal_labels: int


def ingest_tickers(
    tickers: list[str] | None = None,
    *,
    refresh: bool = False,
) -> list[CompanyIngestReport]:
    """Ingest the structured path for each ticker. Safe to re-run."""
    requested = list(tickers) if tickers else list(DEFAULT_TICKERS)
    reports: list[CompanyIngestReport] = []

    with SecClient() as client:
        cik_by_ticker = resolve_tickers(client, requested)
        for ticker, cik in cik_by_ticker.items():
            submissions = client.get_json(endpoints.submissions(cik), refresh=refresh)
            company_facts = client.get_json(
                endpoints.company_facts(cik), refresh=refresh
            )
            # One transaction per company: a failure part-way through leaves the
            # other companies intact and the run re-runnable.
            with psycopg.connect(settings().dsn) as conn:
                upsert_company(conn, cik=cik, ticker=ticker, submissions=submissions)
                filing_count = upsert_filings(
                    conn, client, cik=cik, submissions=submissions
                )
                fact_result = ingest_company_facts(
                    conn, cik=cik, company_facts=company_facts
                )
                labelled = derive_fiscal_labels(conn, cik=cik)
                conn.commit()
            reports.append(
                CompanyIngestReport(
                    ticker=ticker,
                    cik=cik,
                    filings=filing_count,
                    facts_staged=fact_result.rows_staged,
                    facts_skipped=fact_result.rows_skipped,
                    stub_filings=fact_result.stub_filings,
                    fiscal_labels=labelled,
                )
            )
    return reports
