from typing import Any

import httpx
import pytest

from sra.upstox.client import UpstoxAuthError, UpstoxClient, UpstoxError


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
    with client as c, pytest.raises(UpstoxError):
        c.get_json("/v2/market-quote/ltp", params={"instrument_key": "NSE_EQ|FAKE"})
