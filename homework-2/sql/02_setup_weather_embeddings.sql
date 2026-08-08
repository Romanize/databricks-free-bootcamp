-- Setup script for the weather_embeddings table (chunk vectors).
-- Dimension 384 matches sentence-transformers/all-MiniLM-L6-v2, the same model
-- the reference news pipeline uses. Change both the model and this number
-- together if you swap models (see MODEL_DIMENSIONS in embeddings.py).

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS weather_embeddings (
    id          TEXT PRIMARY KEY,             -- "{document_id}#{chunk_index}"
    document_id TEXT NOT NULL REFERENCES weather_documents(id) ON DELETE CASCADE,
    chunk_index INT NOT NULL,
    chunk_text  TEXT NOT NULL,
    embedding   VECTOR(384) NOT NULL,
    model_name  TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (document_id, chunk_index)
);

-- HNSW index for fast cosine similarity search with the <=> operator.
CREATE INDEX IF NOT EXISTS ix_weather_embeddings_vector
ON weather_embeddings USING hnsw (embedding vector_cosine_ops);

CREATE INDEX IF NOT EXISTS ix_weather_embeddings_document_id
ON weather_embeddings (document_id);

-- Verify
SELECT column_name, data_type, udt_name
FROM information_schema.columns
WHERE table_name = 'weather_embeddings'
ORDER BY ordinal_position;
