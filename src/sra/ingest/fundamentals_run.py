"""Ingest P/E, ROE and other ratios plus income-statement/balance-sheet
history for the tracked Nifty-50 universe, from Upstox's Fundamentals API.

One Postgres transaction per ticker, same as ingest/run.py: a failure
part-way through a 50-company run leaves the rest intact and the run
re-runnable.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import psycopg

from sra.config import settings
from sra.ingest.instruments_run import DEFAULT_NSE_TICKERS
from sra.upstox.client import UpstoxClient
from sra.upstox.instruments import UnknownInstrumentError

UPSERT_RATIO = """
INSERT INTO fundamental_ratios
  (instrument_key, name, company_value, sector_value, fetched_at)
VALUES
  (%(instrument_key)s, %(name)s, %(company_value)s, %(sector_value)s, %(fetched_at)s)
ON CONFLICT (instrument_key, name) DO UPDATE
SET company_value = EXCLUDED.company_value,
    sector_value = EXCLUDED.sector_value,
    fetched_at = EXCLUDED.fetched_at
"""

UPSERT_FINANCIAL = """
INSERT INTO fundamental_financials
  (instrument_key, statement, time_period, category, period, value, fetched_at)
VALUES
  (%(instrument_key)s, %(statement)s, %(time_period)s, %(category)s, %(period)s,
   %(value)s, %(fetched_at)s)
ON CONFLICT (instrument_key, statement, time_period, category, period) DO UPDATE
SET value = EXCLUDED.value, fetched_at = EXCLUDED.fetched_at
"""


@dataclass(frozen=True)
class FundamentalsIngestReport:
    ticker: str
    ratios: int
    financials: int


def _parse_ratio_value(raw: str | None) -> float | None:
    """'8.94%' -> 8.94, '20.15' -> 20.15, anything unparseable -> None.

    Key Ratios values come back as strings, some with an embedded '%' rather
    than a separate field -- a parsing bug here would silently corrupt every
    ratio, so this has its own unit test.
    """
    if raw is None:
        return None
    try:
        return float(raw.strip().rstrip("%"))
    except ValueError:
        return None


def _ratio_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "name": str(entry.get("name")),
            "company_value": _parse_ratio_value(entry.get("company_value")),
            "sector_value": _parse_ratio_value(entry.get("sector_value")),
        }
        for entry in payload.get("data", [])
    ]


def _income_statement_rows(
    payload: dict[str, Any], *, time_period: str
) -> list[dict[str, Any]]:
    rows = []
    for entry in payload.get("data", {}).get("income_statement", []):
        category = str(entry.get("category"))
        for point in entry.get("history", []):
            rows.append(
                {
                    "statement": "income_statement",
                    "time_period": time_period,
                    "category": category,
                    "period": str(point.get("period")),
                    "value": point.get("value"),
                }
            )
    return rows


def _balance_sheet_rows(
    payload: dict[str, Any], *, time_period: str
) -> list[dict[str, Any]]:
    # Unlike income-statement, Upstox returns balance-sheet history as one
    # flat list of {period, total_asset, total_liability} rather than a list
    # keyed by category -- pivot it into the same (category, period, value)
    # shape so both statements share one table.
    rows = []
    for point in payload.get("data", {}).get("history", []):
        period = str(point.get("period"))
        for category in ("total_asset", "total_liability"):
            if point.get(category) is not None:
                rows.append(
                    {
                        "statement": "balance_sheet",
                        "time_period": time_period,
                        "category": category,
                        "period": period,
                        "value": point.get(category),
                    }
                )
    return rows


def ingest_fundamentals(
    tickers: list[str] | None = None,
) -> list[FundamentalsIngestReport]:
    """Fetch key ratios and income-statement/balance-sheet history for each
    tracked ticker and upsert them. Safe to re-run."""
    requested = list(tickers) if tickers else list(DEFAULT_NSE_TICKERS)
    with psycopg.connect(settings().readonly_dsn) as lookup_conn:
        rows = lookup_conn.execute(
            "SELECT ticker, instrument_key FROM instruments WHERE ticker = ANY(%s)",
            (requested,),
        ).fetchall()
    resolved = {str(ticker): str(instrument_key) for ticker, instrument_key in rows}
    missing = [t for t in requested if t not in resolved]
    if missing:
        raise UnknownInstrumentError(
            "not in the instruments table "
            f"(run `sra upstox ingest-instruments` first): {', '.join(missing)}"
        )

    reports = []
    with UpstoxClient() as client:
        for ticker in requested:
            instrument_key = resolved[ticker]
            isin = instrument_key.split("|", 1)[1]
            fetched_at = datetime.now(UTC)

            ratio_rows = _ratio_rows(
                client.get_json(f"/v2/fundamentals/{isin}/key-ratios", params={})
            )
            financial_rows = _income_statement_rows(
                client.get_json(
                    f"/v2/fundamentals/{isin}/income-statement",
                    params={"type": "consolidated", "time_period": "yearly"},
                ),
                time_period="yearly",
            )
            financial_rows += _income_statement_rows(
                client.get_json(
                    f"/v2/fundamentals/{isin}/income-statement",
                    params={"type": "consolidated", "time_period": "quarterly"},
                ),
                time_period="quarterly",
            )
            financial_rows += _balance_sheet_rows(
                client.get_json(
                    f"/v2/fundamentals/{isin}/balance-sheet",
                    params={"type": "consolidated"},
                ),
                time_period="yearly",
            )

            with psycopg.connect(settings().dsn) as conn:
                for row in ratio_rows:
                    conn.execute(
                        UPSERT_RATIO,
                        {
                            "instrument_key": instrument_key,
                            "fetched_at": fetched_at,
                            **row,
                        },
                    )
                for row in financial_rows:
                    conn.execute(
                        UPSERT_FINANCIAL,
                        {
                            "instrument_key": instrument_key,
                            "fetched_at": fetched_at,
                            **row,
                        },
                    )
                conn.commit()
            reports.append(
                FundamentalsIngestReport(ticker, len(ratio_rows), len(financial_rows))
            )
    return reports
