import json
from dataclasses import dataclass, field
from typing import Any

import httpx

from sra.agent.prompts import (
    NARRATIVE_SYSTEM_PROMPT,
    NUMERIC_SYSTEM_PROMPT,
    RUN_SQL_TOOL,
    SEARCH_FILINGS_TOOL,
)
from sra.config import settings
from sra.tools.run_sql import SqlResult, run_sql
from sra.tools.search_filings import DEFAULT_K, SearchResult, search_filings

MAX_TOOL_ROUNDS = 6

# A model that reissues a query it already ran is stuck rather than working;
# without this it burns every remaining round on the same call and returns
# nothing. Observed when a question needs a figure that XBRL does not tag.
MAX_REPEATED_CALLS = 2


@dataclass
class PathResult:
    """What one path produced, kept separate from the other path's evidence."""

    text: str = ""
    sql_calls: list[SqlResult] = field(default_factory=list)
    search_calls: list[SearchResult] = field(default_factory=list)
    rounds: int = 0
    stopped_early: bool = False

    @property
    def accessions(self) -> list[str]:
        """Accession numbers this path actually read, in first-seen order."""
        seen: dict[str, None] = {}
        for call in self.sql_calls:
            for row in call.rows:
                value = row.get("accession_no")
                if isinstance(value, str):
                    seen.setdefault(value, None)
        for search in self.search_calls:
            for passage in search.passages:
                seen.setdefault(passage.accession_no, None)
        return list(seen)

    @property
    def periods(self) -> list[str]:
        """Fiscal periods the SQL results covered, most recent first.

        Used to scope the narrative path to the same periods the figures cover,
        so a composed answer does not explain one quarter with another
        quarter's commentary.
        """
        labels: dict[str, str] = {}
        for call in self.sql_calls:
            for row in call.rows:
                period_end = row.get("period_end")
                if not isinstance(period_end, str):
                    continue
                year = row.get("fiscal_year")
                period = row.get("fiscal_period")
                label = (
                    f"{period} {year} (ended {period_end})"
                    if year is not None and period is not None
                    else f"period ended {period_end}"
                )
                labels[period_end] = label
        return [labels[key] for key in sorted(labels, reverse=True)]

    @property
    def cited_values(self) -> set[str]:
        """Every value run_sql returned, for the hallucinated-number check."""
        return {
            str(value)
            for call in self.sql_calls
            for row in call.rows
            for value in row.values()
            if value is not None
        }

    @property
    def cited_sections(self) -> set[tuple[str, str]]:
        """(section, accession) pairs actually retrieved, for citation scoring."""
        return {
            (passage.section, passage.accession_no)
            for call in self.search_calls
            for passage in call.passages
        }


class OllamaError(RuntimeError):
    pass


class ModelUnavailableError(OllamaError):
    """The local model server could not be reached at all."""


def _chat(
    client: httpx.Client,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
) -> dict[str, Any]:
    try:
        response = _post_chat(client, messages, tools)
    except httpx.ConnectError as exc:
        raise ModelUnavailableError(
            f"cannot reach the model server at {settings().ollama_url}. "
            "Start it with `ollama serve`."
        ) from exc
    except httpx.TimeoutException as exc:
        raise ModelUnavailableError(
            f"the model server at {settings().ollama_url} did not respond in time."
        ) from exc
    if response.status_code == 404:
        raise OllamaError(
            f"model {settings().generation_model!r} is not installed. "
            f"Pull it with `ollama pull {settings().generation_model}`."
        )
    if response.status_code != 200:
        raise OllamaError(f"{response.status_code}: {response.text[:300]}")
    payload = response.json()
    if not isinstance(payload, dict):
        raise OllamaError(f"expected a JSON object from /api/chat, got {type(payload)}")
    return payload


def _post_chat(
    client: httpx.Client,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
) -> httpx.Response:
    return client.post(
        "/api/chat",
        json={
            "model": settings().generation_model,
            "messages": messages,
            "tools": tools,
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


def _optional_str(value: object) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _dispatch(name: str, arguments: dict[str, Any], result: PathResult) -> str:
    """Run one tool call and record it for later scoring."""
    if name == "run_sql":
        sql_result = run_sql(str(arguments.get("sql", "")))
        result.sql_calls.append(sql_result)
        return sql_result.to_text()
    if name == "search_filings":
        raw_k = arguments.get("k")
        found = search_filings(
            str(arguments.get("query", "")),
            ticker=_optional_str(arguments.get("ticker")),
            form_type=_optional_str(arguments.get("form_type")),
            section=_optional_str(arguments.get("section")),
            k=int(raw_k) if isinstance(raw_k, int) else DEFAULT_K,
        )
        result.search_calls.append(found)
        return found.to_text()
    # A path is given only its own tool, so this means the model invented one.
    return f"ERROR: no tool named {name!r} is available"


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

    with httpx.Client(
        base_url=settings().ollama_url, timeout=httpx.Timeout(600.0)
    ) as client:
        for round_index in range(max_rounds):
            payload = _chat(client, messages, tools)
            message = payload.get("message", {})
            tool_calls = message.get("tool_calls") or []
            result.rounds = round_index + 1

            if not tool_calls:
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
                    content = _dispatch(name, arguments, result)
                messages.append(
                    {"role": "tool", "tool_name": name or "unknown", "content": content}
                )

    result.stopped_early = True
    result.text = f"Stopped after {max_rounds} tool rounds without an answer."
    return result


def numeric_path(question: str, **kwargs: Any) -> PathResult:
    """Figures only. Has no way to read filing text."""
    return run_path(
        question,
        system_prompt=NUMERIC_SYSTEM_PROMPT,
        tools=[RUN_SQL_TOOL],
        **kwargs,
    )


def narrative_path(question: str, **kwargs: Any) -> PathResult:
    """Filing text only. Has no way to look up a figure."""
    return run_path(
        question,
        system_prompt=NARRATIVE_SYSTEM_PROMPT,
        tools=[SEARCH_FILINGS_TOOL],
        **kwargs,
    )
