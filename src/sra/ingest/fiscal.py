import psycopg

# Assign each fact the fiscal label its own filer uses for that period.
#
# companyfacts reports fy/fp at the FILING level: every fact in Nvidia's FY2026
# 10-K carries fy=2026, fp=FY, including the FY2025 and FY2024 comparatives.
# Copying those straight onto the fact is the single easiest way to answer
# "revenue in FY2025" with the wrong year's number.
#
# Instead, anchor on period_of_report. A 10-K or 10-Q states the one period it
# is about, and every fact whose period_end equals that date belongs to that
# filing's fiscal year and period. Comparatives get their label from the earlier
# filing that was actually about them. No assumption is made about December
# year-ends, about which calendar year a fiscal year is named after, or about
# quarters falling on calendar boundaries.
DERIVE_FISCAL_LABELS = """
WITH anchors AS (
    SELECT DISTINCT ON (f.cik, fl.period_of_report)
           f.cik,
           fl.period_of_report AS period_end,
           f.filed_fy,
           f.filed_fp
    FROM facts f
    JOIN filings fl ON fl.accession_no = f.accession_no
    WHERE fl.form_type IN ('10-K', '10-Q', '10-K/A', '10-Q/A')
      AND fl.period_of_report IS NOT NULL
      AND f.filed_fy IS NOT NULL
      AND f.filed_fp IS NOT NULL
    -- Earliest filing wins: that is the original report for the period, and an
    -- amendment of it carries the same fiscal label anyway.
    ORDER BY f.cik, fl.period_of_report, fl.filed_date, f.accession_no
)
UPDATE facts f
SET fiscal_year = a.filed_fy,
    fiscal_period = a.filed_fp
FROM anchors a
WHERE f.cik = a.cik
  AND f.period_end = a.period_end
  AND (f.fiscal_year IS DISTINCT FROM a.filed_fy
       OR f.fiscal_period IS DISTINCT FROM a.filed_fp)
"""


def derive_fiscal_labels(conn: psycopg.Connection, *, cik: str | None = None) -> int:
    """Populate facts.fiscal_year / fiscal_period. Idempotent."""
    sql = DERIVE_FISCAL_LABELS
    params: dict[str, str] = {}
    if cik is not None:
        sql = f"{sql}\n  AND f.cik = %(cik)s"
        params["cik"] = cik
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.rowcount
