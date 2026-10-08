import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from sra.upstox.auth import (
    PREEMPTIVE_STALE_AFTER,
    MissingUpstoxTokenError,
    current_upstox_token,
)


@pytest.fixture(autouse=True)
def _no_analytics_token(monkeypatch: pytest.MonkeyPatch) -> None:
    # These tests exercise the cached-daily-token fallback path; they must not
    # depend on whatever SRA_UPSTOX_ANALYTICS_TOKEN happens to be set in the
    # real environment.
    monkeypatch.setattr(
        "sra.upstox.auth.settings", lambda: SimpleNamespace(upstox_analytics_token="")
    )


def test_missing_token_file_raises_actionably(tmp_path: Path) -> None:
    with pytest.raises(MissingUpstoxTokenError, match="sra upstox login"):
        current_upstox_token(cache_dir=tmp_path)


def test_analytics_token_is_preferred_over_the_cached_daily_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No file on disk at all -- the analytics token alone must be enough.
    monkeypatch.setattr(
        "sra.upstox.auth.settings",
        lambda: SimpleNamespace(upstox_analytics_token="long-lived-token"),
    )
    assert current_upstox_token(cache_dir=tmp_path) == "long-lived-token"


def _write_fake_token(cache_dir: Path, *, obtained_at: datetime) -> None:
    (cache_dir / "upstox_token.json").write_text(
        json.dumps({"access_token": "abc123", "obtained_at": obtained_at.isoformat()})
    )


def test_fresh_token_is_returned(tmp_path: Path) -> None:
    _write_fake_token(tmp_path, obtained_at=datetime.now(UTC))
    assert current_upstox_token(cache_dir=tmp_path) == "abc123"


def test_stale_token_raises_actionably(tmp_path: Path) -> None:
    obtained_at = datetime.now(UTC) - PREEMPTIVE_STALE_AFTER - timedelta(hours=1)
    _write_fake_token(tmp_path, obtained_at=obtained_at)
    with pytest.raises(MissingUpstoxTokenError, match="sra upstox login"):
        current_upstox_token(cache_dir=tmp_path)
