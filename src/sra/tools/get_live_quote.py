import logging
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

import psycopg

from sra.db import connect
from sra.upstox.client import UpstoxClient, UpstoxError
from sra.upstox.instruments import UnknownInstrumentError

log = logging.getLogger(__name__)


class QuoteClient(Protocol):
    """Structural shape of UpstoxClient, so a test fake needs no inheritance."""

    def __enter__(self) -> "QuoteClient": ...
    def __exit__(self, *exc: object) -> None: ...
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
        return (
            f"{self.ticker} ({self.instrument_key}, {self.exchange}): "
            f"last price {self.last_price} INR, fetched at {self.fetched_at} UTC. "
            "This is a live NSE/BSE quote from Upstox."
        )


def resolve_instrument_key(
    ticker: str, *, exchange: str | None = None
) -> tuple[str, str]:
    with connect(readonly=True) as conn:
        if exchange is None:
            row = conn.execute(
                "SELECT instrument_key, exchange FROM instruments "
                "WHERE ticker = upper(%s) "
                # NSE is the primary listing; BSE only when NSE lacks the symbol.
                "ORDER BY exchange = 'NSE_EQ' DESC, exchange LIMIT 1",
                (ticker,),
            ).fetchone()
        else:
            segment = (
                f"{exchange.upper()}_EQ"
                if exchange.upper() in {"NSE", "BSE"}
                else exchange.upper()
            )
            row = conn.execute(
                "SELECT instrument_key, exchange FROM instruments "
                "WHERE ticker = upper(%s) AND exchange = upper(%s)",
                (ticker, segment),
            ).fetchone()
    if row is None:
        raise UnknownInstrumentError(
            f"{ticker!r} is not in the synced NSE/BSE equity universe"
        )
    return str(row["instrument_key"]), str(row["exchange"])


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
        if exchange is None:
            instrument_key, resolved_exchange = resolve_instrument_key(ticker)
        else:
            instrument_key, resolved_exchange = resolve_instrument_key(
                ticker, exchange=exchange
            )
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

    try:
        with client or UpstoxClient() as c:
            payload = c.get_json(
                "/v2/market-quote/ltp", params={"instrument_key": instrument_key}
            )
    except UpstoxError as exc:
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
