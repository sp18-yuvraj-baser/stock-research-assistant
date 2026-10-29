"""The NSE/BSE cash-equity universe: which listing a symbol or name means.

Two sources, one set of rules. At ingest time the Upstox instrument master
(one bulk file of every tradable instrument) is filtered to plain cash
equities and stored in the `instruments` table. At answer time every lookup
reads that table instead, so a chat question never depends on the bulk file
being reachable. Both directions prefer the NSE listing and fall back to BSE
only when NSE lacks the symbol, so a quote, a holding review and an ingest
run all agree on which listing a symbol means.
"""

import gzip
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from sra.config import settings
from sra.db import connect
from sra.upstox.client import UpstoxError

INSTRUMENT_MASTER_URL = (
    "https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz"
)
CACHE_TTL_SECONDS = 24 * 60 * 60


class UnknownInstrumentError(LookupError):
    pass


@dataclass(frozen=True)
class EquityInstrument:
    """A cash-equity listing that can be quoted and researched."""

    ticker: str
    instrument_key: str
    exchange: str
    name: str


def fetch_instrument_master(
    *, cache_dir: Path | None = None, refresh: bool = False
) -> list[dict[str, Any]]:
    """Download, or reuse a same-day cached copy of, the full instrument list."""
    cache_dir = cache_dir or settings().cache_dir
    path = cache_dir / "upstox_instruments.json.gz"
    stale = (
        not path.exists() or (time.time() - path.stat().st_mtime) > CACHE_TTL_SECONDS
    )
    if refresh or stale:
        try:
            response = httpx.get(
                INSTRUMENT_MASTER_URL,
                timeout=httpx.Timeout(60.0),
                follow_redirects=True,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise UpstoxError(
                f"could not download the instrument master: {exc}"
            ) from exc
        # Parse before touching the cache so a truncated or non-gzip body can
        # never replace a good copy (and then look fresh for a whole TTL).
        try:
            records = list(json.loads(gzip.decompress(response.content)))
        except (OSError, EOFError, ValueError) as exc:
            raise UpstoxError(
                f"instrument master download was not valid: {exc}"
            ) from exc
        _write_atomically(path, response.content)
        return records
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return list(json.load(f))


def _write_atomically(path: Path, data: bytes) -> None:
    """Write via a temp file in the same directory and rename into place, so a
    crash or full disk mid-write leaves the previous cache file intact."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _cash_equity(record: dict[str, Any]) -> EquityInstrument | None:
    """The listing a master record describes, if it is a plain cash equity.

    Upstox keys the exchange+series as `segment` ('NSE_EQ', 'BSE_EQ') and the
    series itself as `instrument_type`; both must be checked, because
    `segment` alone also matches SME, odd-lot and other non-equity series.
    Kept, exactly: NSE 'EQ' and BSE 'A'. Trade-for-trade NSE 'BE' and every
    other BSE group (B, T, Z, X, XT, ...) are deliberately excluded.
    """
    segment = str(record.get("segment") or "")
    instrument_type = str(record.get("instrument_type") or "")
    if (segment, instrument_type) not in {("NSE_EQ", "EQ"), ("BSE_EQ", "A")}:
        return None
    ticker = str(record.get("trading_symbol") or "").upper()
    instrument_key = str(record.get("instrument_key") or "")
    if not ticker or not instrument_key:
        return None
    return EquityInstrument(
        ticker=ticker,
        instrument_key=instrument_key,
        exchange=segment,
        name=str(record.get("name") or ticker),
    )


def list_indian_equities(*, refresh: bool = False) -> list[EquityInstrument]:
    """Every plain NSE/BSE cash equity in the instrument master, one per
    (ticker, segment), sorted for a stable sync order."""
    equities: dict[tuple[str, str], EquityInstrument] = {}
    for record in fetch_instrument_master(refresh=refresh):
        equity = _cash_equity(record)
        if equity is not None:
            equities[(equity.ticker, equity.exchange)] = equity
    return sorted(equities.values(), key=lambda item: (item.exchange, item.ticker))


def resolve_instruments(
    tickers: list[str], *, segment: str = "NSE_EQ", refresh: bool = False
) -> dict[str, EquityInstrument]:
    """Map trading symbols to their cash-equity listing in one segment.

    Raises on any unresolved ticker so a typo in an ingest run fails loudly
    instead of silently tracking fewer tickers than asked for.
    """
    by_symbol = {
        equity.ticker: equity
        for equity in list_indian_equities(refresh=refresh)
        if equity.exchange == segment
    }
    resolved: dict[str, EquityInstrument] = {}
    unknown: list[str] = []
    for ticker in tickers:
        key = ticker.upper()
        if key in by_symbol:
            resolved[key] = by_symbol[key]
        else:
            unknown.append(ticker)
    if unknown:
        raise UnknownInstrumentError(f"no Upstox instrument for: {', '.join(unknown)}")
    return resolved


def _segment(exchange: str) -> str:
    upper = exchange.upper()
    return f"{upper}_EQ" if upper in {"NSE", "BSE"} else upper


def lookup_instrument(ticker: str, *, exchange: str | None = None) -> EquityInstrument:
    """The stored listing for a symbol: NSE first, BSE only when NSE lacks it.

    Raises UnknownInstrumentError when the symbol is not in the synced
    universe, and lets psycopg.Error through for the caller to report.
    """
    with connect(readonly=True) as conn:
        row = conn.execute(
            "SELECT ticker, instrument_key, exchange, name FROM instruments "
            "WHERE ticker = upper(%(ticker)s) "
            "AND (%(segment)s::text IS NULL OR exchange = %(segment)s) "
            "ORDER BY exchange = 'NSE_EQ' DESC, exchange LIMIT 1",
            {"ticker": ticker, "segment": _segment(exchange) if exchange else None},
        ).fetchone()
    if row is None:
        raise UnknownInstrumentError(
            f"{ticker!r} is not in the synced NSE/BSE equity universe"
        )
    return EquityInstrument(
        str(row["ticker"]),
        str(row["instrument_key"]),
        str(row["exchange"]),
        str(row["name"]),
    )


def match_instruments(
    *, tickers: list[str], names: list[str], prefixes: list[str]
) -> list[EquityInstrument]:
    """Stored listings whose symbol is in `tickers`, whose upper-cased name is
    in `names`, or whose name matches a LIKE pattern in `prefixes`.

    One result per symbol. The name is the one that matched (NSE and BSE spell
    names differently, and callers compare against it), while the key and
    exchange are always the preferred listing, NSE first. Lets psycopg.Error
    through.
    """
    with connect(readonly=True) as conn:
        rows = conn.execute(
            "SELECT DISTINCT ON (m.ticker) m.ticker, m.name, "
            "p.instrument_key, p.exchange "
            "FROM instruments m CROSS JOIN LATERAL ("
            "  SELECT instrument_key, exchange FROM instruments p "
            "  WHERE p.ticker = m.ticker "
            "  ORDER BY p.exchange = 'NSE_EQ' DESC, p.exchange LIMIT 1"
            ") p "
            "WHERE m.ticker = ANY(%(tickers)s) OR upper(m.name) = ANY(%(names)s) "
            "OR upper(m.name) LIKE ANY(%(prefixes)s) "
            "ORDER BY m.ticker, m.exchange = 'NSE_EQ' DESC, m.exchange",
            {"tickers": tickers, "names": names, "prefixes": prefixes},
        ).fetchall()
    return [
        EquityInstrument(
            str(r["ticker"]),
            str(r["instrument_key"]),
            str(r["exchange"]),
            str(r["name"]),
        )
        for r in rows
    ]
