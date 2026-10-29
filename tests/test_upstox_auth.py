import json
import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from sra.upstox.auth import (
    PREEMPTIVE_STALE_AFTER,
    MissingUpstoxTokenError,
    UpstoxToken,
    _write_token,
    current_upstox_token,
)
from sra.upstox.client import UpstoxError


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


@pytest.mark.parametrize(
    "content",
    [
        "",
        '{"access_token": "abc',
        "not json",
        "[]",
        "null",
        json.dumps({"obtained_at": "2026-01-01T00:00:00+00:00"}),
        json.dumps({"access_token": "abc"}),
        json.dumps({"access_token": 123, "obtained_at": "2026-01-01T00:00:00+00:00"}),
        json.dumps({"access_token": "", "obtained_at": "2026-01-01T00:00:00+00:00"}),
        json.dumps({"access_token": "abc", "obtained_at": "yesterday"}),
        json.dumps({"access_token": "abc", "obtained_at": 12345}),
        json.dumps({"access_token": "abc", "obtained_at": None}),
    ],
)
def test_corrupt_token_file_raises_actionably(tmp_path: Path, content: str) -> None:
    (tmp_path / "upstox_token.json").write_text(content)
    with pytest.raises(MissingUpstoxTokenError, match="sra upstox login"):
        current_upstox_token(cache_dir=tmp_path)


def test_non_utf8_token_file_raises_actionably(tmp_path: Path) -> None:
    (tmp_path / "upstox_token.json").write_bytes(b"\xff\xfe\x00garbage")
    with pytest.raises(MissingUpstoxTokenError, match="sra upstox login"):
        current_upstox_token(cache_dir=tmp_path)


def test_naive_timestamp_does_not_crash(tmp_path: Path) -> None:
    (tmp_path / "upstox_token.json").write_text(
        json.dumps(
            {
                "access_token": "abc123",
                "obtained_at": datetime.now(UTC).replace(tzinfo=None).isoformat(),
            }
        )
    )
    assert current_upstox_token(cache_dir=tmp_path) == "abc123"


def test_missing_token_error_is_an_upstox_error() -> None:
    # get_live_quote catches UpstoxError and returns it as an error value.
    assert issubclass(MissingUpstoxTokenError, UpstoxError)


def test_token_file_is_created_0600_in_a_0700_dir(tmp_path: Path) -> None:
    cache_dir = tmp_path / "cache"
    _write_token(UpstoxToken("secret", datetime.now(UTC)), cache_dir)
    token_path = cache_dir / "upstox_token.json"
    assert stat.S_IMODE(token_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(cache_dir.stat().st_mode) == 0o700
    assert json.loads(token_path.read_text())["access_token"] == "secret"
    assert [p.name for p in cache_dir.iterdir()] == ["upstox_token.json"]


def test_token_file_is_never_created_wider_than_0600(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A permissive umask must not matter: the mode is set at creation, so
    # there is no window where the token is group/world readable.
    old = os.umask(0)
    try:
        _write_token(UpstoxToken("secret", datetime.now(UTC)), tmp_path)
    finally:
        os.umask(old)
    assert stat.S_IMODE((tmp_path / "upstox_token.json").stat().st_mode) == 0o600


def test_rewriting_replaces_an_existing_token_and_keeps_0600(tmp_path: Path) -> None:
    path = tmp_path / "upstox_token.json"
    path.write_text("old")
    path.chmod(0o644)
    _write_token(UpstoxToken("new", datetime.now(UTC)), tmp_path)
    assert json.loads(path.read_text())["access_token"] == "new"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_failed_write_keeps_the_old_token_and_leaves_no_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "upstox_token.json"
    path.write_text("old")

    def _fail(*_a: object, **_k: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("sra.upstox.auth.os.replace", _fail)
    with pytest.raises(OSError, match="disk full"):
        _write_token(UpstoxToken("new", datetime.now(UTC)), tmp_path)
    assert path.read_text() == "old"
    assert [p.name for p in tmp_path.iterdir()] == ["upstox_token.json"]
