"""Thin HTTP client for Upstox Market Data endpoints.

Unlike SecClient, a response is never written to disk: a quote is only
correct for the instant it was fetched, so a TTL cache would let a stale
price silently outlive its truth. Rate limiting is still applied so repeated
questions in a short window do not hammer Upstox's API.
"""

import time
from typing import Any

import httpx

from sra.config import settings


class UpstoxError(RuntimeError):
    """Base for every Upstox-related failure, caught uniformly in cli.main()."""


class UpstoxAuthError(UpstoxError):
    pass


class UpstoxClient:
    def __init__(
        self,
        *,
        token: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        # Imported lazily to avoid a circular import (auth.py imports
        # UpstoxError from this module).
        from sra.upstox.auth import current_upstox_token

        self._token = token or current_upstox_token()
        cfg = settings()
        self._min_interval = 1.0 / cfg.upstox_max_requests_per_second
        self._last_request_at = 0.0
        self._client = httpx.Client(
            base_url="https://api.upstox.com",
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/json",
            },
            timeout=httpx.Timeout(10.0),
            transport=transport,
        )

    def __enter__(self) -> "UpstoxClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self._client.close()

    def get_json(self, path: str, *, params: dict[str, str]) -> Any:
        self._throttle()
        response = self._client.get(path, params=params)
        if response.status_code == 401:
            raise UpstoxAuthError(
                "Upstox rejected the access token (expired or revoked). "
                "Run `sra upstox login` again."
            )
        if response.status_code != 200:
            raise UpstoxError(
                f"{response.status_code} from {path}: {response.text[:200]}"
            )
        return response.json()

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_request_at = time.monotonic()
