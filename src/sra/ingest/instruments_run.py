from dataclasses import dataclass

import psycopg

from sra.config import settings
from sra.upstox.instruments import resolve_instruments

# Nifty 50 large-caps (best-effort snapshot; index composition drifts, and
# resolve_instruments already raises loudly on anything that no longer
# resolves, so drift is caught at ingest time rather than silently).
# Tata Motors demerged into two listed entities in 2025: TMPV (passenger
# vehicles / JLR / EVs) is used here as "Tata Motors" since that's the
# business most questions mean, over TMCV (commercial vehicles).
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


@dataclass(frozen=True)
class InstrumentIngestReport:
    ticker: str
    instrument_key: str
    exchange: str


def ingest_instruments(
    tickers: list[str] | None = None, *, refresh: bool = False
) -> list[InstrumentIngestReport]:
    """Resolve NSE tickers to Upstox instrument keys and upsert them. Safe to re-run."""
    requested = list(tickers) if tickers else list(DEFAULT_NSE_TICKERS)
    resolved = resolve_instruments(requested, refresh=refresh)
    reports = []
    with psycopg.connect(settings().dsn) as conn:
        for ticker, (instrument_key, exchange) in resolved.items():
            conn.execute(
                UPSERT_INSTRUMENT,
                {
                    "instrument_key": instrument_key,
                    "ticker": ticker,
                    "exchange": exchange,
                    "name": ticker,
                },
            )
            reports.append(InstrumentIngestReport(ticker, instrument_key, exchange))
        conn.commit()
    return reports
