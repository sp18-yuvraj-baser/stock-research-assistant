import logging

import pytest
from pydantic import ValidationError

from sra import config


def test_log_level_comes_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []
    monkeypatch.setenv("SRA_LOG_LEVEL", "WARNING")
    monkeypatch.setattr(logging, "basicConfig", lambda **kw: calls.append(kw))
    config.settings.cache_clear()
    try:
        config.configure_logging()
    finally:
        config.settings.cache_clear()
    assert calls[0]["level"] == "WARNING"


def test_an_unknown_log_level_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SRA_LOG_LEVEL", "LOUD")
    with pytest.raises(ValidationError):
        config.Settings()  # type: ignore[call-arg]  # values come from env
