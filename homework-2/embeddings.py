"""
Chunking + embedding for the weather documents.

Shared by the Flask app (which embeds the incoming search query) and by
notebooks/ingest_weather_embeddings.py (which embeds the harvested documents),
so both sides always use the same model, the same dimensionality and the same
chunking rules.

The model is loaded once per process behind an lru_cache, never per request.
Writes go through psycopg2 with the vector passed as a literal and cast with
%s::vector - no Spark JDBC anywhere in this pipeline.
"""

import logging
import os
from functools import lru_cache

from psycopg2.extras import execute_values

import lakebase

logger = logging.getLogger(__name__)

MODEL_NAME = os.environ.get("EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2")

# The pgvector column type VECTOR(N) must match the model exactly, so the
# dimension is derived from the model name rather than hardcoded in the DDL.
MODEL_DIMENSIONS = {
    "sentence-transformers/all-MiniLM-L6-v2": 384,
    "sentence-transformers/all-MiniLM-L12-v2": 384,
    "sentence-transformers/all-mpnet-base-v2": 768,
    "BAAI/bge-small-en-v1.5": 384,
    "BAAI/bge-base-en-v1.5": 768,
}

if MODEL_NAME not in MODEL_DIMENSIONS:
    raise ValueError(
        f"Unknown embedding model {MODEL_NAME!r} - add its output dimension to "
        "MODEL_DIMENSIONS before using it, and recreate weather_embeddings."
    )

EMBEDDING_DIM = MODEL_DIMENSIONS[MODEL_NAME]

# NWS text is short: a forecast period is ~200 chars and most alerts fit in one
# chunk. The window only matters for long alert description + instruction
# bodies, so the lab's 800/100 defaults are kept unchanged.
CHUNK_SIZE = int(os.environ.get("CHUNK_SIZE", 800))
CHUNK_OVERLAP = int(os.environ.get("CHUNK_OVERLAP", 100))

# Where sentence-transformers caches model weights. /tmp is writable both in
# Databricks Apps and in a notebook.
CACHE_FOLDER = os.environ.get("SENTENCE_TRANSFORMERS_HOME", "/tmp/.cache/huggingface")


@lru_cache(maxsize=1)
def get_model():
    """Load the sentence-transformers model once per process."""
    from sentence_transformers import SentenceTransformer

    logger.info("Loading embedding model %s", MODEL_NAME)
    return SentenceTransformer(MODEL_NAME, cache_folder=CACHE_FOLDER)


def chunk_text(text: str) -> list[str]:
    """Split text into overlapping windows of CHUNK_SIZE characters."""
    text = (text or "").strip()
    if not text:
        return []

    chunks = []
    step = CHUNK_SIZE - CHUNK_OVERLAP
    for start in range(0, len(text), step):
        chunk = text[start : start + CHUNK_SIZE].strip()
        if chunk:
            chunks.append(chunk)
        if start + CHUNK_SIZE >= len(text):
            break
    return chunks


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed a batch of strings, returning plain Python lists of floats."""
    if not texts:
        return []
    vectors = get_model().encode(texts, batch_size=32, show_progress_bar=False)
    return [[float(x) for x in vector] for vector in vectors]


def to_vector_literal(vector: list[float]) -> str:
    """Format a vector the way pgvector parses it: '[0.1,0.2,...]'."""
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"


def embed_query(query: str) -> str:
    """Embed a search query and return it ready to bind as %s::vector."""
    return to_vector_literal(embed_texts([query])[0])


# ------------------------------------------------------------------ ingestion

PENDING_SQL = """
    SELECT d.id, d.narrative_text
    FROM weather_documents d
    LEFT JOIN LATERAL (
        SELECT max(e.created_at) AS embedded_at
        FROM weather_embeddings e
        WHERE e.document_id = d.id
    ) e ON TRUE
    WHERE e.embedded_at IS NULL OR e.embedded_at < d.synced_at
    ORDER BY d.synced_at
"""

INSERT_SQL = """
    INSERT INTO weather_embeddings (
        id, document_id, chunk_index, chunk_text, embedding, model_name, created_at
    ) VALUES %s
    ON CONFLICT (id) DO UPDATE SET
        chunk_text = EXCLUDED.chunk_text,
        embedding  = EXCLUDED.embedding,
        model_name = EXCLUDED.model_name,
        created_at = now()
"""

INSERT_TEMPLATE = "(%s, %s, %s, %s, %s::vector, %s, now())"


def fetch_pending(limit: int | None = None) -> list[dict]:
    """Documents that have never been embedded, or were re-synced since."""
    sql = PENDING_SQL + (" LIMIT %s" if limit else "")
    return lakebase.query(sql, (limit,) if limit else None)


def embed_pending(limit: int | None = None, batch_size: int = 64) -> dict:
    """
    Embed every pending document and upsert its chunks into weather_embeddings.

    Returns counts so the caller (Flask route or notebook) can report progress.
    """
    documents = fetch_pending(limit)
    if not documents:
        return {"documents": 0, "chunks": 0, "model": MODEL_NAME}

    # Flatten to (document_id, chunk_index, chunk_text) before embedding so the
    # model sees one large batch instead of one call per document.
    rows: list[tuple[str, int, str]] = []
    chunk_counts: dict[str, int] = {}
    for doc in documents:
        chunks = chunk_text(doc["narrative_text"])
        chunk_counts[doc["id"]] = len(chunks)
        rows.extend((doc["id"], index, chunk) for index, chunk in enumerate(chunks))

    written = 0
    with lakebase.get_connection() as conn:
        with conn.cursor() as cur:
            for start in range(0, len(rows), batch_size):
                batch = rows[start : start + batch_size]
                vectors = embed_texts([chunk for _, _, chunk in batch])
                values = [
                    (
                        f"{document_id}#{index}",
                        document_id,
                        index,
                        chunk,
                        to_vector_literal(vector),
                        MODEL_NAME,
                    )
                    for (document_id, index, chunk), vector in zip(batch, vectors)
                ]
                execute_values(cur, INSERT_SQL, values, template=INSERT_TEMPLATE, page_size=100)
                written += len(values)

            # A re-synced document can be shorter than it was; drop chunks left
            # over from the previous, longer version.
            for document_id, count in chunk_counts.items():
                cur.execute(
                    "DELETE FROM weather_embeddings WHERE document_id = %s AND chunk_index >= %s",
                    (document_id, count),
                )
        conn.commit()

    logger.info("Embedded %s chunks across %s documents", written, len(documents))
    return {"documents": len(documents), "chunks": written, "model": MODEL_NAME}
