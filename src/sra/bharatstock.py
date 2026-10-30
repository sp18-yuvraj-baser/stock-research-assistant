"""Optional BharatStock API client, cached in the database.

The free plan allows 60 requests a day, so every response is stored in
bharatstock_cache and reused until it is older than the caller's max_age.
Without SRA_BHARATSTOCK_API_KEY nothing is called and callers say the data
is not covered.
"""

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote, urlencode

import httpx
import psycopg
from psycopg.types.json import Jsonb

from sra.config import settings
from sra.db import connect

log = logging.getLogger(__name__)

BASE_URL = "https://bharatstockapi.com/v1"
TIMEOUT_SECONDS = 20.0

NOT_CONFIGURED = (
    "This needs a BharatStock API key (free at bharatstockapi.com, 60 requests "
    "a day). Set SRA_BHARATSTOCK_API_KEY in .env to enable it."
)


class BharatStockError(RuntimeError):
    pass


def configured() -> bool:
    return bool(settings().bharatstock_api_key)


def path_part(value: str) -> str:
    return quote(value, safe="")


def _cached(request: str, max_age: timedelta) -> Any | None:
    try:
        with connect(readonly=True) as conn:
            row = conn.execute(
                "SELECT payload FROM bharatstock_cache "
                "WHERE request = %(r)s AND fetched_at > %(since)s",
                {"r": request, "since": datetime.now(UTC) - max_age},
            ).fetchone()
    except psycopg.Error:
        return None
    return row["payload"] if row else None


def _store(request: str, payload: Any) -> None:
    try:
        with connect() as conn:
            conn.execute(
                "INSERT INTO bharatstock_cache (request, payload, fetched_at) "
                "VALUES (%(r)s, %(p)s, now()) ON CONFLICT (request) DO UPDATE "
                "SET payload = EXCLUDED.payload, fetched_at = EXCLUDED.fetched_at",
                {"r": request, "p": Jsonb(payload)},
            )
            conn.commit()
    except psycopg.Error as exc:
        log.warning("could not cache BharatStock response: %s", exc)


def get(path: str, params: dict[str, str], *, max_age: timedelta) -> Any:
    """A cached GET; raises BharatStockError when unconfigured or failing."""
    if not configured():
        raise BharatStockError(NOT_CONFIGURED)
    request = path + ("?" + urlencode(sorted(params.items())) if params else "")
    cached = _cached(request, max_age)
    if cached is not None:
        return cached
    try:
        response = httpx.get(
            BASE_URL + path,
            params=params,
            headers={"X-API-Key": settings().bharatstock_api_key},
            timeout=TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        raise BharatStockError(f"BharatStock is unreachable: {exc}") from exc
    if response.status_code == 429:
        raise BharatStockError("the BharatStock daily request limit is used up")
    if response.status_code in (401, 403):
        raise BharatStockError("BharatStock rejected the API key")
    if response.status_code == 404:
        raise BharatStockError("BharatStock has no data for this")
    if response.status_code != 200:
        raise BharatStockError(f"BharatStock answered {response.status_code}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise BharatStockError("BharatStock returned invalid JSON") from exc
    _store(request, payload)
    return payload
