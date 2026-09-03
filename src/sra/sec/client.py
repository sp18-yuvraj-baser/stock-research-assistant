import hashlib
import json
import time
from pathlib import Path
from typing import Any

import httpx

from sra.config import settings


class SecError(RuntimeError):
    pass


class SecClient:
    """Rate-limited, disk-cached reader for SEC endpoints.

    Filing documents are immutable once published, so they are cached
    permanently. Index endpoints (submissions, company facts) gain rows as new
    filings land and are cached with a TTL instead.
    """

    DEFAULT_TTL_SECONDS = 12 * 60 * 60

    def __init__(self, *, cache_dir: Path | None = None) -> None:
        cfg = settings()
        self._cache_dir = cache_dir or cfg.cache_dir
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._min_interval = 1.0 / cfg.sec_max_requests_per_second
        self._last_request_at = 0.0
        self._client = httpx.Client(
            headers={
                "User-Agent": cfg.sec_user_agent,
                "Accept-Encoding": "gzip, deflate",
            },
            timeout=httpx.Timeout(30.0),
            follow_redirects=True,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "SecClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def get_json(
        self,
        url: str,
        *,
        immutable: bool = False,
        refresh: bool = False,
    ) -> Any:
        return json.loads(self.get_text(url, immutable=immutable, refresh=refresh))

    def get_text(
        self,
        url: str,
        *,
        immutable: bool = False,
        refresh: bool = False,
    ) -> str:
        path = self._cache_path(url)
        if not refresh and self._cache_is_fresh(path, immutable=immutable):
            return path.read_text(encoding="utf-8")
        body = self._fetch(url)
        path.write_text(body, encoding="utf-8")
        return body

    def _cache_path(self, url: str) -> Path:
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]
        return self._cache_dir / f"{digest}.cache"

    def _cache_is_fresh(self, path: Path, *, immutable: bool) -> bool:
        if not path.exists():
            return False
        if immutable:
            return True
        return (time.time() - path.stat().st_mtime) < self.DEFAULT_TTL_SECONDS

    def _fetch(self, url: str, *, attempts: int = 4) -> str:
        last_error: Exception | None = None
        for attempt in range(attempts):
            self._throttle()
            try:
                response = self._client.get(url)
            except httpx.HTTPError as exc:
                last_error = exc
            else:
                if response.status_code == 200:
                    return response.text
                # SEC throttles with 429 and sheds load with 5xx; both recover.
                if response.status_code in (429, 500, 502, 503, 504):
                    last_error = SecError(f"{response.status_code} from {url}")
                else:
                    raise SecError(
                        f"{response.status_code} from {url}: {response.text[:200]}"
                    )
            time.sleep(2**attempt)
        raise SecError(f"giving up on {url} after {attempts} attempts") from last_error

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_request_at = time.monotonic()
