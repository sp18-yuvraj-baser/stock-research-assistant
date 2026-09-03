from dataclasses import dataclass
from datetime import date
from typing import Any

import psycopg

from sra.periods import classify_period, duration_days

STAGING_DDL = """
DROP TABLE IF EXISTS facts_staging;
CREATE TEMP TABLE facts_staging (
  cik           CHAR(10),
  accession_no  TEXT,
  taxonomy      TEXT,
  tag           TEXT,
  unit          TEXT,
  value         NUMERIC,
  period_start  DATE,
  period_end    DATE,
  duration_days INT,
  period_type   TEXT,
  form          TEXT,
  filed_fy      INT,
  filed_fp      TEXT
) ON COMMIT DROP
"""

# DISTINCT ON collapses duplicates inside one companyfacts payload: the same
# value is sometimes listed twice for a period, and ON CONFLICT DO UPDATE
# refuses to touch the same row twice in one statement.
MERGE = """
INSERT INTO facts (cik, accession_no, taxonomy, tag, unit, value,
                   period_start, period_end, duration_days, period_type,
                   form, filed_fy, filed_fp)
SELECT DISTINCT ON (cik, taxonomy, tag, unit, period_end,
                    COALESCE(period_start, DATE '0001-01-01'), accession_no)
       cik, accession_no, taxonomy, tag, unit, value,
       period_start, period_end, duration_days, period_type,
       form, filed_fy, filed_fp
FROM facts_staging
ORDER BY cik, taxonomy, tag, unit, period_end,
         COALESCE(period_start, DATE '0001-01-01'), accession_no
ON CONFLICT (cik, taxonomy, tag, unit, period_end, period_start_key,
             accession_no)
DO UPDATE SET value = EXCLUDED.value,
              duration_days = EXCLUDED.duration_days,
              period_type = EXCLUDED.period_type,
              form = EXCLUDED.form,
              filed_fy = EXCLUDED.filed_fy,
              filed_fp = EXCLUDED.filed_fp
"""

# A fact can be reported in a filing that submissions.json did not list. The
# stub keeps the foreign key satisfiable; DO NOTHING means real submissions
# data always wins over it.
STUB_FILING = """
INSERT INTO filings (accession_no, cik, form_type, filed_date, is_amendment)
VALUES (%(accession_no)s, %(cik)s, %(form_type)s, %(filed_date)s,
        %(is_amendment)s)
ON CONFLICT (accession_no) DO NOTHING
"""

COPY_COLUMNS = (
    "cik",
    "accession_no",
    "taxonomy",
    "tag",
    "unit",
    "value",
    "period_start",
    "period_end",
    "duration_days",
    "period_type",
    "form",
    "filed_fy",
    "filed_fp",
)


@dataclass(frozen=True)
class FactIngestResult:
    rows_staged: int
    rows_skipped: int
    stub_filings: int


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    return date.fromisoformat(value)


def _fiscal_period(raw: Any) -> str | None:
    """companyfacts "fp" is the FILING's period, kept only as provenance.

    It is stored raw and never used as the fact's own fiscal period: a 10-K
    tags its prior-year comparatives with fp='FY' too.
    """
    if not isinstance(raw, str) or not raw:
        return None
    return raw.upper()


def ingest_company_facts(
    conn: psycopg.Connection,
    *,
    cik: str,
    company_facts: dict[str, Any],
) -> FactIngestResult:
    rows: list[tuple[Any, ...]] = []
    skipped = 0
    filings_seen: dict[str, tuple[str, date | None]] = {}

    for taxonomy, tags in company_facts.get("facts", {}).items():
        for tag, body in tags.items():
            for unit, entries in body.get("units", {}).items():
                for entry in entries:
                    value = entry.get("val")
                    accession_no = entry.get("accn")
                    raw_end = entry.get("end")
                    if not isinstance(value, (int, float)) or isinstance(value, bool):
                        skipped += 1
                        continue
                    if not accession_no or not raw_end:
                        skipped += 1
                        continue

                    period_end = _parse_date(raw_end)
                    period_start = _parse_date(entry.get("start"))
                    assert period_end is not None
                    form = entry.get("form") or "UNKNOWN"
                    filings_seen.setdefault(
                        accession_no, (form, _parse_date(entry.get("filed")))
                    )
                    rows.append(
                        (
                            cik,
                            accession_no,
                            taxonomy,
                            tag,
                            unit,
                            value,
                            period_start,
                            period_end,
                            duration_days(period_start, period_end),
                            classify_period(period_start, period_end),
                            form,
                            entry.get("fy"),
                            _fiscal_period(entry.get("fp")),
                        )
                    )

    with conn.cursor() as cur:
        for accession_no, (form, filed) in filings_seen.items():
            cur.execute(
                STUB_FILING,
                {
                    "accession_no": accession_no,
                    "cik": cik,
                    "form_type": form,
                    "filed_date": filed,
                    "is_amendment": form.endswith("/A"),
                },
            )
        cur.execute(STAGING_DDL)
        columns = ", ".join(COPY_COLUMNS)
        with cur.copy(f"COPY facts_staging ({columns}) FROM STDIN") as copy:
            for row in rows:
                copy.write_row(row)
        cur.execute(MERGE)

    return FactIngestResult(
        rows_staged=len(rows),
        rows_skipped=skipped,
        stub_filings=len(filings_seen),
    )
