"""
Chunking + embedding for ticker news articles.

Shared by the MCP server (which embeds the incoming search query), the Flask app
(which can trigger an embed run from the UI) and notebooks/ingest_news_embeddings.py
(the every-2-hours job), so all three always use the same model, the same
dimensionality and the same chunking rules.

Carried over from homework 2 with two changes:

  * the source table is `ticker_news` rather than `weather_documents`, and each
    embedding row copies the article's `tickers` array so a vector search can be
    filtered to one symbol without joining back;
  * `embed_pending()` returns the per-symbol counts the job logs.

The model is loaded once per process behind an lru_cache, never per request.
Writes go through psycopg2 with the vector passed as a literal and cast with
%s::vector - no Spark JDBC anywhere in this pipeline.
"""

import logging
import os
from functools import lru_cache

from psycopg2.extras import execute_values

from lakebase import app_db

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
        "MODEL_DIMENSIONS before using it, and recreate tickers_news_embeddings."
    )

EMBEDDING_DIM = MODEL_DIMENSIONS[MODEL_NAME]

# News articles are longer than the NWS text homework 2 handled: a Massive
# article's title + description runs 300-1500 characters, so chunking actually
# does work here. 800/100 is kept anyway - it splits a long article into 2-3
# overlapping windows, which is the granularity a "why is AAPL down?" question
# wants back.
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

# An article is pending when it has no chunks yet, or when its text changed
# since the last time it was embedded (upsert_articles only bumps synced_at on
# a real text change, so a re-run of the news job re-embeds nothing).
PENDING_SQL = """
    SELECT n.id, n.tickers, n.embed_text
    FROM ticker_news n
    LEFT JOIN LATERAL (
        SELECT max(e.created_at) AS embedded_at
        FROM tickers_news_embeddings e
        WHERE e.article_id = n.id
    ) e ON TRUE
    WHERE e.embedded_at IS NULL OR e.embedded_at < n.synced_at
    ORDER BY n.published_utc DESC NULLS LAST
"""

INSERT_SQL = """
    INSERT INTO tickers_news_embeddings (
        id, article_id, tickers, chunk_index, chunk_text, embedding,
        model_name, created_at
    ) VALUES %s
    ON CONFLICT (id) DO UPDATE SET
        tickers    = EXCLUDED.tickers,
        chunk_text = EXCLUDED.chunk_text,
        embedding  = EXCLUDED.embedding,
        model_name = EXCLUDED.model_name,
        created_at = now()
"""

INSERT_TEMPLATE = "(%s, %s, %s, %s, %s, %s::vector, %s, now())"


def fetch_pending(limit: int | None = None) -> list[dict]:
    """Articles that have never been embedded, or were re-synced since."""
    sql = PENDING_SQL + (" LIMIT %s" if limit else "")
    return app_db.query(sql, (limit,) if limit else None)


def count_pending() -> int:
    """How many articles are waiting to be embedded, for the app's status bar."""
    row = app_db.query_one(f"SELECT COUNT(*) AS pending FROM ({PENDING_SQL}) p")
    return row["pending"] if row else 0


def embed_pending(limit: int | None = None, batch_size: int = 64) -> dict:
    """
    Embed every pending article and upsert its chunks into tickers_news_embeddings.

    Returns counts so the caller (Flask route or notebook) can report progress.
    """
    articles = fetch_pending(limit)
    if not articles:
        return {"articles": 0, "chunks": 0, "model": MODEL_NAME, "symbols": {}}

    # Flatten to (article_id, tickers, chunk_index, chunk_text) before embedding
    # so the model sees one large batch instead of one call per article.
    rows: list[tuple[str, list, int, str]] = []
    chunk_counts: dict[str, int] = {}
    symbols: dict[str, int] = {}
    for article in articles:
        chunks = chunk_text(article["embed_text"])
        chunk_counts[article["id"]] = len(chunks)
        tickers = article["tickers"] or []
        for symbol in tickers:
            symbols[symbol] = symbols.get(symbol, 0) + len(chunks)
        rows.extend(
            (article["id"], tickers, index, chunk) for index, chunk in enumerate(chunks)
        )

    written = 0
    with app_db.get_connection() as conn:
        with conn.cursor() as cur:
            for start in range(0, len(rows), batch_size):
                batch = rows[start : start + batch_size]
                vectors = embed_texts([chunk for _, _, _, chunk in batch])
                values = [
                    (
                        f"{article_id}#{index}",
                        article_id,
                        tickers,
                        index,
                        chunk,
                        to_vector_literal(vector),
                        MODEL_NAME,
                    )
                    for (article_id, tickers, index, chunk), vector in zip(batch, vectors)
                ]
                execute_values(cur, INSERT_SQL, values, template=INSERT_TEMPLATE, page_size=100)
                written += len(values)

            # A re-synced article can be shorter than it was; drop chunks left
            # over from the previous, longer version.
            for article_id, count in chunk_counts.items():
                cur.execute(
                    "DELETE FROM tickers_news_embeddings "
                    "WHERE article_id = %s AND chunk_index >= %s",
                    (article_id, count),
                )
        conn.commit()

    logger.info("Embedded %s chunks across %s articles", written, len(articles))
    return {
        "articles": len(articles),
        "chunks": written,
        "model": MODEL_NAME,
        "symbols": symbols,
    }
