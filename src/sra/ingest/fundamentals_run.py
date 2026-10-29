"""Ingest NSE/BSE fundamental research from Upstox's Fundamentals API."""

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from sra.db import connect
from sra.upstox.client import UpstoxAuthError, UpstoxClient, UpstoxError
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
  (instrument_key, statement, time_period, category, period, period_end, value,
   fetched_at)
VALUES
  (%(instrument_key)s, %(statement)s, %(time_period)s, %(category)s, %(period)s,
   %(period_end)s, %(value)s, %(fetched_at)s)
ON CONFLICT (instrument_key, statement, time_period, category, period) DO UPDATE
SET value = EXCLUDED.value,
    period_end = EXCLUDED.period_end,
    fetched_at = EXCLUDED.fetched_at
"""

# --resume skips a listing whose ratios were stored within this window, so a
# stopped full-universe run continues instead of starting over.
RESUME_WINDOW_SECONDS = 3600

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")  # fmt: skip
_PERIOD_LABEL = re.compile(r"^([A-Z][a-z]{2}) ([0-9]{4})$")


@dataclass(frozen=True)
class FundamentalsIngestReport:
    ticker: str
    ratios: int
    financials: int
    error: str | None = None
    skipped: bool = False


ProgressCallback = Callable[[int, int, FundamentalsIngestReport], None]


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


def _period_end(label: str) -> date | None:
    """'Mar 2026' -> 2026-03-01 (first of the month, for ordering); else None.

    Matches the migration's backfill. Month names are looked up rather than
    parsed with strptime, whose %b depends on the process locale.
    """
    match = _PERIOD_LABEL.match(label)
    if match is None or match.group(1) not in _MONTHS:
        return None
    return date(int(match.group(2)), _MONTHS.index(match.group(1)) + 1, 1)


def _data(payload: Any, expected: type[list[Any]] | type[dict[str, Any]]) -> Any:
    """The payload's "data" member, or an empty one when it is null/absent.

    Upstox can answer 200 with `"data": null`; `.get("data", {})` would not
    default that (nor an empty list where an object is expected). A body of
    the wrong shape is an error for this ticker.
    """
    if not isinstance(payload, dict):
        raise ValueError(f"unexpected response body: {type(payload).__name__}")
    data = payload.get("data")
    if not data:
        return expected()
    if not isinstance(data, expected):
        raise ValueError(f"unexpected data member: {type(data).__name__}")
    return data


def _ratio_rows(payload: Any) -> list[dict[str, Any]]:
    return [
        {
            "name": str(entry["name"]),
            "company_value": _parse_ratio_value(entry.get("company_value")),
            "sector_value": _parse_ratio_value(entry.get("sector_value")),
        }
        for entry in _data(payload, list)
        # An entry with no name would be stored under the literal name 'None'.
        if isinstance(entry, dict) and entry.get("name")
    ]


def _financial_row(
    statement: str, time_period: str, category: str, period: str, value: Any
) -> dict[str, Any]:
    return {
        "statement": statement,
        "time_period": time_period,
        "category": category,
        "period": period,
        "period_end": _period_end(period),
        "value": value,
    }


def _income_statement_rows(payload: Any, *, time_period: str) -> list[dict[str, Any]]:
    rows = []
    for entry in _data(payload, dict).get("income_statement") or []:
        if not isinstance(entry, dict) or not entry.get("category"):
            continue
        category = str(entry["category"])
        for point in entry.get("history") or []:
            if isinstance(point, dict) and point.get("period"):
                rows.append(
                    _financial_row(
                        "income_statement",
                        time_period,
                        category,
                        str(point["period"]),
                        point.get("value"),
                    )
                )
    return rows


def _balance_sheet_rows(payload: Any, *, time_period: str) -> list[dict[str, Any]]:
    # Unlike income-statement, Upstox returns balance-sheet history as one
    # flat list of {period, total_asset, total_liability} rather than a list
    # keyed by category -- pivot it into the same (category, period, value)
    # shape so both statements share one table.
    rows = []
    for point in _data(payload, dict).get("history") or []:
        if not isinstance(point, dict) or not point.get("period"):
            continue
        period = str(point["period"])
        for category in ("total_asset", "total_liability"):
            if point.get(category) is not None:
                rows.append(
                    _financial_row(
                        "balance_sheet",
                        time_period,
                        category,
                        period,
                        point[category],
                    )
                )
    return rows


def ingest_fundamentals(
    tickers: list[str] | None = None,
    *,
    all_equities: bool = False,
    resume: bool = False,
    on_progress: ProgressCallback | None = None,
) -> list[FundamentalsIngestReport]:
    """Fetch key ratios and income-statement/balance-sheet history for each
    requested ticker and upsert them. Safe to re-run.

    Full-universe refreshes are opt-in because Upstox is queried separately
    for every company and a complete NSE/BSE run can take substantial time.
    With ``resume=True``, a listing with a recently stored ratios response is
    skipped. Each completed listing is committed independently, so a stopped
    full-universe job can continue without redoing its completed work.

    A failure on one ticker (bad payload, Upstox error in ``--all`` mode) is
    recorded on its report. An expired token (``UpstoxAuthError``) is not:
    every remaining ticker would fail the same way, so the run aborts.
    """
    if tickers and all_equities:
        raise ValueError("choose explicit tickers or all_equities, not both")
    with connect(readonly=True) as lookup_conn:
        if all_equities:
            ticker_rows = lookup_conn.execute(
                "SELECT DISTINCT ticker FROM instruments ORDER BY ticker"
            ).fetchall()
            requested = [str(row["ticker"]) for row in ticker_rows]
            if not requested:
                raise UnknownInstrumentError(
                    "no NSE/BSE instruments are stored; run "
                    "`sra upstox ingest-instruments --all` first"
                )
        else:
            requested = list(tickers or [])
            if not requested:
                raise ValueError("provide one or more symbols, or use `--all`")
        rows = lookup_conn.execute(
            """SELECT DISTINCT ON (ticker) ticker, instrument_key
               FROM instruments WHERE ticker = ANY(%s)
               ORDER BY ticker,
                 CASE exchange WHEN 'NSE_EQ' THEN 0 WHEN 'BSE_EQ' THEN 1 ELSE 2 END""",
            (requested,),
        ).fetchall()
    resolved = {str(row["ticker"]): str(row["instrument_key"]) for row in rows}
    missing = [t for t in requested if t not in resolved]
    if missing:
        raise UnknownInstrumentError(
            "not in the instruments table "
            f"(run `sra upstox ingest-instruments` first): {', '.join(missing)}"
        )

    recently_completed: set[str] = set()
    if resume:
        cutoff = datetime.now(UTC).timestamp() - RESUME_WINDOW_SECONDS
        with connect(readonly=True) as lookup_conn:
            recent_rows = lookup_conn.execute(
                """SELECT DISTINCT i.ticker
                   FROM fundamental_ratios fr
                   JOIN instruments i ON i.instrument_key = fr.instrument_key
                   WHERE fr.fetched_at >= to_timestamp(%s)""",
                (cutoff,),
            ).fetchall()
        recently_completed = {str(row["ticker"]) for row in recent_rows}

    reports: list[FundamentalsIngestReport] = []
    total = len(requested)

    def record(position: int, report: FundamentalsIngestReport) -> None:
        reports.append(report)
        if on_progress:
            on_progress(position, total, report)

    with UpstoxClient() as client:
        for position, ticker in enumerate(requested, start=1):
            if ticker in recently_completed:
                record(position, FundamentalsIngestReport(ticker, 0, 0, skipped=True))
                continue
            instrument_key = resolved[ticker]
            isin = instrument_key.split("|", 1)[1]
            fetched_at = datetime.now(UTC)
            try:
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
            except UpstoxAuthError:
                raise
            except UpstoxError as exc:
                if not all_equities:
                    raise
                record(position, FundamentalsIngestReport(ticker, 0, 0, error=str(exc)))
                continue
            except (ValueError, TypeError, AttributeError, KeyError) as exc:
                # A non-JSON body (JSONDecodeError is a ValueError) or an
                # unexpected payload shape: this listing failed, not the run.
                record(
                    position,
                    FundamentalsIngestReport(
                        ticker, 0, 0, error=f"bad response: {exc}"
                    ),
                )
                continue

            with connect() as conn:
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
            record(
                position,
                FundamentalsIngestReport(ticker, len(ratio_rows), len(financial_rows)),
            )
    return reports
