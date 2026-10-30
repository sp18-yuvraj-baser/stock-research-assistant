"""The model tool loop: one path's prompt and tools against the local Ollama.

Everything a caller can see from here is a PathResult or an OllamaError
subclass: transport, status and JSON failures are mapped in `chat`, tool
failures are fed back to the model, and a blank reply is retried and then
reported, never returned as an empty answer.
"""

import json
import logging
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

import httpx

from sra.agent.prompts import (
    FUNDAMENTALS_SYSTEM_PROMPT,
    GENERAL_FINANCE_SYSTEM_PROMPT,
    LIVE_QUOTE_TOOL,
    REALTIME_SYSTEM_PROMPT,
    RUN_SQL_TOOL,
)
from sra.config import settings
from sra.tools.get_live_quote import LiveQuote, get_live_quote
from sra.tools.run_sql import SqlResult, run_sql

logger = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 6

# A tool round on a local model can take minutes; a one-line rewrite cannot.
ANSWER_TIMEOUT_SECONDS = 600.0

# A model that reissues a query it already ran is stuck rather than working;
# without this it burns every remaining round on the same call and returns
# nothing. Observed when a question needs a figure the tables do not hold.
MAX_REPEATED_CALLS = 2


@dataclass(frozen=True)
class Chart:
    """A price series for the UI to draw under the answer: (ISO date, close)."""

    title: str
    points: list[tuple[str, float]]


@dataclass
class PathResult:
    """What one path produced: the answer and the tool calls behind it."""

    text: str = ""
    sql_calls: list[SqlResult] = field(default_factory=list)
    quote_calls: list[LiveQuote] = field(default_factory=list)
    rounds: int = 0
    stopped_early: bool = False
    chart: Chart | None = None


class OllamaError(RuntimeError):
    pass


class ModelUnavailableError(OllamaError):
    """The local model server could not be reached at all."""


NO_ANSWER = "The model returned no answer. Try asking again or rephrasing the question."


@lru_cache(maxsize=1)
def ollama_client() -> httpx.Client:
    """The process's one Ollama client, so every model call reuses its pooled
    keep-alive connection instead of opening a new one per question. httpx
    clients are thread-safe, so concurrent dashboard requests can share it."""
    return httpx.Client(
        base_url=settings().ollama_url, timeout=httpx.Timeout(ANSWER_TIMEOUT_SECONDS)
    )


def chat(
    client: httpx.Client,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    *,
    think: bool | None = None,
    timeout: float = ANSWER_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """One /api/chat call; every failure surfaces as an OllamaError subclass."""
    try:
        response = _post_chat(client, messages, tools, think=think, timeout=timeout)
    except httpx.ConnectError as exc:
        raise ModelUnavailableError(
            f"cannot reach the model server at {settings().ollama_url}. "
            "Start it with `ollama serve`."
        ) from exc
    except httpx.TimeoutException as exc:
        raise ModelUnavailableError(
            f"the model server at {settings().ollama_url} did not respond in time."
        ) from exc
    except httpx.HTTPStatusError as exc:
        raise OllamaError(
            f"{exc.response.status_code}: {exc.response.text[:300]}"
        ) from exc
    except httpx.HTTPError as exc:
        # A reset or protocol error mid-request: the server is not usable now.
        raise ModelUnavailableError(
            f"the model server at {settings().ollama_url} failed: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    if response.status_code == 404:
        raise OllamaError(
            f"model {settings().generation_model!r} is not installed. "
            f"Pull it with `ollama pull {settings().generation_model}`."
        )
    if response.status_code != 200:
        raise OllamaError(f"{response.status_code}: {response.text[:300]}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise OllamaError(f"/api/chat returned invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise OllamaError(f"expected a JSON object from /api/chat, got {type(payload)}")
    return payload


def _post_chat(
    client: httpx.Client,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    *,
    think: bool | None = None,
    timeout: float = ANSWER_TIMEOUT_SECONDS,
) -> httpx.Response:
    # None leaves the model's own default; a thinking model asked for a
    # one-line rewrite otherwise spends most of its time reasoning about it.
    extra = {} if think is None else {"think": think}
    return client.post(
        "/api/chat",
        timeout=timeout,
        json={
            "model": settings().generation_model,
            "messages": messages,
            "tools": tools,
            "stream": False,
            **extra,
            "options": {
                # Figures are read from tool output, not sampled; keep the
                # wording and the SQL as deterministic as the runtime allows.
                "temperature": 0.0,
                "num_ctx": settings().context_tokens,
                "num_predict": settings().max_output_tokens,
            },
        },
    )


def _tool_arguments(call: dict[str, Any]) -> dict[str, Any]:
    """Ollama returns arguments as an object, but some builds emit a JSON string."""
    raw = call.get("function", {}).get("arguments", {})
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return raw if isinstance(raw, dict) else {}


def _dispatch(
    name: str,
    arguments: dict[str, Any],
    result: PathResult,
    allowed: frozenset[str],
) -> str:
    """Run one tool call and record it for later scoring.

    Only tools the path was offered run: a confused or injected model must not
    reach run_sql from the price path. A tool that raises is reported to the
    model as an ERROR, like any other tool failure, rather than ending the
    question.
    """
    if name not in allowed:
        return f"ERROR: no tool named {name!r} is available"
    try:
        if name == "run_sql":
            sql_result = run_sql(str(arguments.get("sql", "")))
            result.sql_calls.append(sql_result)
            return sql_result.to_text()
        if name == "get_live_quote":
            quote = get_live_quote(str(arguments.get("ticker", "")))
            result.quote_calls.append(quote)
            return quote.to_text()
    except Exception as exc:
        logger.exception("tool %s raised", name)
        return (
            f"ERROR: the {name} tool failed ({type(exc).__name__}). Say the data "
            "could not be fetched; do not guess it."
        )
    return f"ERROR: no tool named {name!r} is available"


def _usable(payload: dict[str, Any]) -> bool:
    """A final answer that is neither blank nor cut off at the output limit."""
    content = ((payload.get("message") or {}).get("content") or "").strip()
    return bool(content) and payload.get("done_reason") != "length"


def run_path(
    question: str,
    *,
    system_prompt: str,
    tools: list[dict[str, Any]],
    max_rounds: int = MAX_TOOL_ROUNDS,
) -> PathResult:
    """Run one tool loop with a single path's prompt and tools."""
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": question},
    ]
    result = PathResult()
    seen_calls: dict[str, int] = {}
    allowed = frozenset(str(tool["function"]["name"]) for tool in tools)
    retried = False

    client = ollama_client()
    for round_index in range(max_rounds):
        payload = chat(client, messages, tools)
        message = payload.get("message", {})
        tool_calls = message.get("tool_calls") or []
        result.rounds = round_index + 1

        if not tool_calls and not _usable(payload) and not retried:
            # A thinking model can spend the whole output budget reasoning
            # and emit no answer; asking again without thinking usually
            # fits. Once per question, so a broken model cannot loop.
            retried = True
            logger.warning(
                "blank or truncated answer (done_reason=%s); retrying without thinking",
                payload.get("done_reason"),
            )
            payload = chat(client, messages, tools, think=False)
            message = payload.get("message", {})
            tool_calls = message.get("tool_calls") or []

        if not tool_calls:
            if not _usable(payload):
                logger.warning(
                    "no usable answer after retry (done_reason=%s)",
                    payload.get("done_reason"),
                )
                result.stopped_early = True
                result.text = NO_ANSWER
                return result
            result.text = (message.get("content") or "").strip()
            return result

        # Keep the assistant turn verbatim so the model sees its own calls.
        messages.append(
            {
                "role": "assistant",
                "content": message.get("content") or "",
                "tool_calls": tool_calls,
            }
        )
        for call in tool_calls:
            name = str(call.get("function", {}).get("name") or "")
            arguments = _tool_arguments(call)
            fingerprint = f"{name}:{json.dumps(arguments, sort_keys=True)}"
            seen_calls[fingerprint] = seen_calls.get(fingerprint, 0) + 1
            if seen_calls[fingerprint] > MAX_REPEATED_CALLS:
                content = (
                    "ERROR: this exact call was already made and returned the "
                    "same rows. Either answer from the results you have, or "
                    "state what is missing. Do not repeat it."
                )
            else:
                content = _dispatch(name, arguments, result, allowed)
            messages.append(
                {"role": "tool", "tool_name": name or "unknown", "content": content}
            )

    result.stopped_early = True
    result.text = f"Stopped after {max_rounds} tool rounds without an answer."
    return result


def realtime_path(question: str, **kwargs: Any) -> PathResult:
    """Live NSE/BSE equity prices only."""
    return run_path(
        question,
        system_prompt=REALTIME_SYSTEM_PROMPT,
        tools=[LIVE_QUOTE_TOOL],
        **kwargs,
    )


def fundamentals_path(question: str, **kwargs: Any) -> PathResult:
    """NSE/BSE ratios and financials only, via run_sql against those tables.

    The run_sql tool is schema-agnostic (sql_guard only validates that the SQL
    is a single read-only statement), so the system prompt describing the
    research tables is all this path adds.

    Research refreshes run explicitly through the CLI rather than starting a
    potentially multi-thousand-company provider sync while a user waits for a
    chat response.
    """
    return run_path(
        question,
        system_prompt=FUNDAMENTALS_SYSTEM_PROMPT,
        tools=[RUN_SQL_TOOL],
        **kwargs,
    )


def general_finance_path(question: str, **kwargs: Any) -> PathResult:
    """Educational finance explanations with no company-data tools."""
    return run_path(
        question,
        system_prompt=GENERAL_FINANCE_SYSTEM_PROMPT,
        tools=[],
        **kwargs,
    )
