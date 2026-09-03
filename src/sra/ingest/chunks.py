from dataclasses import dataclass

import psycopg

from sra.narrative.chunking import MIN_CHARS, chunk_text
from sra.narrative.embeddings import embed_documents
from sra.narrative.items import section_label
from sra.narrative.sections import split_sections
from sra.sec import endpoints
from sra.sec.client import SecClient

EMBED_BATCH = 32

INSERT = """
INSERT INTO chunks (accession_no, section, ordinal, text, embedding)
VALUES (%(accession_no)s, %(section)s, %(ordinal)s, %(text)s,
        %(embedding)s::vector)
ON CONFLICT (accession_no, section, ordinal) DO UPDATE
SET text = EXCLUDED.text, embedding = EXCLUDED.embedding
"""


@dataclass(frozen=True)
class ChunkIngestReport:
    accession_no: str
    ticker: str
    form_type: str
    sections: int
    chunks: int


def _as_vector_literal(vector: list[float]) -> str:
    """pgvector's text input format. Passing a literal avoids taking on the
    pgvector Python package for one cast."""
    return "[" + ",".join(repr(x) for x in vector) + "]"


def index_filing(
    conn: psycopg.Connection[dict[str, object]],
    client: SecClient,
    *,
    cik: str,
    ticker: str,
    accession_no: str,
    form_type: str,
    primary_document: str,
) -> ChunkIngestReport:
    """Parse, chunk, embed and store one filing's narrative sections."""
    url = endpoints.filing_document(cik, accession_no, primary_document)
    # Filings are immutable once published, so this is cached permanently.
    document = client.get_text(url, immutable=True).encode("utf-8")

    rows: list[dict[str, object]] = []
    sections = 0
    for section in split_sections(document):
        if len(section.text) < MIN_CHARS:
            continue
        label = section_label(form_type, section.part, section.item, section.title)
        chunks = chunk_text(section.text)
        if not chunks:
            continue
        sections += 1
        for chunk in chunks:
            rows.append(
                {
                    "accession_no": accession_no,
                    "section": label,
                    "ordinal": chunk.ordinal,
                    "text": chunk.text,
                }
            )

    with conn.cursor() as cur:
        # Re-indexing with different chunk sizes would otherwise leave the old
        # chunks behind, since the natural key includes the ordinal.
        cur.execute("DELETE FROM chunks WHERE accession_no = %s", (accession_no,))
        for start in range(0, len(rows), EMBED_BATCH):
            batch = rows[start : start + EMBED_BATCH]
            vectors = embed_documents([str(row["text"]) for row in batch])
            for row, vector in zip(batch, vectors, strict=True):
                cur.execute(INSERT, {**row, "embedding": _as_vector_literal(vector)})

    return ChunkIngestReport(
        accession_no=accession_no,
        ticker=ticker,
        form_type=form_type,
        sections=sections,
        chunks=len(rows),
    )
