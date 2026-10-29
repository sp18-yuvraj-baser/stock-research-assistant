"""Turn a follow-up message into a question that stands on its own.

Every path answers one self-contained question, and the router decides from
that question alone. Handing the chat history to the tool-using paths instead
would let a price or ratio from an earlier turn be repeated as if it had just
been fetched. So the history is used once, here, to rewrite "and Infosys?" or
"reply to the above" into a full question; everything downstream is unchanged.
"""

import re
from dataclasses import dataclass
from typing import Literal

import httpx

from sra.agent.loop import OllamaError, chat
from sra.config import settings

# Enough for "what about X?" chains without drowning a small local model.
MAX_HISTORY_MESSAGES = 6
# Assistant answers are long; their opening says what they were about.
MAX_MESSAGE_CHARS = 600
# A rewrite longer than this is the model answering, not rewriting.
MAX_REWRITE_CHARS = 400

REWRITE_PROMPT = """\
You rewrite the user's latest chat message as ONE standalone question for an
Indian stock research assistant. Use the conversation only to fill in what
the latest message refers to: the company, share count, buy price, metric or
earlier question. Keep the user's own wording where you can.

Rules:
- Do not answer the question.
- Do not add any company, number or fact the user did not state.
- Name the company instead of "it", "its", "them" or "this stock". When an
  answer shows its NSE symbol in brackets, e.g. "STATE BANK OF INDIA (SBIN)",
  use that symbol: the data is looked up by symbol, not by nickname.
- A question about a different company keeps the kind of question but not
  the share count or buy price, which belonged to the first company.
- If the latest message already stands on its own, return it unchanged.
- If it asks to repeat or answer an earlier question, return that question.
- Output only the question, on one line, with no preamble or quotes.

Examples, after "User: I have 100 hdfc bank stocks, should I keep or sell
them?" and "Assistant: HDFC BANK LTD (HDFCBANK): review ...":
  "reply to the above" -> I have 100 hdfc bank stocks, should I keep or sell them?
  "what about infosys?" -> Should I keep or sell my Infosys shares?
  "and its live price" -> What is HDFCBANK's live price?
  "I bought them at 1500" -> I have 100 HDFCBANK shares bought at 1500,
    should I keep or sell them?
  "what is ROE?" -> what is ROE?
"""

# Words that point back at the conversation. A message with none of them is
# answered as typed: the small model otherwise "helpfully" ties "What is
# ROE?" to the last company discussed, and skipping it saves a model call.
_REFERS_BACK = re.compile(
    r"\b(it|its|it's|them|they|their|this|that|these|those|he|she|above|same"
    r"|previous|earlier|last|again|also|too|instead|else|more|why|how come"
    r"|what about|how about|and|then|so|compare|vs|versus|both|either|other)\b",
    re.IGNORECASE,
)

_PREAMBLE = re.compile(r"^(standalone|rewritten|question)[^:]{0,20}:\s*", re.IGNORECASE)


@dataclass(frozen=True)
class Turn:
    role: Literal["user", "assistant"]
    content: str


def _transcript(question: str, history: list[Turn]) -> str:
    lines = []
    for turn in history[-MAX_HISTORY_MESSAGES:]:
        text = " ".join(turn.content.split())
        if len(text) > MAX_MESSAGE_CHARS:
            text = text[:MAX_MESSAGE_CHARS] + " …"
        lines.append(f"{'User' if turn.role == 'user' else 'Assistant'}: {text}")
    return (
        "Conversation so far:\n"
        + "\n".join(lines)
        + f"\n\nLatest message: {question}\n\nStandalone question:"
    )


def _clean(rewrite: str) -> str:
    line = next((part for part in rewrite.splitlines() if part.strip()), "")
    return _PREAMBLE.sub("", line.strip()).strip().strip("\"'“”").strip()


def standalone_question(question: str, history: list[Turn]) -> str:
    """The question to route and answer; the original when there is nothing
    to resolve or the rewrite cannot be trusted."""
    if not any(turn.role == "user" for turn in history):
        return question
    if not _REFERS_BACK.search(question):
        return question
    messages = [
        {"role": "system", "content": REWRITE_PROMPT},
        {"role": "user", "content": _transcript(question, history)},
    ]
    try:
        with httpx.Client(
            base_url=settings().ollama_url, timeout=httpx.Timeout(120.0)
        ) as client:
            payload = chat(client, messages, [], think=False)
    except OllamaError:
        # The paths that need the model will report it; the ones that do not
        # (holding review) can still answer the message as typed.
        return question
    rewrite = _clean(str(payload.get("message", {}).get("content") or ""))
    if not rewrite or len(rewrite) > MAX_REWRITE_CHARS:
        return question
    return rewrite
