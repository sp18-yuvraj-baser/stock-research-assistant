from datetime import date
from typing import Any

import psycopg

from sra.sec import endpoints
from sra.sec.client import SecClient

UPSERT = """
INSERT INTO filings (accession_no, cik, form_type, filed_date,
                     period_of_report, is_amendment)
VALUES (%(accession_no)s, %(cik)s, %(form_type)s, %(filed_date)s,
        %(period_of_report)s, %(is_amendment)s)
ON CONFLICT (accession_no) DO UPDATE
SET form_type = EXCLUDED.form_type,
    filed_date = EXCLUDED.filed_date,
    period_of_report = EXCLUDED.period_of_report,
    is_amendment = EXCLUDED.is_amendment
"""


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    return date.fromisoformat(value)


def _rows_from_table(cik: str, table: dict[str, list[Any]]) -> list[dict[str, Any]]:
    """submissions.json stores filings column-wise: one parallel array per field."""
    accessions = table.get("accessionNumber") or []
    rows: list[dict[str, Any]] = []
    for i, accession_no in enumerate(accessions):
        form_type = table["form"][i]
        rows.append(
            {
                "accession_no": accession_no,
                "cik": cik,
                "form_type": form_type,
                "filed_date": _parse_date(table["filingDate"][i]),
                "period_of_report": _parse_date(table["reportDate"][i]),
                "is_amendment": form_type.endswith("/A"),
            }
        )
    return rows


def upsert_filings(
    conn: psycopg.Connection,
    client: SecClient,
    *,
    cik: str,
    submissions: dict[str, Any],
) -> int:
    """Ingest the filer's whole filing history.

    Every form type is stored, not only 10-K/10-Q: an XBRL fact can be reported
    in an 8-K or S-1, and facts.accession_no is a foreign key onto this table.
    """
    filings = submissions.get("filings", {})
    rows = _rows_from_table(cik, filings.get("recent", {}))

    # submissions.json inlines only the most recent ~1000 filings; older ones
    # live in separate paged files that are immutable once written.
    for page in filings.get("files", []):
        payload = client.get_json(
            endpoints.submissions_page(page["name"]), immutable=True
        )
        rows.extend(_rows_from_table(cik, payload))

    with conn.cursor() as cur:
        cur.executemany(UPSERT, rows)
    return len(rows)
