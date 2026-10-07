from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

import psycopg
from psycopg.rows import dict_row

from sra.config import settings
from sra.upstox.client import UpstoxClient, UpstoxError
from sra.upstox.instruments import UnknownInstrumentError


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
            "This is a live quote, not a figure from a SEC filing."
        )


def resolve_instrument_key(ticker: str) -> tuple[str, str]:
    with psycopg.connect(settings().readonly_dsn, row_factory=dict_row) as conn:
        row = conn.execute(
            "SELECT instrument_key, exchange FROM instruments WHERE ticker = upper(%s)",
            (ticker,),
        ).fetchone()
    if row is None:
        raise UnknownInstrumentError(f"{ticker!r} is not a tracked NSE instrument")
    return str(row["instrument_key"]), str(row["exchange"])


def _log_quote(instrument_key: str, last_price: float | None, fetched_at: str) -> None:
    """Best-effort audit write; a logging failure is never a reason to
    withhold a live price the model already has."""
    try:
        with psycopg.connect(settings().dsn) as conn:
            conn.execute(
                "INSERT INTO quotes (instrument_key, fetched_at, last_price) "
                "VALUES (%s, %s, %s)",
                (instrument_key, fetched_at, last_price),
            )
            conn.commit()
    except psycopg.Error:
        pass


def get_live_quote(ticker: str, *, client: QuoteClient | None = None) -> LiveQuote:
    fetched_at = datetime.now(UTC).isoformat(timespec="seconds")
    try:
        instrument_key, exchange = resolve_instrument_key(ticker)
    except UnknownInstrumentError as exc:
        return LiveQuote(ticker, "", "", None, fetched_at, error=str(exc))

    try:
        with client or UpstoxClient() as c:
            payload = c.get_json(
                "/v2/market-quote/ltp", params={"instrument_key": instrument_key}
            )
    except UpstoxError as exc:
        return LiveQuote(
            ticker, instrument_key, exchange, None, fetched_at, error=str(exc)
        )

    # Upstox keys the response by "SEGMENT:TRADING_SYMBOL" (e.g. "NSE_EQ:TCS"),
    # not by the instrument_key used to request it -- the instrument_key comes
    # back inside each entry as instrument_token instead. One instrument_key
    # is requested per call, so match on instrument_token rather than assume
    # a key format that isn't documented to be stable.
    data: dict[str, Any] = next(
        (
            entry
            for entry in payload.get("data", {}).values()
            if entry.get("instrument_token") == instrument_key
        ),
        {},
    )
    last_price = data.get("last_price")
    _log_quote(instrument_key, last_price, fetched_at)
    return LiveQuote(
        ticker,
        instrument_key,
        exchange,
        float(last_price) if last_price is not None else None,
        fetched_at,
    )
