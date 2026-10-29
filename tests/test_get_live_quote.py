from typing import Any

import psycopg
import pytest

from sra.tools.get_live_quote import (
    _log_quote,
    get_live_quote,
    resolve_instrument_key,
)
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
        raise UnknownInstrumentError(
            "'FAKE' is not in the synced NSE/BSE equity universe"
        )

    monkeypatch.setattr("sra.tools.get_live_quote.resolve_instrument_key", _raise)
    quote = get_live_quote("FAKE")
    assert quote.error is not None
    assert "synced NSE/BSE equity universe" in quote.error
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


def test_successful_quote_is_parsed_and_identified_as_an_nse_bse_quote(
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
    assert "live NSE/BSE quote" in quote.to_text()


_KEY = "NSE_EQ|INE467B01029"


def _resolve_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "sra.tools.get_live_quote.resolve_instrument_key",
        lambda _ticker: (_KEY, "NSE_EQ"),
    )


def test_database_error_while_resolving_becomes_an_error_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _down(_ticker: str) -> tuple[str, str]:
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr("sra.tools.get_live_quote.resolve_instrument_key", _down)
    client = _FakeClient(payload={})
    quote = get_live_quote("TCS", client=client)
    assert quote.error is not None and "connection refused" in quote.error
    assert quote.last_price is None
    assert client.calls == []  # never reached Upstox


@pytest.mark.parametrize(
    "payload",
    [
        {"data": {}},  # halted or unknown instrument
        {"data": {"NSE_EQ:TCS": {"last_price": 1.0, "instrument_token": "OTHER"}}},
        {"data": {"NSE_EQ:TCS": {"instrument_token": _KEY}}},  # no price
        {"data": {"NSE_EQ:TCS": {"instrument_token": _KEY, "last_price": None}}},
        {"data": {"NSE_EQ:TCS": {"instrument_token": _KEY, "last_price": "n/a"}}},
        {"data": {"NSE_EQ:TCS": {"instrument_token": _KEY, "last_price": "nan"}}},
        {"data": {"NSE_EQ:TCS": {"instrument_token": _KEY, "last_price": [1]}}},
        {"data": {"NSE_EQ:TCS": "not-a-dict"}},
        {"data": None},
        {},
        None,
        ["unexpected"],
    ],
)
def test_response_without_a_usable_price_is_an_error_not_a_none_price(
    monkeypatch: pytest.MonkeyPatch, payload: Any
) -> None:
    _resolve_ok(monkeypatch)
    logged: list[Any] = []
    monkeypatch.setattr(
        "sra.tools.get_live_quote._log_quote", lambda *a, **_k: logged.append(a)
    )
    quote = get_live_quote("TCS", client=_FakeClient(payload=payload))
    assert quote.error is not None and _KEY in quote.error
    assert quote.last_price is None
    assert quote.to_text().startswith("ERROR:")
    assert logged == []  # no NULL-price audit row


def test_numeric_string_price_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    _resolve_ok(monkeypatch)
    payload = {"data": {"NSE_EQ:TCS": {"instrument_token": _KEY, "last_price": "99.5"}}}
    quote = get_live_quote("TCS", client=_FakeClient(payload=payload))
    assert quote.error is None and quote.last_price == 99.5


def test_nse_first_resolution_is_kept_when_no_exchange_is_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class _Conn:
        def __enter__(self) -> "_Conn":
            return self

        def __exit__(self, *_exc: object) -> None:
            pass

        def execute(self, sql: str, params: Any) -> "_Conn":
            captured["sql"], captured["params"] = sql, params
            return self

        def fetchone(self) -> dict[str, str]:
            return {"instrument_key": _KEY, "exchange": "NSE_EQ"}

    def fake_connect(*, readonly: bool = False) -> _Conn:
        captured["readonly"] = readonly
        return _Conn()

    monkeypatch.setattr("sra.tools.get_live_quote.connect", fake_connect)
    assert resolve_instrument_key("tcs") == (_KEY, "NSE_EQ")
    assert captured["readonly"] is True
    assert "exchange = 'NSE_EQ' DESC" in captured["sql"]
    assert captured["params"] == ("tcs",)


def test_audit_write_failure_is_logged_not_raised(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    class _Failing:
        def __enter__(self) -> Any:
            raise psycopg.OperationalError("down")

        def __exit__(self, *_exc: object) -> None:
            pass

    monkeypatch.setattr("sra.tools.get_live_quote.connect", lambda: _Failing())
    with caplog.at_level("WARNING", logger="sra.tools.get_live_quote"):
        _log_quote(_KEY, 1.0, "2026-01-01T00:00:00+00:00")
    assert "could not log quote" in caplog.text
