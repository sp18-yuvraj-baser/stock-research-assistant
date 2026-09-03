import re
from dataclasses import dataclass

# Big enough to hold a complete risk-factor paragraph, small enough that a hit
# points at a specific claim rather than a whole page.
TARGET_CHARS = 1800
OVERLAP_CHARS = 200
MIN_CHARS = 120

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


@dataclass(frozen=True)
class Chunk:
    ordinal: int
    text: str


def _split_long_line(line: str, target: int) -> list[str]:
    """Break a paragraph that alone exceeds the target, on sentence boundaries."""
    pieces: list[str] = []
    current = ""
    for sentence in _SENTENCE_END.split(line):
        if current and len(current) + len(sentence) + 1 > target:
            pieces.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        pieces.append(current)
    return pieces


def _tail(text: str, size: int) -> str:
    """The trailing sentences of a chunk, for overlap into the next one."""
    # Guard the zero case explicitly: text[-0:] is the whole string, which
    # would duplicate every chunk instead of disabling overlap.
    if size <= 0:
        return ""
    if len(text) <= size:
        return text
    window = text[-size:]
    match = _SENTENCE_END.search(window)
    return window[match.end() :] if match else window


def chunk_text(
    text: str,
    *,
    target_chars: int = TARGET_CHARS,
    overlap_chars: int = OVERLAP_CHARS,
) -> list[Chunk]:
    """Chunk one Item's text, never crossing into another Item.

    Section boundaries are the meaningful ones, so chunking happens inside a
    section: a fixed window over the whole filing would merge the end of Risk
    Factors into the start of MD&A and make the citation wrong.
    """
    lines: list[str] = []
    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            continue
        if len(line) > target_chars:
            lines.extend(_split_long_line(line, target_chars))
        else:
            lines.append(line)

    chunks: list[str] = []
    current = ""
    for line in lines:
        if current and len(current) + len(line) + 1 > target_chars:
            chunks.append(current)
            # Carry a little context so a claim split across the boundary is
            # still retrievable from the following chunk.
            current = f"{_tail(current, overlap_chars)}\n{line}".strip()
        else:
            current = f"{current}\n{line}".strip()
    if current:
        chunks.append(current)

    # A trailing fragment is merged back rather than stored as its own chunk:
    # an isolated heading or cross-reference retrieves noisily.
    if len(chunks) > 1 and len(chunks[-1]) < MIN_CHARS:
        chunks[-2] = f"{chunks[-2]}\n{chunks[-1]}"
        chunks.pop()

    return [Chunk(ordinal=i, text=t) for i, t in enumerate(chunks)]
