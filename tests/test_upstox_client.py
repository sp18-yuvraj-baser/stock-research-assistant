from typing import Any

import httpx
import pytest

from sra.upstox.client import (
    MAX_RETRY_WAIT_SECONDS,
    UpstoxAuthError,
    UpstoxClient,
    UpstoxError,
)


def _transport(status_code: int, body: dict[str, Any]) -> httpx.MockTransport:
    return httpx.MockTransport(lambda request: httpx.Response(status_code, json=body))


def test_get_json_parses_a_successful_response() -> None:
    payload = {"data": {"NSE_EQ|FAKE": {"last_price": 123.45}}}
    client = UpstoxClient(token="fake-token", transport=_transport(200, payload))
    with client as c:
        result = c.get_json(
            "/v2/market-quote/ltp", params={"instrument_key": "NSE_EQ|FAKE"}
        )
    assert result == payload


def test_unauthorized_response_raises_an_actionable_auth_error() -> None:
    client = UpstoxClient(
        token="stale-token", transport=_transport(401, {"error": "bad token"})
    )
    with client as c, pytest.raises(UpstoxAuthError, match="sra upstox login"):
        c.get_json("/v2/market-quote/ltp", params={"instrument_key": "NSE_EQ|FAKE"})


def test_other_error_status_raises_a_plain_upstox_error() -> None:
    client = UpstoxClient(
        token="fake-token", transport=_transport(500, {"error": "boom"})
    )
    # attempts=1: a 5xx is retried by design, but a unit test shouldn't pay
    # for the real backoff delay to prove the eventual-failure path works.
    with client as c, pytest.raises(UpstoxError):
        c.get_json(
            "/v2/market-quote/ltp", params={"instrument_key": "NSE_EQ|FAKE"}, attempts=1
        )


def test_transient_5xx_recovers_on_retry() -> None:
    attempts_made = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        attempts_made["n"] += 1
        if attempts_made["n"] < 2:
            return httpx.Response(503, json={"error": "unavailable"})
        return httpx.Response(200, json={"data": {}})

    client = UpstoxClient(token="fake-token", transport=httpx.MockTransport(handler))
    with client as c:
        result = c.get_json(
            "/v2/market-quote/ltp", params={"instrument_key": "NSE_EQ|FAKE"}
        )
    assert result == {"data": {}}
    assert attempts_made["n"] == 2


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record retry waits instead of sleeping. The rate limiter is silenced so
    only backoff waits are captured."""
    recorded: list[float] = []
    monkeypatch.setattr("sra.upstox.client.time.sleep", recorded.append)
    monkeypatch.setattr(UpstoxClient, "_throttle", lambda _self: None)
    return recorded


def _counting(
    status: int, headers: dict[str, str] | None = None
) -> tuple[httpx.MockTransport, list[int]]:
    hits: list[int] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        hits.append(1)
        return httpx.Response(status, json={}, headers=headers)

    return httpx.MockTransport(handler), hits


def test_no_sleep_after_the_final_failed_attempt(sleeps: list[float]) -> None:
    transport, hits = _counting(503)
    with (
        UpstoxClient(token="t", transport=transport) as c,
        pytest.raises(UpstoxError, match="giving up"),
    ):
        c.get_json("/x", params={}, attempts=3)
    assert len(hits) == 3
    assert sleeps == [1.0, 2.0]  # between attempts only, none after the third


def test_worst_case_wait_is_bounded(sleeps: list[float]) -> None:
    transport, _hits = _counting(503)
    with UpstoxClient(token="t", transport=transport) as c, pytest.raises(UpstoxError):
        c.get_json("/x", params={}, attempts=8)
    assert len(sleeps) == 7
    assert max(sleeps) <= MAX_RETRY_WAIT_SECONDS


def test_retry_after_is_honoured_but_capped(sleeps: list[float]) -> None:
    transport, _hits = _counting(429, {"Retry-After": "3"})
    with UpstoxClient(token="t", transport=transport) as c, pytest.raises(UpstoxError):
        c.get_json("/x", params={}, attempts=3)
    assert sleeps == [3.0, 3.0]

    sleeps.clear()
    transport, _hits = _counting(429, {"Retry-After": "86400"})
    with UpstoxClient(token="t", transport=transport) as c, pytest.raises(UpstoxError):
        c.get_json("/x", params={}, attempts=2)
    assert sleeps == [MAX_RETRY_WAIT_SECONDS]


@pytest.mark.parametrize("header", ["Wed, 21 Oct 2026 07:28:00 GMT", "nan", "-5", ""])
def test_unusable_retry_after_falls_back_to_backoff(
    sleeps: list[float], header: str
) -> None:
    transport, _hits = _counting(429, {"Retry-After": header})
    with UpstoxClient(token="t", transport=transport) as c, pytest.raises(UpstoxError):
        c.get_json("/x", params={}, attempts=3)
    assert all(0 <= s <= MAX_RETRY_WAIT_SECONDS for s in sleeps)
    assert len(sleeps) == 2


def test_401_is_not_retried(sleeps: list[float]) -> None:
    transport, hits = _counting(401)
    with (
        UpstoxClient(token="t", transport=transport) as c,
        pytest.raises(UpstoxAuthError),
    ):
        c.get_json("/x", params={})
    assert len(hits) == 1
    assert sleeps == []


def test_non_json_200_is_an_upstox_error() -> None:
    transport = httpx.MockTransport(lambda _r: httpx.Response(200, text="<html>"))
    with (
        UpstoxClient(token="t", transport=transport) as c,
        pytest.raises(UpstoxError, match="non-JSON"),
    ):
        c.get_json("/x", params={})


def test_network_errors_are_retried_then_reported(sleeps: list[float]) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    with (
        UpstoxClient(token="t", transport=httpx.MockTransport(handler)) as c,
        pytest.raises(UpstoxError, match="giving up"),
    ):
        c.get_json("/x", params={}, attempts=2)
    assert sleeps == [1.0]


def test_shared_client_is_reused_until_reset(monkeypatch: pytest.MonkeyPatch) -> None:
    from sra.upstox import client as client_module

    made: list[UpstoxClient] = []

    class _Recording(UpstoxClient):
        def __init__(self) -> None:
            super().__init__(token="t")
            made.append(self)

    monkeypatch.setattr(client_module, "UpstoxClient", _Recording)
    client_module.reset_shared_client()
    first = client_module.shared_client()
    assert client_module.shared_client() is first
    client_module.reset_shared_client()
    assert client_module.shared_client() is not first
    assert len(made) == 2
    client_module.reset_shared_client()


def test_throttle_spaces_requests_from_any_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading

    sleeps: list[float] = []
    monkeypatch.setattr("sra.upstox.client.time.sleep", sleeps.append)
    client = UpstoxClient(
        token="t", transport=_transport(200, {"status": "success", "data": {}})
    )
    threads = [
        threading.Thread(target=client.get_json, args=("/x",), kwargs={"params": {}})
        for _ in range(3)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    # One shared limiter: every request after the first waited its turn.
    assert len(sleeps) == 2
