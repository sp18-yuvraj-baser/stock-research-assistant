"""Thin HTTP client for Upstox Market Data endpoints.

A response is never cached: a quote is only correct for the instant it was
fetched, so a TTL cache would let a stale price silently outlive its truth.
Answering questions goes through one shared client per process, so its TLS
connection is reused and its rate limit applies across every request rather
than per call.
"""

import contextlib
import threading
import time
from typing import Any

import httpx

from sra.config import settings

# Backoff between attempts is 1s, 2s, 4s, ... but never more than this, and a
# server's Retry-After is honoured only up to it. With the default 4 attempts the
# worst case is therefore 3 waits of <= 5s plus 4 requests of <= 10s each, about
# 55s, instead of blocking a chat turn for as long as a hostile header asks.
MAX_RETRY_WAIT_SECONDS = 5.0


def _retry_delay(response: httpx.Response | None, attempt: int) -> float:
    """Seconds to wait before the next attempt, bounded by MAX_RETRY_WAIT_SECONDS."""
    delay = float(2**attempt)
    if response is not None:
        # Only the delta-seconds form; an HTTP-date falls back to backoff.
        with contextlib.suppress(KeyError, ValueError):
            delay = float(response.headers["Retry-After"])
    # A NaN delay would slip past min/max, so treat it as no usable hint.
    if delay != delay:
        delay = float(2**attempt)
    return min(max(delay, 0.0), MAX_RETRY_WAIT_SECONDS)


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
        # Dashboard requests run on worker threads that share this client.
        self._throttle_lock = threading.Lock()
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
        self.close()

    def close(self) -> None:
        self._client.close()

    def get_json(self, path: str, *, params: dict[str, str], attempts: int = 4) -> Any:
        """GET with bounded retry and backoff; every failure is an UpstoxError."""
        last_error: Exception | None = None
        for attempt in range(attempts):
            self._throttle()
            response: httpx.Response | None = None
            try:
                response = self._client.get(path, params=params)
            except httpx.HTTPError as exc:
                last_error = exc
            else:
                if response.status_code == 200:
                    try:
                        return response.json()
                    except ValueError as exc:
                        raise UpstoxError(f"non-JSON 200 response from {path}") from exc
                if response.status_code == 401:
                    raise UpstoxAuthError(
                        "Upstox rejected the access token (expired or revoked). "
                        "Run `sra upstox login` again."
                    )
                # 429 throttles, 5xx sheds load; both recover on retry.
                if response.status_code in (429, 500, 502, 503, 504):
                    last_error = UpstoxError(f"{response.status_code} from {path}")
                else:
                    raise UpstoxError(
                        f"{response.status_code} from {path}: {response.text[:200]}"
                    )
            # No wait after the last attempt: there is nothing left to retry.
            if attempt < attempts - 1:
                time.sleep(_retry_delay(response, attempt))
        raise UpstoxError(
            f"giving up on {path} after {attempts} attempts"
        ) from last_error

    def _throttle(self) -> None:
        with self._throttle_lock:
            elapsed = time.monotonic() - self._last_request_at
            if elapsed < self._min_interval:
                time.sleep(self._min_interval - elapsed)
            self._last_request_at = time.monotonic()


_shared_lock = threading.Lock()
_shared: UpstoxClient | None = None


def shared_client() -> UpstoxClient:
    """The process's one client for answering questions, created on first use.

    The token is read when it is created; call reset_shared_client() after an
    auth failure so the next request picks up a token from `sra upstox login`.
    """
    global _shared
    with _shared_lock:
        if _shared is None:
            _shared = UpstoxClient()
        return _shared


def reset_shared_client() -> None:
    global _shared
    with _shared_lock:
        if _shared is not None:
            _shared.close()
            _shared = None
