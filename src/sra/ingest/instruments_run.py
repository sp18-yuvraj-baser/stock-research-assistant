"""Store NSE/BSE cash-equity listings from the Upstox instrument master."""

import logging
from dataclasses import dataclass
from typing import Any

from sra.db import connect
from sra.upstox.instruments import (
    EquityInstrument,
    list_indian_equities,
    resolve_instruments,
)

log = logging.getLogger(__name__)

# The master occasionally gives no name, and the ticker then stands in for
# it; that placeholder must never replace a real name already stored.
UPSERT_INSTRUMENT = """
INSERT INTO instruments (instrument_key, ticker, exchange, name)
VALUES (%(instrument_key)s, %(ticker)s, %(exchange)s, %(name)s)
ON CONFLICT (instrument_key) DO UPDATE
SET ticker = EXCLUDED.ticker,
    exchange = EXCLUDED.exchange,
    name = CASE WHEN EXCLUDED.name = EXCLUDED.ticker
                THEN instruments.name ELSE EXCLUDED.name END
"""


@dataclass(frozen=True)
class SkippedInstrument:
    ticker: str
    exchange: str
    instrument_key: str


@dataclass(frozen=True)
class InstrumentSyncResult:
    synced: list[EquityInstrument]
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


def _store(equities: list[EquityInstrument]) -> InstrumentSyncResult:
    """Upsert listings in one transaction, skipping (ticker, exchange)
    conflicts instead of crashing on them. Safe to re-run."""
    with connect() as conn:
        held = {
            (str(row["ticker"]), str(row["exchange"])): str(row["instrument_key"])
            for row in conn.execute(
                "SELECT instrument_key, ticker, exchange FROM instruments"
            ).fetchall()
        }
        keep, skipped = split_conflicting(equities, held)
        rows: list[dict[str, Any]] = [
            {
                "instrument_key": equity.instrument_key,
                "ticker": equity.ticker,
                "exchange": equity.exchange,
                "name": equity.name,
            }
            for equity in keep
        ]
        with conn.cursor() as cursor:
            cursor.executemany(UPSERT_INSTRUMENT, rows)
        conn.commit()
    for item in skipped:
        log.warning(
            "skipped %s (%s): ticker already stored under another instrument key",
            item.ticker,
            item.exchange,
        )
    log.info("stored %d listing(s), skipped %d", len(keep), len(skipped))
    return InstrumentSyncResult(keep, skipped)


def ingest_instruments(
    tickers: list[str] | None = None, *, refresh: bool = False
) -> InstrumentSyncResult:
    """Resolve named NSE tickers from the master and store them."""
    requested = list(tickers or [])
    if not requested:
        raise ValueError("provide one or more symbols, or use `--all`")
    return _store(list(resolve_instruments(requested, refresh=refresh).values()))


def ingest_all_indian_equities(*, refresh: bool = False) -> InstrumentSyncResult:
    """Store the complete NSE/BSE cash-equity universe from Upstox.

    The master is authoritative for membership; rerunning updates stored
    listings and adds new ones without deleting history.
    """
    return _store(list_indian_equities(refresh=refresh))
