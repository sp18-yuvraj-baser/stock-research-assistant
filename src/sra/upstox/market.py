"""Upstox market data: full quotes, daily candles, market status, holidays.

Quotes are never cached: a price is only true for the instant it was
fetched. Daily candles are completed sessions, so a day's fetch is reused for
the rest of that day. Every function lets UpstoxError through for the caller
to turn into an answer.
"""

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import lru_cache
from typing import Any, Protocol
from urllib.parse import quote

from sra.upstox.client import shared_client

# Upstox accepts up to 500 instrument keys in one quotes request.
QUOTE_BATCH = 500


class JsonClient(Protocol):
    def get_json(self, path: str, *, params: dict[str, str]) -> Any: ...


@dataclass(frozen=True)
class Index:
    key: str
    name: str
    aliases: tuple[str, ...]


# The indices the market overview shows, and the names people use for them.
INDICES = (
    Index("NSE_INDEX|Nifty 50", "Nifty 50", ("nifty 50", "nifty50", "nifty")),
    Index("BSE_INDEX|SENSEX", "Sensex", ("sensex",)),
    Index(
        "NSE_INDEX|Nifty Bank", "Nifty Bank", ("bank nifty", "banknifty", "nifty bank")
    ),
    Index("NSE_INDEX|Nifty IT", "Nifty IT", ("nifty it",)),
    Index("NSE_INDEX|Nifty Auto", "Nifty Auto", ("nifty auto",)),
    Index("NSE_INDEX|Nifty Pharma", "Nifty Pharma", ("nifty pharma",)),
    Index("NSE_INDEX|Nifty FMCG", "Nifty FMCG", ("nifty fmcg",)),
    Index("NSE_INDEX|Nifty Metal", "Nifty Metal", ("nifty metal",)),
    Index("NSE_INDEX|Nifty Realty", "Nifty Realty", ("nifty realty",)),
    Index("NSE_INDEX|Nifty Energy", "Nifty Energy", ("nifty energy",)),
    Index("NSE_INDEX|Nifty PSU Bank", "Nifty PSU Bank", ("nifty psu bank", "psu bank")),
    Index(
        "NSE_INDEX|Nifty Fin Service",
        "Nifty Financial Services",
        ("finnifty", "nifty fin"),
    ),
    Index("NSE_INDEX|NIFTY MIDCAP 100", "Nifty Midcap 100", ("midcap", "nifty midcap")),
    Index(
        "NSE_INDEX|Nifty Smallcap 500",
        "Nifty Smallcap 500",
        ("smallcap", "nifty smallcap"),
    ),
    Index("NSE_INDEX|India VIX", "India VIX", ("india vix", "vix")),
)


def find_index(text: str) -> Index | None:
    """The index a question names, longest alias first ("bank nifty" before
    "nifty")."""
    lower = f" {' '.join(text.lower().split())} "
    pairs = sorted(
        ((alias, index) for index in INDICES for alias in index.aliases),
        key=lambda pair: -len(pair[0]),
    )
    for alias, index in pairs:
        if f" {alias} " in lower or f" {alias}?" in lower or f" {alias}'" in lower:
            return index
    return None


@dataclass(frozen=True)
class FullQuote:
    key: str
    last_price: float
    net_change: float | None
    open: float | None
    high: float | None
    low: float | None
    volume: int | None
    average_price: float | None
    lower_circuit: float | None
    upper_circuit: float | None

    @property
    def previous_close(self) -> float | None:
        if self.net_change is None:
            return None
        return self.last_price - self.net_change

    @property
    def change_pct(self) -> float | None:
        prior = self.previous_close
        if prior is None or prior <= 0 or self.net_change is None:
            return None
        return 100 * self.net_change / prior

    @property
    def traded_value(self) -> float | None:
        """Rupees traded today (volume x average price), when both are known."""
        if self.volume is None or self.average_price is None:
            return None
        return self.volume * self.average_price


def _finite(raw: Any) -> float | None:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _parse_quote(entry: Any) -> FullQuote | None:
    if not isinstance(entry, dict):
        return None
    key, price = entry.get("instrument_token"), _finite(entry.get("last_price"))
    if not isinstance(key, str) or price is None or price <= 0:
        return None
    raw_ohlc = entry.get("ohlc")
    ohlc: dict[str, Any] = raw_ohlc if isinstance(raw_ohlc, dict) else {}
    volume = _finite(entry.get("volume"))
    return FullQuote(
        key=key,
        last_price=price,
        net_change=_finite(entry.get("net_change")),
        open=_finite(ohlc.get("open")),
        high=_finite(ohlc.get("high")),
        low=_finite(ohlc.get("low")),
        volume=int(volume) if volume is not None else None,
        average_price=_finite(entry.get("average_price")),
        lower_circuit=_finite(entry.get("lower_circuit_limit")),
        upper_circuit=_finite(entry.get("upper_circuit_limit")),
    )


def full_quotes(
    keys: list[str], *, client: JsonClient | None = None
) -> dict[str, FullQuote]:
    """Live quotes for many instruments, keyed by instrument key.

    Upstox keys its response by "SEGMENT:SYMBOL", not by the requested key,
    so entries are matched on instrument_token. An instrument with no usable
    price is simply absent from the result.
    """
    source = client or shared_client()
    quotes: dict[str, FullQuote] = {}
    unique = list(dict.fromkeys(keys))
    for start in range(0, len(unique), QUOTE_BATCH):
        batch = unique[start : start + QUOTE_BATCH]
        payload = source.get_json(
            "/v2/market-quote/quotes", params={"instrument_key": ",".join(batch)}
        )
        entries = payload.get("data") if isinstance(payload, dict) else None
        for entry in (entries or {}).values() if isinstance(entries, dict) else ():
            parsed = _parse_quote(entry)
            if parsed is not None and parsed.key in batch:
                quotes[parsed.key] = parsed
    return quotes


@dataclass(frozen=True)
class Candle:
    day: date
    open: float
    high: float
    low: float
    close: float
    volume: int


def daily_candles(
    key: str, *, years: int = 5, today: date | None = None
) -> list[Candle]:
    """Completed daily sessions for the last `years` years, oldest first."""
    end = today or date.today()
    return list(_daily_candles(key, end - timedelta(days=366 * years), end))


@lru_cache(maxsize=256)
def _daily_candles(key: str, start: date, end: date) -> tuple[Candle, ...]:
    # The end date is part of the cache key, so a new day fetches afresh.
    payload = shared_client().get_json(
        f"/v2/historical-candle/{quote(key, safe='')}/day/{end}/{start}", params={}
    )
    data = payload.get("data") if isinstance(payload, dict) else None
    rows = data.get("candles") if isinstance(data, dict) else None
    candles: list[Candle] = []
    for row in rows or []:
        if not isinstance(row, list) or len(row) < 6:
            continue
        values = [_finite(v) for v in row[1:5]]
        if any(v is None for v in values):
            continue
        try:
            day = datetime.fromisoformat(str(row[0])).date()
        except ValueError:
            continue
        o, h, lo, c = (v for v in values if v is not None)
        candles.append(Candle(day, o, h, lo, c, int(_finite(row[5]) or 0)))
    return tuple(sorted(candles, key=lambda candle: candle.day))


def market_status(exchange: str = "NSE") -> str | None:
    """Upstox's status word for an exchange (e.g. NORMAL_OPEN, CLOSING_END)."""
    payload = shared_client().get_json(f"/v2/market/status/{exchange}", params={})
    data = payload.get("data") if isinstance(payload, dict) else None
    status = data.get("status") if isinstance(data, dict) else None
    return str(status) if status else None


def upcoming_holidays(today: date, *, count: int = 3) -> list[tuple[date, str]]:
    """The next trading holidays on which NSE is closed."""
    payload = shared_client().get_json("/v2/market/holidays", params={})
    rows = payload.get("data") if isinstance(payload, dict) else None
    found: list[tuple[date, str]] = []
    for row in rows or []:
        if not isinstance(row, dict) or "NSE" not in (
            row.get("closed_exchanges") or []
        ):
            continue
        try:
            day = date.fromisoformat(str(row.get("date")))
        except ValueError:
            continue
        if day >= today:
            found.append((day, str(row.get("description") or "Holiday")))
    return sorted(found)[:count]
