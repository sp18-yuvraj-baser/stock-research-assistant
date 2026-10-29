"""FastAPI entry point for the local research dashboard."""

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import psycopg
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse

from sra.agent.compose import Answer, ask
from sra.agent.followup import Turn
from sra.agent.loop import OllamaError
from sra.config import settings

logger = logging.getLogger(__name__)

STATIC_PATH = Path(__file__).with_name("index.html")
app = FastAPI(title="Indian Equity Research Dashboard", docs_url=None, redoc_url=None)
# The browser holds the conversation; only the recent part is ever used.
MAX_HISTORY_TURNS = 20
MAX_TURN_CHARS = 4000
# Health probes must answer quickly even when a dependency is down.
HEALTH_TIMEOUT_SECONDS = 2.0

# The page is one file with an inline script and style and talks only to this
# origin, so everything else is denied outright.
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; base-uri 'none'; "
        "form-action 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
# Host names this server answers to. A rebound DNS name points a browser at
# 127.0.0.1 while keeping its own name in Host, so anything else is refused.
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
# Binding to every interface names no single host to allow.
WILDCARD_HOSTS = frozenset({"", "0.0.0.0", "::"})


@app.middleware("http")
async def guard(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Refuse unknown Host names and cross-origin writes; add security headers."""
    if not _host_allowed(request):
        response: Response = JSONResponse(
            {"detail": "Host not allowed."}, status_code=403
        )
    elif request.method not in SAFE_METHODS and _cross_origin(request):
        response = JSONResponse(
            {"detail": "Cross-origin requests are not allowed."}, status_code=403
        )
    else:
        response = await call_next(request)
    response.headers.update(SECURITY_HEADERS)
    return response


def _hostname(host: str) -> str:
    """Lower-cased host name of a Host header value, without port or brackets."""
    try:
        return urlsplit(f"//{host}").hostname or ""
    except ValueError:
        return ""


def _host_allowed(request: Request) -> bool:
    allowed = LOOPBACK_HOSTS | getattr(app.state, "extra_hosts", frozenset())
    return _hostname(request.headers.get("host", "")) in allowed


def _cross_origin(request: Request) -> bool:
    """True when a browser-supplied Origin does not match the Host it reached.

    A page on another site cannot forge Origin, so this stops it driving the
    local server. It does not stop DNS rebinding on its own, because a rebound
    page is same-origin with the rebound Host; _host_allowed covers that.
    Clients that send no Origin, such as curl, are unaffected.
    """
    origin = request.headers.get("origin")
    if origin is None:
        return False
    return urlsplit(origin).netloc.lower() != request.headers.get("host", "").lower()


@app.get("/", response_class=HTMLResponse)
def home() -> str:
    return STATIC_PATH.read_text(encoding="utf-8")


@app.post("/api/chat")
def chat(payload: dict[str, Any]) -> dict[str, Any]:
    """Expose the existing safe research router through the local chat UI."""
    raw_message = payload.get("message", "")
    if not isinstance(raw_message, str):
        raise HTTPException(status_code=422, detail="message must be a string")
    message = raw_message.strip()
    if not message:
        raise HTTPException(status_code=422, detail="Enter a question first.")
    if len(message) > MAX_TURN_CHARS:
        raise HTTPException(
            status_code=422,
            detail=f"Message is too long; keep it under {MAX_TURN_CHARS} characters.",
        )
    history = _history(payload.get("history"))
    try:
        answer = ask(message, history=history)
    except OllamaError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        # Internals stay in the log; the browser gets a generic message.
        logger.exception("chat request failed")
        raise HTTPException(
            status_code=500, detail="Something went wrong answering that. Try again."
        ) from exc
    return {
        "answer": answer.text,
        "route": answer.route.value,
        "interpreted_as": (
            answer.standalone if answer.standalone not in ("", message) else None
        ),
        "research_summary": _research_summary(answer),
    }


def _history(raw: object) -> list[Turn]:
    """Validate the conversation the browser sends back; it is untrusted."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise HTTPException(status_code=422, detail="history must be a list")
    turns = []
    for item in raw[-MAX_HISTORY_TURNS:]:
        if not isinstance(item, dict):
            raise HTTPException(status_code=422, detail="invalid history entry")
        role, content = item.get("role"), item.get("content")
        if role not in ("user", "assistant") or not isinstance(content, str):
            raise HTTPException(status_code=422, detail="invalid history entry")
        if content.strip():
            turns.append(Turn(role, content.strip()[:MAX_TURN_CHARS]))
    return turns


def _research_summary(answer: Answer) -> dict[str, Any]:
    """Return an auditable activity summary without exposing private reasoning."""
    query_count = sum(len(path.sql_calls) for path in answer.paths)
    quote_count = sum(len(path.quote_calls) for path in answer.paths)
    route = answer.route.value
    source = {
        "realtime": "Live last-traded quote from Upstox.",
        "fundamentals": "NSE/BSE fundamentals database.",
        "general_finance": "General finance explanation; no market data used.",
        "advice": "Holding review from live quote and fundamentals; no verdict.",
        "out_of_scope": "Scope check; no market data used.",
    }[route]
    return {
        "route": route,
        "source": source,
        "database_queries": query_count,
        "live_quote_requests": quote_count,
    }


@app.get("/api/health")
def health() -> JSONResponse:
    """Report whether the database and the model server are usable."""
    checks = {"database": _check_database(), "model": _check_model()}
    healthy = all(check["ok"] for check in checks.values())
    return JSONResponse(
        {"status": "ok" if healthy else "unavailable", **checks},
        status_code=200 if healthy else 503,
    )


def _check_database() -> dict[str, Any]:
    try:
        with psycopg.connect(
            settings().readonly_dsn, connect_timeout=int(HEALTH_TIMEOUT_SECONDS)
        ) as conn:
            conn.execute("SELECT 1")
    except Exception as exc:
        logger.warning("health: database check failed: %s", exc)
        return {"ok": False, "detail": "cannot reach the database"}
    return {"ok": True}


def _check_model() -> dict[str, Any]:
    try:
        response = httpx.get(
            f"{settings().ollama_url}/api/tags", timeout=HEALTH_TIMEOUT_SECONDS
        )
        response.raise_for_status()
        installed = {m.get("name") for m in response.json().get("models", [])}
    except Exception as exc:
        logger.warning("health: model server check failed: %s", exc)
        return {"ok": False, "detail": "cannot reach the model server"}
    wanted = settings().generation_model
    # Ollama lists untagged names as "<name>:latest".
    if wanted not in installed and f"{wanted}:latest" not in installed:
        return {"ok": False, "detail": f"model {wanted} is not installed"}
    return {"ok": True}


def run(*, host: str, port: int) -> None:
    import uvicorn

    # A concrete bind host is a name the user chose to reach; a wildcard bind
    # adds nothing beyond loopback.
    name = host.strip().strip("[]").lower()
    app.state.extra_hosts = frozenset() if name in WILDCARD_HOSTS else frozenset({name})
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    uvicorn.run("sra.dashboard.app:app", host=host, port=port, reload=False)
