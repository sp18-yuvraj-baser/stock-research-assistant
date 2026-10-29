import logging
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import psycopg

from sra.db import connect
from sra.upstox.client import (
    UpstoxAuthError,
    UpstoxError,
    reset_shared_client,
    shared_client,
)
from sra.upstox.instruments import UnknownInstrumentError, lookup_instrument

log = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")


def ist_time(stamp: object) -> str:
    """A datetime or ISO string as Indian time, e.g. '29 Oct 2026, 14:31 IST'.

    Market hours and every user here are in IST; a raw UTC ISO string makes
    the reader do timezone arithmetic just to see whether the market was open.
    """
    moment = (
        stamp if isinstance(stamp, datetime) else datetime.fromisoformat(str(stamp))
    )
    return moment.astimezone(IST).strftime("%d %b %Y, %H:%M IST")


class QuoteClient(Protocol):
    """Structural shape of UpstoxClient, so a test fake needs no inheritance."""

    def get_json(self, path: str, *, params: dict[str, str]) -> Any: ...


@dataclass(frozen=True)
class LiveQuote:
    ticker: str
    instrument_key: str
    exchange: str
    last_price: float | None
    fetched_at: str  # ISO 8601 UTC, set by this call -- never trusted from the payload
    error: str | None = None

    def to_text(self) -> str:
        if self.error:
            return f"ERROR: {self.error}"
        exchange = self.exchange.removesuffix("_EQ")
        return (
            f"{self.ticker} ({exchange}): last price {self.last_price} INR at "
            f"{ist_time(self.fetched_at)}. This is a live NSE/BSE quote from "
            "Upstox, valid only at that moment."
        )


def _log_quote(instrument_key: str, last_price: float | None, fetched_at: str) -> None:
    """Best-effort audit write; a logging failure is never a reason to
    withhold a live price the model already has."""
    try:
        with connect() as conn:
            conn.execute(
                "INSERT INTO quotes (instrument_key, fetched_at, last_price) "
                "VALUES (%s, %s, %s)",
                (instrument_key, fetched_at, last_price),
            )
            conn.commit()
    except psycopg.Error as exc:
        log.warning("could not log quote for %s: %s", instrument_key, exc)


def _price_from(payload: Any, instrument_key: str) -> float | None:
    """The finite last price for instrument_key, or None if the payload has none."""
    # Upstox keys the response by "SEGMENT:TRADING_SYMBOL" (e.g. "NSE_EQ:TCS"),
    # not by the instrument_key used to request it -- the instrument_key comes
    # back inside each entry as instrument_token instead. One instrument_key
    # is requested per call, so match on instrument_token rather than assume
    # a key format that isn't documented to be stable.
    entries = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(entries, dict):
        return None
    for entry in entries.values():
        if isinstance(entry, dict) and entry.get("instrument_token") == instrument_key:
            raw = entry.get("last_price")
            if raw is None:
                return None
            try:
                price = float(raw)
            except (TypeError, ValueError):
                return None
            return price if math.isfinite(price) else None
    return None


def get_live_quote(
    ticker: str, *, exchange: str | None = None, client: QuoteClient | None = None
) -> LiveQuote:
    """Fetch a live quote. Every failure comes back as LiveQuote.error, never
    as an exception, so one bad lookup cannot abort the answer being built."""
    fetched_at = datetime.now(UTC).isoformat(timespec="seconds")
    try:
        listing = lookup_instrument(ticker, exchange=exchange)
    except UnknownInstrumentError as exc:
        return LiveQuote(ticker, "", "", None, fetched_at, error=str(exc))
    except psycopg.Error as exc:
        return LiveQuote(
            ticker,
            "",
            "",
            None,
            fetched_at,
            error=f"could not look up the instrument: {str(exc).strip()}",
        )

    instrument_key, resolved_exchange = listing.instrument_key, listing.exchange
    try:
        payload = (client or shared_client()).get_json(
            "/v2/market-quote/ltp", params={"instrument_key": instrument_key}
        )
    except UpstoxError as exc:
        if isinstance(exc, UpstoxAuthError) and client is None:
            # The shared client holds the rejected token; rebuild it so the
            # next question reads the token a fresh `sra upstox login` wrote.
            reset_shared_client()
        return LiveQuote(
            ticker, instrument_key, resolved_exchange, None, fetched_at, error=str(exc)
        )

    last_price = _price_from(payload, instrument_key)
    if last_price is None:
        return LiveQuote(
            ticker,
            instrument_key,
            resolved_exchange,
            None,
            fetched_at,
            error=f"Upstox returned no usable price for {instrument_key}",
        )
    _log_quote(instrument_key, last_price, fetched_at)
    return LiveQuote(ticker, instrument_key, resolved_exchange, last_price, fetched_at)
