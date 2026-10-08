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
import time
from pathlib import Path
from typing import Any

import httpx

from sra.config import settings

INSTRUMENT_MASTER_URL = (
    "https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz"
)
CACHE_TTL_SECONDS = 24 * 60 * 60


class UnknownInstrumentError(LookupError):
    pass


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
        response = httpx.get(
            INSTRUMENT_MASTER_URL, timeout=httpx.Timeout(60.0), follow_redirects=True
        )
        response.raise_for_status()
        cache_dir.mkdir(parents=True, exist_ok=True)
        path.write_bytes(response.content)
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return list(json.load(f))


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
    """
    records = fetch_instrument_master(refresh=refresh)
    by_symbol = {
        str(r["trading_symbol"]).upper(): (str(r["instrument_key"]), str(r["segment"]))
        for r in records
        if r.get("segment") == segment and r.get("instrument_type") == "EQ"
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
