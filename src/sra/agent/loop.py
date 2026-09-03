import json
from dataclasses import dataclass, field
from typing import Any

import httpx

from sra.agent.prompts import RUN_SQL_TOOL, SEARCH_FILINGS_TOOL, SYSTEM_PROMPT
from sra.config import settings
from sra.tools.run_sql import SqlResult, run_sql
from sra.tools.search_filings import DEFAULT_K, SearchResult, search_filings

MAX_TOOL_ROUNDS = 6


@dataclass
class Answer:
    question: str
    text: str
    sql_calls: list[SqlResult] = field(default_factory=list)
    search_calls: list[SearchResult] = field(default_factory=list)
    rounds: int = 0
    stopped_early: bool = False

    @property
    def cited_sections(self) -> set[tuple[str, str]]:
        """(section, accession) pairs actually retrieved, for citation scoring."""
        return {
            (passage.section, passage.accession_no)
            for call in self.search_calls
            for passage in call.passages
        }

    @property
    def cited_values(self) -> set[str]:
        """Every value the tool actually returned.

        This is the evidence set the week-4 hallucinated-number check scores the
        answer text against, so it is collected on every run, not just in eval.
        """
        return {
            str(value)
            for call in self.sql_calls
            for row in call.rows
            for value in row.values()
            if value is not None
        }


class OllamaError(RuntimeError):
    pass


def _chat(client: httpx.Client, messages: list[dict[str, Any]]) -> dict[str, Any]:
    response = client.post(
        "/api/chat",
        json={
            "model": settings().generation_model,
            "messages": messages,
            "tools": [RUN_SQL_TOOL, SEARCH_FILINGS_TOOL],
            "stream": False,
            "options": {
                # Figures are read from tool output, not sampled; keep the
                # wording and the SQL as deterministic as the runtime allows.
                "temperature": 0.0,
                "num_ctx": settings().context_tokens,
                "num_predict": settings().max_output_tokens,
            },
        },
    )
    if response.status_code != 200:
        raise OllamaError(f"{response.status_code}: {response.text[:300]}")
    payload = response.json()
    if not isinstance(payload, dict):
        raise OllamaError(f"expected a JSON object from /api/chat, got {type(payload)}")
    return payload


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


def _dispatch(name: str, arguments: dict[str, Any], answer: "Answer") -> str:
    """Run one tool call and record it on the answer for later scoring."""
    if name == "run_sql":
        result = run_sql(str(arguments.get("sql", "")))
        answer.sql_calls.append(result)
        return result.to_text()
    if name == "search_filings":
        raw_k = arguments.get("k")
        found = search_filings(
            str(arguments.get("query", "")),
            ticker=_optional_str(arguments.get("ticker")),
            form_type=_optional_str(arguments.get("form_type")),
            section=_optional_str(arguments.get("section")),
            k=int(raw_k) if isinstance(raw_k, int) else DEFAULT_K,
        )
        answer.search_calls.append(found)
        return found.to_text()
    return f"ERROR: no tool named {name!r}"


def _optional_str(value: object) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def ask(question: str, *, max_rounds: int = MAX_TOOL_ROUNDS) -> Answer:
    cfg = settings()
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]
    answer = Answer(question=question, text="")

    with httpx.Client(base_url=cfg.ollama_url, timeout=httpx.Timeout(300.0)) as client:
        for round_index in range(max_rounds):
            payload = _chat(client, messages)
            message = payload.get("message", {})
            tool_calls = message.get("tool_calls") or []
            answer.rounds = round_index + 1

            if not tool_calls:
                answer.text = (message.get("content") or "").strip()
                return answer

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
                content = _dispatch(name, arguments, answer)
                messages.append(
                    {"role": "tool", "tool_name": name or "unknown", "content": content}
                )

    answer.stopped_early = True
    answer.text = f"Stopped after {max_rounds} tool rounds without reaching an answer."
    return answer
