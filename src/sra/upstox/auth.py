"""Upstox OAuth2 authorization-code flow and daily access-token cache.

Upstox's standard access token must be regenerated once a day through an
interactive browser consent step; there is no fully silent refresh for it.
The token is cached to a file under cache_dir rather than .env, since it
rotates daily and .env is meant for stable configuration.
"""

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from sra.config import settings
from sra.upstox.client import UpstoxError

TOKEN_CACHE_NAME = "upstox_token.json"

# Upstox access tokens are issued daily; refresh a bit before they are likely
# to have expired rather than finding out mid-tool-call.
PREEMPTIVE_STALE_AFTER = timedelta(hours=20)


class MissingUpstoxTokenError(UpstoxError):
    pass


@dataclass(frozen=True)
class UpstoxToken:
    access_token: str
    obtained_at: datetime


def authorize_url(*, api_key: str, redirect_uri: str) -> str:
    return (
        "https://api.upstox.com/v2/login/authorization/dialog"
        f"?response_type=code&client_id={api_key}&redirect_uri={redirect_uri}"
    )


def exchange_code(code: str, *, cache_dir: Path | None = None) -> UpstoxToken:
    """Trade a one-time authorization code for an access token and cache it."""
    cfg = settings()
    if not cfg.upstox_api_key or not cfg.upstox_api_secret:
        raise MissingUpstoxTokenError(
            "SRA_UPSTOX_API_KEY / SRA_UPSTOX_API_SECRET are not set. "
            "Create an app at https://developer.upstox.com and add them to .env."
        )
    response = httpx.post(
        "https://api.upstox.com/v2/login/authorization/token",
        data={
            "code": code,
            "client_id": cfg.upstox_api_key,
            "client_secret": cfg.upstox_api_secret,
            "redirect_uri": cfg.upstox_redirect_uri,
            "grant_type": "authorization_code",
        },
        headers={"Accept": "application/json"},
        timeout=httpx.Timeout(30.0),
    )
    if response.status_code != 200:
        raise UpstoxError(
            f"{response.status_code} exchanging code: {response.text[:200]}"
        )
    token = UpstoxToken(
        access_token=response.json()["access_token"], obtained_at=datetime.now(UTC)
    )
    _write_token(token, cache_dir or cfg.cache_dir)
    return token


def _token_path(cache_dir: Path) -> Path:
    return cache_dir / TOKEN_CACHE_NAME


def _write_token(token: UpstoxToken, cache_dir: Path) -> None:
    # 0700 on the directory and a 0600 file that never exists with wider
    # permissions: it is created by os.open with the mode, written under a
    # temporary name, then renamed over the old token so a crash cannot leave a
    # half-written token behind.
    cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = _token_path(cache_dir)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    payload = json.dumps(
        {
            "access_token": token.access_token,
            "obtained_at": token.obtained_at.isoformat(),
        }
    )
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(payload)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def current_upstox_token(*, cache_dir: Path | None = None) -> str:
    """The only place other code reads the current Upstox token.

    Prefers SRA_UPSTOX_ANALYTICS_TOKEN: a read-only, 1-year-validity token
    scoped to Market Data, so it needs no daily refresh and no on-disk cache.
    Falls back to the cached daily OAuth2 access token from `sra upstox
    login` only if no analytics token is configured.

    Fails loudly and actionably here rather than letting an HTTP call 401
    deep inside a tool call with no context the model or user can act on.
    """
    analytics_token = settings().upstox_analytics_token
    if analytics_token:
        return analytics_token

    path = _token_path(cache_dir or settings().cache_dir)
    if not path.exists():
        raise MissingUpstoxTokenError(
            "no Upstox token configured. Set SRA_UPSTOX_ANALYTICS_TOKEN in .env "
            "(recommended -- valid for a year), or run `sra upstox login` for a "
            "daily access token."
        )
    try:
        data = json.loads(path.read_text())
        access_token = data["access_token"]
        obtained_at = datetime.fromisoformat(data["obtained_at"])
        if not isinstance(access_token, str) or not access_token:
            raise ValueError("access_token is not a non-empty string")
        if obtained_at.tzinfo is None:
            obtained_at = obtained_at.replace(tzinfo=UTC)
        age = datetime.now(UTC) - obtained_at
    except (OSError, ValueError, KeyError, TypeError) as exc:
        # JSONDecodeError and UnicodeDecodeError are ValueErrors; a hand-edited
        # or truncated cache must not leak a raw parse error to the caller.
        raise MissingUpstoxTokenError(
            f"Upstox token cache {path} is unreadable or corrupt ({exc!r}). "
            "Run `sra upstox login` again."
        ) from exc
    if age > PREEMPTIVE_STALE_AFTER:
        raise MissingUpstoxTokenError(
            f"Upstox token is {age.total_seconds() / 3600:.1f}h old and likely expired "
            "(Upstox access tokens last about a day). Run `sra upstox login` again."
        )
    return access_token
