import httpx

from sra.config import settings

# nomic-embed-text is trained with task prefixes and degrades noticeably
# without them: documents and queries are embedded into the same space only if
# each is labelled with its role.
DOCUMENT_PREFIX = "search_document: "
QUERY_PREFIX = "search_query: "

EMBEDDING_DIMENSIONS = 768


class EmbeddingError(RuntimeError):
    pass


def _embed(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    cfg = settings()
    try:
        with httpx.Client(
            base_url=cfg.ollama_url, timeout=httpx.Timeout(600.0)
        ) as client:
            response = client.post(
                "/api/embed", json={"model": cfg.embedding_model, "input": texts}
            )
    except httpx.HTTPError as exc:
        raise EmbeddingError(
            f"cannot reach the embedding model at {cfg.ollama_url}. "
            "Start it with `ollama serve`."
        ) from exc
    if response.status_code == 404:
        raise EmbeddingError(
            f"model {cfg.embedding_model!r} is not installed. "
            f"Pull it with `ollama pull {cfg.embedding_model}`."
        )
    if response.status_code != 200:
        raise EmbeddingError(f"{response.status_code}: {response.text[:300]}")
    payload = response.json()
    vectors = payload.get("embeddings") if isinstance(payload, dict) else None
    if not isinstance(vectors, list) or len(vectors) != len(texts):
        raise EmbeddingError(
            f"expected {len(texts)} embeddings, got {type(vectors).__name__}"
        )
    for vector in vectors:
        if not isinstance(vector, list) or len(vector) != EMBEDDING_DIMENSIONS:
            raise EmbeddingError(
                f"expected {EMBEDDING_DIMENSIONS} dimensions to match the chunks "
                f"table, got {len(vector) if isinstance(vector, list) else '?'}"
            )
    return [[float(x) for x in vector] for vector in vectors]


def embed_documents(texts: list[str]) -> list[list[float]]:
    return _embed([f"{DOCUMENT_PREFIX}{text}" for text in texts])


def embed_query(text: str) -> list[float]:
    return _embed([f"{QUERY_PREFIX}{text}"])[0]
