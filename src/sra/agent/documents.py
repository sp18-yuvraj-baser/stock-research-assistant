"""Answer questions about an earnings call, annual report or presentation.

The newest matching BSE filing (stored by `sra ingest announcements`) is
fetched, its text extracted with pypdf, and the passages that share the most
words with the question are picked in code. The model may only quote those
passages; every quotation in its answer is checked against them, and if one
is not found word for word the passages themselves are shown instead. With
no model at all, the passages are the answer.
"""

import io
import re
import time
from dataclasses import dataclass
from datetime import datetime

import httpx
import psycopg
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from sra.agent.companies import resolve_companies
from sra.agent.loop import OllamaError, PathResult, chat, ollama_client
from sra.db import connect
from sra.feeds import IST, USER_AGENT

MAX_PDF_BYTES = 15 * 1024 * 1024
MAX_PAGES = 120
# A small PDF can expand into huge text (compressed streams); stop at this
# much text and after this long, whichever comes first.
MAX_TEXT_CHARS = 2_000_000
DOWNLOAD_DEADLINE_SECONDS = 45.0
EXTRACT_DEADLINE_SECONDS = 30.0
PASSAGE_CHARS = 700
PASSAGES_USED = 5
FETCH_TIMEOUT_SECONDS = 30.0

# Asked about, in order of preference: (pattern, categories, what it is).
_KINDS = (
    (r"\bannual report\b", ("annual_report",), "annual report"),
    (r"\b(investor )?presentation\b", ("presentation",), "investor presentation"),
    (r"", ("transcript", "presentation"), "earnings call"),
)

LATEST_FILING_SQL = """
SELECT company, description, pdf_url, published_at
FROM announcements
WHERE isin = %(isin)s AND category = ANY(%(categories)s)
ORDER BY published_at DESC
LIMIT 1
"""

# When a question names no topic ("summarise the call"), rank passages by the
# things an earnings call is read for.
_SUMMARY_TERMS = frozenset(
    """
    revenue growth margin guidance outlook demand deal deals order book
    headcount attrition pricing capex dividend profit quarter
    """.split()  # noqa: SIM905 -- a word list reads better as prose
)

_STOP = frozenset(
    """
    what did does do the a an of in on for to and or is are was were be
    about say said says tell me summarise summarize summary latest last
    earnings call concall conference transcript annual report investor
    presentation management company its their it this that from with
    """.split()  # noqa: SIM905 -- a word list reads better as prose
)

SYSTEM_PROMPT = """\
You answer a question about a company filing using ONLY the numbered
passages given. Every factual sentence must contain a direct quotation from
a passage in double quotes, copied exactly, followed by the passage number
in square brackets, e.g. Management said "demand remains strong" [2].
If the passages do not answer the question, say exactly: The filing passages
retrieved do not cover this. Do not add figures, opinions or anything not
quoted. At most five sentences.
"""


@dataclass(frozen=True)
class Filing:
    company: str
    description: str
    pdf_url: str
    published_at: datetime


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z][a-z-]{2,}", text.lower()) if w not in _STOP}


def passages(text: str) -> list[str]:
    """Paragraph-sized chunks of the document's text."""
    blocks = [" ".join(b.split()) for b in re.split(r"\n\s*\n", text)]
    chunks: list[str] = []
    current = ""
    for block in blocks:
        if not block:
            continue
        if current and len(current) + len(block) > PASSAGE_CHARS:
            chunks.append(current)
            current = ""
        current = f"{current} {block}".strip()
        while len(current) > PASSAGE_CHARS * 2:
            chunks.append(current[:PASSAGE_CHARS])
            current = current[PASSAGE_CHARS:]
    if current:
        chunks.append(current)
    return chunks


def best_passages(question: str, chunks: list[str]) -> list[str]:
    terms = _words(question) or set(_SUMMARY_TERMS)
    scored = [
        (len(terms & _words(chunk)), -index, chunk)
        for index, chunk in enumerate(chunks)
    ]
    top = sorted((s for s in scored if s[0] > 0), reverse=True)[:PASSAGES_USED]
    # Back into document order so the answer reads in sequence.
    return [chunk for _, _, chunk in sorted(top, key=lambda s: -s[1])]


def _normal(text: str) -> str:
    return " ".join(text.split()).lower()


def quotes_check(answer: str, chosen: list[str]) -> bool:
    """Whether every quotation in the answer appears in a passage."""
    quotes = re.findall(r'"([^"]{8,})"', answer)
    corpus = _normal(" ".join(chosen))
    return bool(quotes) and all(_normal(q) in corpus for q in quotes)


def _latest_filing(isin: str, categories: tuple[str, ...]) -> Filing | None:
    with connect(readonly=True) as conn:
        row = conn.execute(
            LATEST_FILING_SQL, {"isin": isin, "categories": list(categories)}
        ).fetchone()
    if row is None:
        return None
    return Filing(
        str(row["company"]),
        str(row["description"]),
        str(row["pdf_url"]),
        row["published_at"],
    )


def pdf_text(url: str) -> str:
    """The document's text; raises ValueError for anything unreadable."""
    if not url.startswith("https://"):
        raise ValueError("only https filings are fetched")
    started = time.monotonic()
    try:
        with httpx.stream(
            "GET",
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=FETCH_TIMEOUT_SECONDS,
            follow_redirects=True,
        ) as response:
            response.raise_for_status()
            body = bytearray()
            for piece in response.iter_bytes():
                body += piece
                if len(body) > MAX_PDF_BYTES:
                    raise ValueError("the filing is larger than 15 MB")
                if time.monotonic() - started > DOWNLOAD_DEADLINE_SECONDS:
                    raise ValueError("the filing took too long to download")
    except httpx.HTTPError as exc:
        raise ValueError(f"could not download the filing: {exc}") from exc
    try:
        reader = PdfReader(io.BytesIO(bytes(body)))
        texts: list[str] = []
        total = 0
        started = time.monotonic()
        for page in reader.pages[:MAX_PAGES]:
            text = page.extract_text() or ""
            texts.append(text[: MAX_TEXT_CHARS - total])
            total += len(texts[-1])
            if total >= MAX_TEXT_CHARS:
                break
            if time.monotonic() - started > EXTRACT_DEADLINE_SECONDS:
                break
        return "\n\n".join(texts)
    except (PdfReadError, ValueError, KeyError, TypeError) as exc:
        raise ValueError(f"could not read the PDF: {exc}") from exc


def _quoted_answer(question: str, chosen: list[str]) -> str | None:
    numbered = "\n\n".join(f"[{i}] {p}" for i, p in enumerate(chosen, start=1))
    try:
        payload = chat(
            ollama_client(),
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": f"Passages:\n{numbered}\n\nQuestion: {question}",
                },
            ],
            [],
            think=False,
        )
    except OllamaError:
        return None
    text = str(payload.get("message", {}).get("content") or "").strip()
    return text if text and quotes_check(text, chosen) else None


def answer(question: str) -> PathResult:
    found = resolve_companies(question)
    if len(found.companies) != 1:
        return PathResult(
            text='Name one company, e.g. "summarise TCS\'s latest earnings call".'
        )
    company = found.companies[0]
    isin = company.instrument_key.split("|", 1)[1]
    _, categories, kind = next(
        k for k in _KINDS if re.search(k[0], question, re.IGNORECASE)
    )
    try:
        filing = _latest_filing(isin, categories)
    except psycopg.errors.UndefinedTable:
        filing = None
    if filing is None:
        return PathResult(
            text=f"{company.ticker}: no stored {kind} filing. BSE's feed only covers "
            "the current day, so filings appear here once `sra ingest "
            "announcements` has run on the day they are published."
        )
    when = filing.published_at.astimezone(IST).strftime("%d %b %Y")
    title = f"{company.name} ({company.ticker}): {kind}"
    source = f"Source: {filing.description[:120]} (filed {when}) {filing.pdf_url}"
    try:
        chosen = best_passages(question, passages(pdf_text(filing.pdf_url)))
    except ValueError as exc:
        return PathResult(text=f"{title}\n{exc}.\n\n{source}")
    if not chosen:
        return PathResult(
            text=f"{title}\nThe filing's text does not mention this "
            f"(or it is a scanned image with no text).\n\n{source}"
        )
    quoted = _quoted_answer(question, chosen)
    if quoted is not None:
        body = "Answer, quoted from the filing\n" + quoted
    else:
        body = "Relevant passages from the filing\n" + "\n".join(
            f'  - "{p[:500]}{"..." if len(p) > 500 else ""}"' for p in chosen[:3]
        )
    return PathResult(text="\n\n".join([title, body, source]))
