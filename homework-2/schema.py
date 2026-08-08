"""
Lakebase schema and document writes for the weather RAG pipeline.

`init_db()` runs at app startup and is idempotent: it enables pgvector, creates
`weather_documents` (raw harvested text) and `weather_embeddings` (chunks +
vectors), and builds the HNSW cosine index used by /weather/search. No seed
data - documents arrive through POST /weather/sync.

`upsert_documents()` lives here too, so the Flask route and the batch job in
notebooks/ write harvested documents through exactly the same statement.

The same DDL is mirrored in sql/ so it can be run by hand in the Lakebase SQL
editor before running the notebook job.
"""

import logging

from psycopg2.extras import Json, execute_values

import lakebase
from embeddings import EMBEDDING_DIM

logger = logging.getLogger(__name__)

SOURCE_TYPES = ["alert", "forecast"]

DDL = [
    "CREATE EXTENSION IF NOT EXISTS vector",
    """
    CREATE TABLE IF NOT EXISTS weather_documents (
        id             TEXT PRIMARY KEY,
        location       TEXT NOT NULL,
        source_type    TEXT NOT NULL CHECK (source_type IN ('alert', 'forecast')),
        headline       TEXT,
        event          TEXT,
        narrative_text TEXT NOT NULL,
        issued_at      TIMESTAMPTZ,
        effective_at   TIMESTAMPTZ,
        payload        JSONB NOT NULL,
        synced_at      TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_weather_documents_location ON weather_documents (location)",
    "CREATE INDEX IF NOT EXISTS ix_weather_documents_source_type ON weather_documents (source_type)",
    f"""
    CREATE TABLE IF NOT EXISTS weather_embeddings (
        id          TEXT PRIMARY KEY,
        document_id TEXT NOT NULL REFERENCES weather_documents(id) ON DELETE CASCADE,
        chunk_index INT NOT NULL,
        chunk_text  TEXT NOT NULL,
        embedding   VECTOR({EMBEDDING_DIM}) NOT NULL,
        model_name  TEXT NOT NULL,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (document_id, chunk_index)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_weather_embeddings_vector
    ON weather_embeddings USING hnsw (embedding vector_cosine_ops)
    """,
    "CREATE INDEX IF NOT EXISTS ix_weather_embeddings_document_id ON weather_embeddings (document_id)",
]


def init_db() -> None:
    """Create the extension, tables and indexes if they are missing."""
    lakebase.execute_script([(sql, None) for sql in DDL])
    logger.info("Lakebase weather schema ready (vector dim %s)", EMBEDDING_DIM)


UPSERT_DOCUMENT_SQL = """
    INSERT INTO weather_documents (
        id, location, source_type, headline, event, narrative_text,
        issued_at, effective_at, payload, synced_at
    ) VALUES %s
    ON CONFLICT (id) DO UPDATE SET
        location       = EXCLUDED.location,
        headline       = EXCLUDED.headline,
        event          = EXCLUDED.event,
        narrative_text = EXCLUDED.narrative_text,
        issued_at      = EXCLUDED.issued_at,
        effective_at   = EXCLUDED.effective_at,
        payload        = EXCLUDED.payload,
        -- Only bump synced_at when the text actually changed, so re-running the
        -- sync does not force every document to be re-embedded.
        synced_at      = CASE
            WHEN weather_documents.narrative_text IS DISTINCT FROM EXCLUDED.narrative_text
            THEN now() ELSE weather_documents.synced_at
        END
"""

UPSERT_DOCUMENT_TEMPLATE = (
    "(%s, %s, %s, %s, %s, %s, %s::timestamptz, %s::timestamptz, %s, now())"
)


def upsert_documents(documents: list[dict]) -> int:
    """Insert or refresh weather_documents rows; returns the number written."""
    if not documents:
        return 0

    values = [
        (
            doc["id"],
            doc["location"],
            doc["source_type"],
            doc["headline"],
            doc["event"],
            doc["narrative_text"],
            doc["issued_at"],
            doc["effective_at"],
            Json(doc["payload"]),
        )
        for doc in documents
    ]
    with lakebase.get_connection() as conn:
        with conn.cursor() as cur:
            execute_values(
                cur,
                UPSERT_DOCUMENT_SQL,
                values,
                template=UPSERT_DOCUMENT_TEMPLATE,
                page_size=100,
            )
        conn.commit()
    return len(values)
