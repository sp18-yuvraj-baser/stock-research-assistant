import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from sra.agent import loop
from sra.agent.loop import (
    NO_ANSWER,
    ModelUnavailableError,
    OllamaError,
    PathResult,
    chat,
    realtime_path,
    run_path,
)
from sra.agent.prompts import FUNDAMENTALS_SYSTEM_PROMPT, LIVE_QUOTE_TOOL, RUN_SQL_TOOL
from sra.tools.run_sql import SqlResult

Handler = Callable[[httpx.Request], httpx.Response]


def _client(handler: Handler) -> httpx.Client:
    return httpx.Client(
        base_url="http://ollama.test", transport=httpx.MockTransport(handler)
    )


def _serve(monkeypatch: pytest.MonkeyPatch, handler: Handler) -> list[dict[str, Any]]:
    """Point the shared Ollama client at a fake server; returns request bodies."""
    bodies: list[dict[str, Any]] = []

    def recording(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return handler(request)

    monkeypatch.setattr(loop, "ollama_client", lambda: _client(recording))
    return bodies


def _reply(
    content: str = "",
    *,
    tool_calls: list[dict[str, Any]] | None = None,
    done_reason: str = "stop",
) -> httpx.Response:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return httpx.Response(200, json={"message": message, "done_reason": done_reason})


def _call(name: str, **arguments: Any) -> dict[str, Any]:
    return {"function": {"name": name, "arguments": arguments}}


def _script(*replies: httpx.Response) -> Handler:
    queue = list(replies)
    return lambda request: queue.pop(0)


# chat: every failure is an OllamaError


def test_chat_returns_the_payload() -> None:
    payload = chat(_client(lambda r: _reply("hi")), [], [])
    assert payload["message"]["content"] == "hi"


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError("refused"),
        httpx.ReadTimeout("slow"),
        httpx.RemoteProtocolError("reset"),
        httpx.ReadError("broken pipe"),
    ],
)
def test_transport_errors_become_model_unavailable(exc: httpx.HTTPError) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    with pytest.raises(ModelUnavailableError):
        chat(_client(handler), [], [])


def test_http_status_error_becomes_ollama_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.HTTPStatusError(
            "boom", request=request, response=httpx.Response(502, text="bad gateway")
        )

    with pytest.raises(OllamaError, match="502") as info:
        chat(_client(handler), [], [])
    assert not isinstance(info.value, ModelUnavailableError)


def test_missing_model_and_server_errors_become_ollama_errors() -> None:
    with pytest.raises(OllamaError, match="not installed"):
        chat(_client(lambda r: httpx.Response(404)), [], [])
    with pytest.raises(OllamaError, match="500: oops"):
        chat(_client(lambda r: httpx.Response(500, text="oops")), [], [])


@pytest.mark.parametrize("body", [b"<html>not json", b"[1, 2]"])
def test_invalid_json_becomes_ollama_error(body: bytes) -> None:
    with pytest.raises(OllamaError):
        chat(_client(lambda r: httpx.Response(200, content=body)), [], [])


def test_think_is_sent_only_when_given() -> None:
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return _reply("ok")

    client = _client(handler)
    chat(client, [], [])
    chat(client, [], [], think=False)
    assert "think" not in bodies[0]
    assert bodies[1]["think"] is False


# run_path


def test_tool_round_trip_feeds_the_result_back(monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[str] = []

    def fake_sql(sql: str) -> SqlResult:
        ran.append(sql)
        return SqlResult(sql=sql, columns=["pe"], rows=[{"pe": 25}])

    monkeypatch.setattr(loop, "run_sql", fake_sql)
    bodies = _serve(
        monkeypatch,
        _script(
            _reply(tool_calls=[_call("run_sql", sql="SELECT 1")]),
            _reply("P/E is 25."),
        ),
    )
    result = run_path("pe?", system_prompt="s", tools=[RUN_SQL_TOOL])
    assert result.text == "P/E is 25."
    assert result.rounds == 2
    assert ran == ["SELECT 1"]
    assert len(result.sql_calls) == 1
    tool_message = bodies[1]["messages"][-1]
    assert tool_message["role"] == "tool"
    assert "25" in tool_message["content"]


def test_a_tool_the_path_was_not_offered_does_not_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(sql: str) -> SqlResult:
        raise AssertionError("run_sql must not run on the realtime path")

    monkeypatch.setattr(loop, "run_sql", fail)
    bodies = _serve(
        monkeypatch,
        _script(
            _reply(tool_calls=[_call("run_sql", sql="SELECT 1")]),
            _reply("Cannot."),
        ),
    )
    result = run_path("q", system_prompt="s", tools=[LIVE_QUOTE_TOOL])
    assert result.sql_calls == []
    assert bodies[1]["messages"][-1]["content"].startswith("ERROR: no tool named")


def test_path_with_no_tools_offers_none_to_the_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        loop, "get_live_quote", lambda t: pytest.fail("quote must not run")
    )
    bodies = _serve(
        monkeypatch,
        _script(
            _reply(tool_calls=[_call("get_live_quote", ticker="TCS")]),
            _reply("done"),
        ),
    )
    result = run_path("q", system_prompt="s", tools=[])
    assert result.text == "done"
    assert bodies[0]["tools"] == []
    assert result.quote_calls == []


def test_a_tool_that_raises_is_reported_to_the_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(ticker: str) -> object:
        raise RuntimeError("db is down")

    monkeypatch.setattr(loop, "get_live_quote", boom)
    bodies = _serve(
        monkeypatch,
        _script(
            _reply(tool_calls=[_call("get_live_quote", ticker="TCS")]),
            _reply("The price could not be fetched."),
        ),
    )
    result = realtime_path("TCS price")
    assert result.text == "The price could not be fetched."
    content = bodies[1]["messages"][-1]["content"]
    assert content.startswith("ERROR:")
    assert "RuntimeError" in content


def test_blank_answer_is_retried_without_thinking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bodies = _serve(monkeypatch, _script(_reply(""), _reply("Real answer.")))
    result = run_path("q", system_prompt="s", tools=[])
    assert result.text == "Real answer."
    assert not result.stopped_early
    assert "think" not in bodies[0]
    assert bodies[1]["think"] is False


def test_truncated_answer_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    bodies = _serve(
        monkeypatch,
        _script(_reply("partial", done_reason="length"), _reply("Full answer.")),
    )
    result = run_path("q", system_prompt="s", tools=[])
    assert result.text == "Full answer."
    assert len(bodies) == 2


@pytest.mark.parametrize("first", [_reply(""), _reply("cut", done_reason="length")])
def test_still_no_answer_is_reported_not_returned_blank(
    monkeypatch: pytest.MonkeyPatch, first: httpx.Response
) -> None:
    bodies = _serve(monkeypatch, _script(first, _reply("", done_reason="length")))
    result = run_path("q", system_prompt="s", tools=[])
    assert result.text == NO_ANSWER
    assert result.stopped_early
    assert len(bodies) == 2


def test_the_retry_happens_once_per_question(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        loop, "run_sql", lambda sql: SqlResult(sql=sql, columns=[], rows=[])
    )
    bodies = _serve(
        monkeypatch,
        _script(
            _reply(""),
            _reply(tool_calls=[_call("run_sql", sql="SELECT 1")]),
            _reply(""),
        ),
    )
    result = run_path("q", system_prompt="s", tools=[RUN_SQL_TOOL])
    assert result.text == NO_ANSWER
    assert len(bodies) == 3


def test_a_repeated_call_is_refused_after_the_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runs: list[str] = []

    def fake_sql(sql: str) -> SqlResult:
        runs.append(sql)
        return SqlResult(sql=sql, columns=[], rows=[])

    monkeypatch.setattr(loop, "run_sql", fake_sql)
    same = _reply(tool_calls=[_call("run_sql", sql="SELECT 1")])
    bodies = _serve(monkeypatch, _script(same, same, same, _reply("answered")))
    result = run_path("q", system_prompt="s", tools=[RUN_SQL_TOOL])
    assert result.text == "answered"
    assert len(runs) == loop.MAX_REPEATED_CALLS
    assert "already made" in bodies[3]["messages"][-1]["content"]


def test_running_out_of_rounds_stops_early(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        loop, "run_sql", lambda sql: SqlResult(sql=sql, columns=[], rows=[])
    )
    calls = [_reply(tool_calls=[_call("run_sql", sql=f"SELECT {n}")]) for n in range(3)]
    _serve(monkeypatch, _script(*calls))
    result: PathResult = run_path(
        "q", system_prompt="s", tools=[RUN_SQL_TOOL], max_rounds=3
    )
    assert result.stopped_early
    assert "3 tool rounds" in result.text


def test_string_arguments_are_decoded() -> None:
    call = {"function": {"name": "run_sql", "arguments": '{"sql": "SELECT 1"}'}}
    assert loop._tool_arguments(call) == {"sql": "SELECT 1"}
    assert loop._tool_arguments({"function": {"arguments": "not json"}}) == {}


def test_fundamentals_prompt_orders_periods_by_period_end() -> None:
    prompt = FUNDAMENTALS_SYSTEM_PROMPT
    assert "period_end" in prompt
    assert "ORDER BY curr.period_end DESC" in prompt
    # The old text-comparison query and the stale labels and coverage claims.
    assert "p2.period <" not in prompt
    assert "ORDER BY curr.period DESC" not in prompt
    for stale in ("FY2025", "Q2FY26", "Nifty", "46 of the 52", "6 tracked"):
        assert stale not in prompt


def test_every_model_call_shares_one_client() -> None:
    loop.ollama_client.cache_clear()
    try:
        assert loop.ollama_client() is loop.ollama_client()
    finally:
        loop.ollama_client.cache_clear()
