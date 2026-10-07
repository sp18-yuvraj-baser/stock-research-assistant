from typing import Any

import pytest

from sra.tools.get_live_quote import get_live_quote
from sra.upstox.client import UpstoxError
from sra.upstox.instruments import UnknownInstrumentError


class _FakeClient:
    """Stands in for UpstoxClient: same get_json/context-manager shape."""

    def __init__(
        self, *, payload: dict[str, Any] | None = None, error: Exception | None = None
    ):
        self._payload = payload
        self._error = error
        self.calls: list[tuple[str, dict[str, str]]] = []

    def __enter__(self) -> "_FakeClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        pass

    def get_json(self, path: str, *, params: dict[str, str]) -> Any:
        self.calls.append((path, params))
        if self._error is not None:
            raise self._error
        return self._payload


@pytest.fixture(autouse=True)
def _no_db_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    # get_live_quote's audit write is best-effort and must never run against
    # a real database in a unit test.
    monkeypatch.setattr("sra.tools.get_live_quote._log_quote", lambda *_a, **_k: None)


def test_unknown_ticker_returns_error_without_calling_upstox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise(_ticker: str) -> tuple[str, str]:
        raise UnknownInstrumentError("'FAKE' is not a tracked NSE instrument")

    monkeypatch.setattr("sra.tools.get_live_quote.resolve_instrument_key", _raise)
    quote = get_live_quote("FAKE")
    assert quote.error is not None
    assert "not a tracked NSE instrument" in quote.error
    assert quote.last_price is None


def test_upstox_error_is_surfaced_on_the_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "sra.tools.get_live_quote.resolve_instrument_key",
        lambda _ticker: ("NSE_EQ|INE467B01029", "NSE_EQ"),
    )
    client = _FakeClient(error=UpstoxError("Upstox rejected the access token"))
    quote = get_live_quote("TCS", client=client)
    assert quote.error is not None
    assert "Upstox rejected the access token" in quote.error
    assert quote.last_price is None


def test_successful_quote_is_parsed_and_never_implies_a_filing_figure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "sra.tools.get_live_quote.resolve_instrument_key",
        lambda _ticker: ("NSE_EQ|INE467B01029", "NSE_EQ"),
    )
    client = _FakeClient(
        payload={
            "data": {
                # Upstox keys responses by "SEGMENT:SYMBOL", not by
                # instrument_key -- the instrument_key comes back inside the
                # entry as instrument_token instead.
                "NSE_EQ:TCS": {
                    "last_price": 4123.5,
                    "instrument_token": "NSE_EQ|INE467B01029",
                }
            }
        }
    )
    quote = get_live_quote("TCS", client=client)
    assert quote.error is None
    assert quote.last_price == 4123.5
    assert quote.fetched_at
    assert "SEC filing" in quote.to_text()
