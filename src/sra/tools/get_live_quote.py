import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import psycopg

from sra.db import connect
from sra.upstox.client import UpstoxAuthError, UpstoxError, reset_shared_client
from sra.upstox.instruments import UnknownInstrumentError, lookup_instrument
from sra.upstox.market import FullQuote, full_quotes

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
    # Today's session so far: change, range, volume, circuit limits.
    details: FullQuote | None = None

    def to_text(self) -> str:
        if self.error:
            return f"ERROR: {self.error}"
        exchange = self.exchange.removesuffix("_EQ")
        lines = [
            f"{self.ticker} ({exchange}): last price {self.last_price} INR at "
            f"{ist_time(self.fetched_at)}. This is a live NSE/BSE quote from "
            "Upstox, valid only at that moment."
        ]
        quote = self.details
        if quote is not None:
            if quote.net_change is not None and quote.change_pct is not None:
                lines.append(
                    f"Change today: {quote.net_change:+.2f} INR "
                    f"({quote.change_pct:+.2f}%) from previous close "
                    f"{quote.previous_close:.2f} INR."
                )
            if quote.low is not None and quote.high is not None:
                lines.append(f"Day range: {quote.low} to {quote.high} INR.")
            if quote.volume is not None:
                lines.append(f"Volume today: {quote.volume:,} shares.")
            if quote.lower_circuit is not None and quote.upper_circuit is not None:
                lines.append(
                    f"Circuit limits: {quote.lower_circuit:.2f} to "
                    f"{quote.upper_circuit:.2f} INR."
                )
        return "\n".join(lines)


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
        quote = full_quotes([instrument_key], client=client).get(instrument_key)
    except UpstoxError as exc:
        if isinstance(exc, UpstoxAuthError) and client is None:
            # The shared client holds the rejected token; rebuild it so the
            # next question reads the token a fresh `sra upstox login` wrote.
            reset_shared_client()
        return LiveQuote(
            ticker, instrument_key, resolved_exchange, None, fetched_at, error=str(exc)
        )

    if quote is None:
        return LiveQuote(
            ticker,
            instrument_key,
            resolved_exchange,
            None,
            fetched_at,
            error=f"Upstox returned no usable price for {instrument_key}",
        )
    _log_quote(instrument_key, quote.last_price, fetched_at)
    return LiveQuote(
        ticker,
        instrument_key,
        resolved_exchange,
        quote.last_price,
        fetched_at,
        details=quote,
    )
