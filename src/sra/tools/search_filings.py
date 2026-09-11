from dataclasses import dataclass, field

import psycopg
from psycopg.rows import dict_row

from sra.config import settings
from sra.narrative.embeddings import embed_query

DEFAULT_K = 6
MAX_K = 20

# Common abbreviations the model reaches for that do not appear verbatim in
# the canonical section labels (see narrative/items.py), so a substring filter
# on them silently matches zero rows. Observed: "MD&A" against
# "Management's Discussion and Analysis" filtered out every passage even
# though the ticker and topic were both correct.
_SECTION_ALIASES: dict[str, str] = {
    "md&a": "discussion and analysis",
    "mdna": "discussion and analysis",
}


def _normalize_section(section: str) -> str:
    key = section.strip().lower()
    return _SECTION_ALIASES.get(key, section)


SEARCH = """
SELECT c.ticker,
       f.form_type,
       f.period_of_report,
       ch.accession_no,
       ch.section,
       ch.ordinal,
       ch.text,
       1 - (ch.embedding <=> %(query)s::vector) AS similarity
FROM chunks ch
JOIN filings f ON f.accession_no = ch.accession_no
JOIN companies c ON c.cik = f.cik
WHERE ch.embedding IS NOT NULL
  AND (%(ticker)s::text IS NULL OR c.ticker = upper(%(ticker)s))
  AND (%(form_type)s::text IS NULL OR f.form_type = upper(%(form_type)s))
  AND (%(section)s::text IS NULL OR ch.section ILIKE '%%' || %(section)s || '%%')
ORDER BY ch.embedding <=> %(query)s::vector
LIMIT %(k)s
"""


@dataclass(frozen=True)
class Passage:
    ticker: str
    form_type: str
    period_of_report: str
    accession_no: str
    section: str
    ordinal: int
    text: str
    similarity: float


@dataclass(frozen=True)
class SearchResult:
    query: str
    passages: list[Passage] = field(default_factory=list)
    error: str | None = None

    def to_text(self) -> str:
        """Render for the model, with the citation attached to each passage so a
        quote cannot be separated from where it came from."""
        if self.error:
            return f"ERROR: {self.error}"
        if not self.passages:
            return "0 passages"
        blocks = [
            f"[{i}] {p.ticker} {p.form_type} filed for period {p.period_of_report}"
            f" | {p.section} | accession {p.accession_no}"
            f" | similarity {p.similarity:.3f}\n{p.text}"
            for i, p in enumerate(self.passages, start=1)
        ]
        return "\n\n".join(blocks)


def search_filings(
    query: str,
    *,
    ticker: str | None = None,
    form_type: str | None = None,
    section: str | None = None,
    k: int = DEFAULT_K,
) -> SearchResult:
    """Nearest-neighbour search over filing text, filtered by filer and section."""
    if not query.strip():
        return SearchResult(query=query, error="empty query")
    section = _normalize_section(section) if section else section
    vector = embed_query(query)
    literal = "[" + ",".join(repr(x) for x in vector) + "]"
    try:
        with psycopg.connect(settings().readonly_dsn, row_factory=dict_row) as conn:
            conn.read_only = True
            rows = conn.execute(
                SEARCH,
                {
                    "query": literal,
                    "ticker": ticker,
                    "form_type": form_type,
                    "section": section,
                    "k": max(1, min(k, MAX_K)),
                },
            ).fetchall()
    except psycopg.Error as exc:
        return SearchResult(query=query, error=str(exc).strip())

    return SearchResult(
        query=query,
        passages=[
            Passage(
                ticker=str(row["ticker"]),
                form_type=str(row["form_type"]),
                period_of_report=str(row["period_of_report"]),
                accession_no=str(row["accession_no"]),
                section=str(row["section"]),
                ordinal=int(row["ordinal"]),
                text=str(row["text"]),
                similarity=float(row["similarity"]),
            )
            for row in rows
        ],
    )
