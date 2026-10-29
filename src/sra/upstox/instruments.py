"""Resolve human tickers to Upstox instrument keys via the instrument master.

Upstox publishes one bulk file listing every tradable instrument on NSE/BSE.
Unlike a live quote, this list changes rarely, so it is safe to cache with a
TTL -- the opposite caching decision from client.py's live-quote calls. Used
only at ingest time: answering a chat question resolves ticker -> instrument
key from the already-populated `instruments` table instead (see
tools/get_live_quote.py), so it never depends on this file being reachable.
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


def resolve_instruments(
    tickers: list[str], *, segment: str = "NSE_EQ", refresh: bool = False
) -> dict[str, tuple[str, str]]:
    """Map trading symbols to (instrument_key, segment).

    Mirrors sec.tickers.resolve_tickers: raises on any unresolved ticker so a
    typo in an ingest run fails loudly instead of silently tracking fewer
    tickers than asked for.

    Upstox's instrument master keys the exchange+series as `segment` (e.g.
    'NSE_EQ', 'BSE_EQ'), separate from the plain `exchange` field ('NSE',
    'BSE') and from `instrument_type`, which is a series code -- plain
    equities are 'EQ', while index/derivative/other-series rows use dozens of
    other codes. Both must be checked: `segment` alone also matches
    non-equity series such as SME or odd-lot rows.

    Series kept: NSE 'EQ' (segment NSE_EQ) and BSE 'A' (segment BSE_EQ) only.
    Trade-for-trade NSE 'BE' and every other BSE group (B, T, Z, X, XT, ...)
    are deliberately excluded, so a ticker that trades only in one of those
    series raises UnknownInstrumentError instead of resolving.
    """
    records = fetch_instrument_master(refresh=refresh)
    by_symbol = {
        str(r["trading_symbol"]).upper(): (str(r["instrument_key"]), str(r["segment"]))
        for r in records
        # NSE calls its plain equity series "EQ"; BSE instrument-master
        # records use "A" for the comparable listed-equity series.
        if r.get("segment") == segment and r.get("instrument_type") in {"EQ", "A"}
    }
    resolved: dict[str, tuple[str, str]] = {}
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


def list_indian_equities(*, refresh: bool = False) -> list[EquityInstrument]:
    """Return every plain NSE/BSE equity in the Upstox instrument master.

    Derivatives, indices, SME/odd-lot series, and duplicate master records are
    excluded so the local coverage table is a stable cash-equity universe.

    Series kept, exactly: NSE 'EQ' (segment NSE_EQ) and BSE 'A' (segment
    BSE_EQ). Trade-for-trade NSE 'BE' and all other BSE groups (B, T, Z, X,
    XT, ...) are deliberately excluded, so securities listed only in those
    series are absent from the universe. Duplicates are collapsed per
    (ticker, segment).
    """
    records = fetch_instrument_master(refresh=refresh)
    equities: dict[tuple[str, str], EquityInstrument] = {}
    for record in records:
        segment = str(record.get("segment") or "")
        instrument_type = str(record.get("instrument_type") or "")
        is_nse_equity = segment == "NSE_EQ" and instrument_type == "EQ"
        is_bse_equity = segment == "BSE_EQ" and instrument_type == "A"
        if not (is_nse_equity or is_bse_equity):
            continue
        ticker = str(record.get("trading_symbol") or "").upper()
        instrument_key = str(record.get("instrument_key") or "")
        if not ticker or not instrument_key:
            continue
        equities[(ticker, segment)] = EquityInstrument(
            ticker=ticker,
            instrument_key=instrument_key,
            exchange=segment,
            name=str(record.get("name") or ticker),
        )
    return sorted(equities.values(), key=lambda item: (item.exchange, item.ticker))
