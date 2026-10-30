"""Put a call and its evidence into words, in an analyst's voice.

The local model writes the note; code decides what it may say. The note must
state the call it was given, and every number in it must be one of the
numbers in the brief (rounding allowed), so a figure can be rephrased but
never invented. A note that fails either check, or a model that is not
running, gives None and the caller falls back to prose assembled in code.
"""

import logging
import re
from decimal import Decimal, InvalidOperation

from sra.agent.loop import OllamaError, chat, ollama_client
from sra.agent.verdict import Action

logger = logging.getLogger(__name__)

NARRATE_TIMEOUT_SECONDS = 180.0
MIN_CHARS, MAX_CHARS = 200, 2400
# "12 months", "3 paragraphs": small whole numbers need no source.
SMALL_NUMBER = 12

SYSTEM_PROMPT = """\
You are a senior equity research analyst at an Indian brokerage, writing a
short note to a client about one NSE stock. Write like a person talking to a
person: plain words, direct and warm, always "I", never "we".

The call is already decided and is in the brief. Open with it as your own
view, without hedging ("I'd hold", "I'd sell", "I'd wait"), then argue it
from the evidence. Do not change it, water it down or recommend a different
action.

Rules:
- Use only facts from the brief. Copy every number exactly as the brief
  writes it. Do not work out new numbers, give price targets or forecasts,
  or mention news, management or anything else the brief does not contain.
- If the client owns shares, say what to do with their position and refer
  to their gain or loss when the brief gives one.
- Weigh the evidence against honestly. Talk about the evidence, not the
  score. Do not say what would change your mind: that is added separately.
- Explain a ratio in a short phrase, at most once.
- 2 short paragraphs, 100 to 180 words in all. No headings, no bullet
  points, no disclaimer (one is added separately).
"""

_CALL_WORDS = {
    Action.BUY_MORE: r"\b(buy(ing)? more|add(ing)? (more|to))\b",
    Action.HOLD: r"\b(hold|holding|keep|stay invested)\b",
    Action.REDUCE: r"\b(reduc\w*|trim\w*|book\w* (some|part))",
    Action.SELL: r"\b(sell\w*|exit\w*)",
    Action.BUY: r"\bbuy\w*",
    Action.WAIT: r"\b(wait\w*|hold off|sidelines|not yet)\b",
    Action.AVOID: r"\b(avoid\w*|stay away|skip)\b",
}
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _value(raw: str) -> Decimal | None:
    try:
        return Decimal(raw.rstrip(",").replace(",", ""))
    except InvalidOperation:
        return None


def numbers_check(note: str, brief: str) -> bool:
    """Whether every number in the note is a number in the brief, as written
    or rounded to fewer decimals."""
    allowed = {v for v in map(_value, _NUMBER.findall(brief)) if v is not None}
    for raw in _NUMBER.findall(note):
        value = _value(raw)
        if value is None:
            return False
        if value <= SMALL_NUMBER and value == value.to_integral_value():
            continue
        if not any(
            value == known or any(value == round(known, places) for places in (0, 1))
            for known in allowed
        ):
            return False
    return True


def _paragraphs(text: str) -> str:
    """One line per paragraph, so the chat shows prose, not a list."""
    blocks = re.split(r"\n\s*\n", text.strip())
    return "\n\n".join(" ".join(block.split()) for block in blocks if block.strip())


def narrate(brief: str, action: Action) -> str | None:
    """The analyst's note for this brief, or None when it cannot be trusted."""
    try:
        payload = chat(
            ollama_client(),
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": brief},
            ],
            [],
            think=False,
            timeout=NARRATE_TIMEOUT_SECONDS,
        )
    except OllamaError as exc:
        logger.info("narration skipped: %s", exc)
        return None
    note = _paragraphs(str(payload.get("message", {}).get("content") or ""))
    if not MIN_CHARS <= len(note) <= MAX_CHARS:
        logger.info("narration rejected: %d characters", len(note))
        return None
    if not re.search(_CALL_WORDS[action], note, re.IGNORECASE):
        logger.info("narration rejected: does not state the call %r", action.value)
        return None
    if not numbers_check(note, brief):
        logger.info("narration rejected: a number is not in the brief")
        return None
    return note
