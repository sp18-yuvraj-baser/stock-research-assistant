from dataclasses import dataclass

from sra.db import connect
from sra.upstox.instruments import (
    EquityInstrument,
    list_indian_equities,
    resolve_instruments,
)

# Backward-compatible explicit-symbol sample for API users. The CLI does not
# default to this list: use `--all` to sync the complete NSE/BSE universe.
DEFAULT_NSE_TICKERS = (
    "RELIANCE",
    "TCS",
    "HDFCBANK",
    "ICICIBANK",
    "INFY",
    "HINDUNILVR",
    "ITC",
    "SBIN",
    "BHARTIARTL",
    "BAJFINANCE",
    "KOTAKBANK",
    "LT",
    "HCLTECH",
    "AXISBANK",
    "MARUTI",
    "SUNPHARMA",
    "ASIANPAINT",
    "TITAN",
    "ULTRACEMCO",
    "WIPRO",
    "NESTLEIND",
    "ADANIENT",
    "ADANIPORTS",
    "TMPV",
    "TATASTEEL",
    "POWERGRID",
    "NTPC",
    "M&M",
    "BAJAJFINSV",
    "JSWSTEEL",
    "TECHM",
    "HDFCLIFE",
    "SBILIFE",
    "GRASIM",
    "CIPLA",
    "DRREDDY",
    "BRITANNIA",
    "EICHERMOT",
    "COALINDIA",
    "DIVISLAB",
    "APOLLOHOSP",
    "HEROMOTOCO",
    "BPCL",
    "ONGC",
    "SHRIRAMFIN",
    "INDUSINDBK",
    "TATACONSUM",
    "UPL",
    "HINDALCO",
    "BAJAJ-AUTO",
    "TRENT",
    "JIOFIN",
)

UPSERT_INSTRUMENT = """
INSERT INTO instruments (instrument_key, ticker, exchange, name)
VALUES (%(instrument_key)s, %(ticker)s, %(exchange)s, %(name)s)
ON CONFLICT (instrument_key) DO UPDATE
SET ticker = EXCLUDED.ticker, exchange = EXCLUDED.exchange, name = EXCLUDED.name
"""

# Explicit-ticker path: the incoming name is only a placeholder (the ticker)
# when the master gave none, so never let it replace a stored real name.
UPSERT_INSTRUMENT_KEEP_NAME = """
INSERT INTO instruments (instrument_key, ticker, exchange, name)
VALUES (%(instrument_key)s, %(ticker)s, %(exchange)s, %(name)s)
ON CONFLICT (instrument_key) DO UPDATE
SET ticker = EXCLUDED.ticker,
    exchange = EXCLUDED.exchange,
    name = CASE WHEN EXCLUDED.name = EXCLUDED.ticker
                THEN instruments.name ELSE EXCLUDED.name END
"""


@dataclass(frozen=True)
class InstrumentIngestReport:
    ticker: str
    instrument_key: str
    exchange: str


def ingest_instruments(
    tickers: list[str] | None = None, *, refresh: bool = False
) -> list[InstrumentIngestReport]:
    """Resolve named NSE/BSE tickers and upsert them. Safe to re-run."""
    requested = list(tickers or [])
    if not requested:
        raise ValueError("provide one or more symbols, or use `--all`")
    resolved = resolve_instruments(requested, refresh=refresh)
    # resolve_instruments returns no company name; take it from the master so a
    # new listing is stored with its real name, falling back to the ticker.
    names = {e.instrument_key: e.name for e in list_indian_equities()}
    reports = []
    with connect() as conn:
        for ticker, (instrument_key, exchange) in resolved.items():
            conn.execute(
                UPSERT_INSTRUMENT_KEEP_NAME,
                {
                    "instrument_key": instrument_key,
                    "ticker": ticker,
                    "exchange": exchange,
                    "name": names.get(instrument_key, ticker),
                },
            )
            reports.append(InstrumentIngestReport(ticker, instrument_key, exchange))
        conn.commit()
    return reports


@dataclass(frozen=True)
class SkippedInstrument:
    ticker: str
    exchange: str
    instrument_key: str


@dataclass(frozen=True)
class InstrumentSyncResult:
    synced: int
    skipped: list[SkippedInstrument]


def split_conflicting(
    equities: list[EquityInstrument], held: dict[tuple[str, str], str]
) -> tuple[list[EquityInstrument], list[SkippedInstrument]]:
    """Separate rows that would break the unique (ticker, exchange) index.

    `held` maps (ticker, exchange) to the instrument_key already stored. A row
    is skipped when its (ticker, exchange) belongs to a different key, either
    in the table or to an earlier row of this same batch. Existing rows are
    never rekeyed: child tables reference instrument_key.
    """
    claimed = dict(held)
    keep: list[EquityInstrument] = []
    skipped: list[SkippedInstrument] = []
    for equity in equities:
        slot = (equity.ticker, equity.exchange)
        owner = claimed.setdefault(slot, equity.instrument_key)
        if owner == equity.instrument_key:
            keep.append(equity)
        else:
            skipped.append(
                SkippedInstrument(equity.ticker, equity.exchange, equity.instrument_key)
            )
    return keep, skipped


def ingest_all_indian_equities(*, refresh: bool = False) -> InstrumentSyncResult:
    """Store the complete NSE/BSE cash-equity universe from Upstox.

    The source master is authoritative for membership; rerunning this updates
    instruments already stored and adds new listings without deleting history.
    A listing whose (ticker, exchange) is already held by a different
    instrument_key is skipped and reported rather than rekeyed or crashed on.
    """
    equities = list_indian_equities(refresh=refresh)
    with connect() as conn:
        held = {
            (str(row["ticker"]), str(row["exchange"])): str(row["instrument_key"])
            for row in conn.execute(
                "SELECT instrument_key, ticker, exchange FROM instruments"
            ).fetchall()
        }
        keep, skipped = split_conflicting(equities, held)
        with conn.cursor() as cursor:
            cursor.executemany(
                UPSERT_INSTRUMENT,
                [
                    {
                        "instrument_key": equity.instrument_key,
                        "ticker": equity.ticker,
                        "exchange": equity.exchange,
                        "name": equity.name,
                    }
                    for equity in keep
                ],
            )
        conn.commit()
    return InstrumentSyncResult(len(keep), skipped)
