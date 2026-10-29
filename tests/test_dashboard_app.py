import logging
from types import SimpleNamespace
from typing import Any

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient

from sra.agent.followup import Turn
from sra.agent.loop import OllamaError
from sra.dashboard import app as dashboard_app


def test_home_is_chat_only() -> None:
    response = TestClient(dashboard_app.app, base_url="http://127.0.0.1").get("/")
    assert response.status_code == 200
    assert 'id="chatForm"' in response.text
    assert "Research screener" not in response.text
    assert "Portfolio" not in response.text
    assert "evidenceFile" not in response.text


def test_chat_endpoint_returns_the_existing_agent_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_ask(message: str, *, history: list[Turn]) -> SimpleNamespace:
        assert message == "What is ROE?"
        assert history == []
        return SimpleNamespace(
            standalone=message,
            text="ROE measures profitability relative to shareholder equity.",
            route=SimpleNamespace(value="general_finance"),
            paths=[],
        )

    monkeypatch.setattr(dashboard_app, "ask", fake_ask)
    response = TestClient(dashboard_app.app, base_url="http://127.0.0.1").post(
        "/api/chat", json={"message": "What is ROE?"}
    )

    assert response.status_code == 200
    assert response.json() == {
        "answer": "ROE measures profitability relative to shareholder equity.",
        "route": "general_finance",
        "interpreted_as": None,
        "research_summary": {
            "route": "general_finance",
            "source": "General finance explanation; no market data used.",
            "database_queries": 0,
            "live_quote_requests": 0,
        },
    }


def test_chat_endpoint_rejects_blank_messages() -> None:
    response = TestClient(dashboard_app.app, base_url="http://127.0.0.1").post(
        "/api/chat", json={"message": " "}
    )
    assert response.status_code == 422


def test_chat_endpoint_passes_history_and_shows_the_rewrite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[list[Turn]] = []

    def fake_ask(message: str, *, history: list[Turn]) -> SimpleNamespace:
        seen.append(history)
        return SimpleNamespace(
            standalone="I have 100 HDFC Bank shares, should I keep or sell them?",
            text="HDFC BANK LTD (HDFCBANK): review",
            route=SimpleNamespace(value="advice"),
            paths=[],
        )

    monkeypatch.setattr(dashboard_app, "ask", fake_ask)
    response = TestClient(dashboard_app.app, base_url="http://127.0.0.1").post(
        "/api/chat",
        json={
            "message": "reply to above question",
            "history": [
                {"role": "user", "content": "I have 100 HDFC Bank shares, keep?"},
                {"role": "assistant", "content": "  "},
            ],
        },
    )

    assert response.status_code == 200
    assert seen == [[Turn("user", "I have 100 HDFC Bank shares, keep?")]]
    assert response.json()["interpreted_as"] == (
        "I have 100 HDFC Bank shares, should I keep or sell them?"
    )


@pytest.mark.parametrize(
    "history",
    ["not a list", [{"role": "system", "content": "x"}], [{"role": "user"}]],
)
def test_chat_endpoint_rejects_malformed_history(history: object) -> None:
    response = TestClient(dashboard_app.app, base_url="http://127.0.0.1").post(
        "/api/chat", json={"message": "and Infosys?", "history": history}
    )
    assert response.status_code == 422


def test_removed_import_and_stock_endpoints_are_gone() -> None:
    client = TestClient(dashboard_app.app, base_url="http://127.0.0.1")
    assert client.post("/api/import").status_code == 404
    assert client.get("/api/template").status_code == 404
    assert client.get("/api/stocks").status_code == 404
    assert client.post("/api/portfolio/review", json={}).status_code == 404


def _raising_ask(exc: Exception) -> Any:
    def fake_ask(message: str, *, history: list[Turn]) -> SimpleNamespace:
        raise exc

    return fake_ask


def test_chat_maps_model_errors_to_503(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dashboard_app, "ask", _raising_ask(OllamaError("model down")))
    response = TestClient(dashboard_app.app, base_url="http://127.0.0.1").post(
        "/api/chat", json={"message": "hi"}
    )
    assert response.status_code == 503
    assert response.json()["detail"] == "model down"


def test_chat_logs_unexpected_errors_and_hides_them(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(
        dashboard_app, "ask", _raising_ask(RuntimeError("secret dsn leak"))
    )
    with caplog.at_level(logging.ERROR, logger="sra.dashboard.app"):
        response = TestClient(dashboard_app.app, base_url="http://127.0.0.1").post(
            "/api/chat", json={"message": "hi"}
        )
    assert response.status_code == 500
    assert "secret" not in response.text
    record = next(r for r in caplog.records if r.name == "sra.dashboard.app")
    assert record.exc_info is not None


@pytest.mark.parametrize("path", ["/", "/api/health", "/nope"])
def test_security_headers_on_every_response(path: str) -> None:
    response = TestClient(dashboard_app.app, base_url="http://127.0.0.1").get(path)
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert "script-src 'unsafe-inline'" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"


def test_cross_origin_post_is_rejected_with_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(message: str, *, history: list[Turn]) -> SimpleNamespace:
        raise AssertionError("must not reach the agent")

    monkeypatch.setattr(dashboard_app, "ask", fail)
    client = TestClient(dashboard_app.app, base_url="http://127.0.0.1")
    for origin in ("http://evil.example", "http://127.0.0.1:9999", "null"):
        response = client.post(
            "/api/chat", json={"message": "hi"}, headers={"Origin": origin}
        )
        assert response.status_code == 403
        assert response.headers["x-frame-options"] == "DENY"


def test_same_origin_post_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_ask(message: str, *, history: list[Turn]) -> SimpleNamespace:
        return SimpleNamespace(
            standalone=message,
            text="ok",
            route=SimpleNamespace(value="out_of_scope"),
            paths=[],
        )

    monkeypatch.setattr(dashboard_app, "ask", fake_ask)
    response = TestClient(dashboard_app.app, base_url="http://127.0.0.1").post(
        "/api/chat", json={"message": "hi"}, headers={"Origin": "http://127.0.0.1"}
    )
    assert response.status_code == 200


class _Conn:
    def __enter__(self) -> "_Conn":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str) -> None:
        assert sql == "SELECT 1"


def _stub_dependencies(
    monkeypatch: pytest.MonkeyPatch, *, db_ok: bool, models: list[str] | None
) -> None:
    monkeypatch.setattr(
        dashboard_app,
        "settings",
        lambda: SimpleNamespace(
            readonly_dsn="postgresql://x",
            ollama_url="http://ollama",
            generation_model="gemma4:latest",
        ),
    )

    def connect(dsn: str, **kwargs: Any) -> _Conn:
        assert kwargs["connect_timeout"] >= 1
        if not db_ok:
            raise psycopg.OperationalError("connection refused")
        return _Conn()

    def get(url: str, **kwargs: Any) -> httpx.Response:
        assert url == "http://ollama/api/tags" and kwargs["timeout"] <= 5
        if models is None:
            raise httpx.ConnectError("refused")
        body = {"models": [{"name": name} for name in models]}
        return httpx.Response(200, json=body, request=httpx.Request("GET", url))

    monkeypatch.setattr(psycopg, "connect", connect)
    monkeypatch.setattr(httpx, "get", get)


def test_health_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_dependencies(monkeypatch, db_ok=True, models=["gemma4:latest"])
    response = TestClient(dashboard_app.app, base_url="http://127.0.0.1").get(
        "/api/health"
    )
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "database": {"ok": True},
        "model": {"ok": True},
    }


@pytest.mark.parametrize(
    ("db_ok", "models", "failing"),
    [
        (False, ["gemma4:latest"], "database"),
        (True, None, "model"),
        (True, ["other:latest"], "model"),
    ],
)
def test_health_reports_the_failing_dependency(
    monkeypatch: pytest.MonkeyPatch,
    db_ok: bool,
    models: list[str] | None,
    failing: str,
) -> None:
    _stub_dependencies(monkeypatch, db_ok=db_ok, models=models)
    response = TestClient(dashboard_app.app, base_url="http://127.0.0.1").get(
        "/api/health"
    )
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "unavailable"
    assert body[failing]["ok"] is False
    assert "refused" not in response.text


def _ok_ask(message: str, *, history: list[Turn]) -> SimpleNamespace:
    return SimpleNamespace(
        standalone=message,
        text="ok",
        route=SimpleNamespace(value="out_of_scope"),
        paths=[],
    )


def test_rebound_host_is_rejected_even_with_matching_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(message: str, *, history: list[Turn]) -> SimpleNamespace:
        raise AssertionError("must not reach the agent")

    monkeypatch.setattr(dashboard_app, "ask", fail)
    client = TestClient(dashboard_app.app, base_url="http://evil.example:8000")
    get = client.get("/", headers={"Origin": "http://evil.example:8000"})
    post = client.post(
        "/api/chat",
        json={"message": "hi"},
        headers={"Origin": "http://evil.example:8000"},
    )
    assert get.status_code == 403
    assert post.status_code == 403
    assert post.headers["x-frame-options"] == "DENY"


@pytest.mark.parametrize(
    "host", ["localhost:8001", "127.0.0.1", "127.0.0.1:9", "[::1]:8000", "LOCALHOST"]
)
def test_loopback_hosts_are_allowed(host: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dashboard_app, "ask", _ok_ask)
    client = TestClient(dashboard_app.app, base_url=f"http://{host}")
    assert client.get("/").status_code == 200
    origin = f"http://{host}"
    response = client.post(
        "/api/chat", json={"message": "hi"}, headers={"Origin": origin}
    )
    assert response.status_code == 200


def test_malformed_host_header_is_rejected() -> None:
    client = TestClient(dashboard_app.app, base_url="http://127.0.0.1")
    assert client.get("/", headers={"Host": "[bad"}).status_code == 403
    assert client.get("/", headers={"Host": ""}).status_code == 403


def test_run_allows_a_concrete_bind_host_but_not_a_wildcard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda *_a, **_k: None)
    monkeypatch.setattr(
        dashboard_app.app.state, "extra_hosts", frozenset(), raising=False
    )
    dashboard_app.run(host="dash.lan", port=1)
    client = TestClient(dashboard_app.app, base_url="http://dash.lan:1")
    assert client.get("/").status_code == 200
    dashboard_app.run(host="0.0.0.0", port=1)
    assert client.get("/").status_code == 403
    other = TestClient(dashboard_app.app, base_url="http://192.168.1.5:1")
    assert other.get("/").status_code == 403


@pytest.mark.parametrize("message", [123, ["hi"], {"a": 1}, True])
def test_chat_rejects_non_string_message(message: object) -> None:
    response = TestClient(dashboard_app.app, base_url="http://127.0.0.1").post(
        "/api/chat", json={"message": message}
    )
    assert response.status_code == 422
    assert "string" in response.json()["detail"]


def test_chat_rejects_overlong_message(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dashboard_app, "ask", _ok_ask)
    client = TestClient(dashboard_app.app, base_url="http://127.0.0.1")
    too_long = "x" * (dashboard_app.MAX_TURN_CHARS + 1)
    response = client.post("/api/chat", json={"message": too_long})
    assert response.status_code == 422
    assert str(dashboard_app.MAX_TURN_CHARS) in response.json()["detail"]
    at_limit = "x" * dashboard_app.MAX_TURN_CHARS
    assert client.post("/api/chat", json={"message": at_limit}).status_code == 200
