import json
from dataclasses import dataclass, field
from typing import Any

import httpx

from sra.agent.prompts import RUN_SQL_TOOL, SYSTEM_PROMPT
from sra.config import settings
from sra.tools.run_sql import SqlResult, run_sql

MAX_TOOL_ROUNDS = 6


@dataclass
class Answer:
    question: str
    text: str
    sql_calls: list[SqlResult] = field(default_factory=list)
    rounds: int = 0
    stopped_early: bool = False

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
            "tools": [RUN_SQL_TOOL],
            "stream": False,
            # Figures are read from tool output, not sampled; keep the wording
            # and the SQL as deterministic as the runtime allows.
            "options": {"temperature": 0.0},
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
                name = call.get("function", {}).get("name")
                if name != "run_sql":
                    messages.append(
                        {
                            "role": "tool",
                            "tool_name": name or "unknown",
                            "content": f"ERROR: no tool named {name!r}",
                        }
                    )
                    continue
                sql = str(_tool_arguments(call).get("sql", ""))
                result = run_sql(sql)
                answer.sql_calls.append(result)
                messages.append(
                    {
                        "role": "tool",
                        "tool_name": "run_sql",
                        "content": result.to_text(),
                    }
                )

    answer.stopped_early = True
    answer.text = f"Stopped after {max_rounds} tool rounds without reaching an answer."
    return answer
