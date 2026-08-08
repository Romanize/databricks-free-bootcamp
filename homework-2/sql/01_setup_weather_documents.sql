-- Setup script for the weather_documents table (raw harvested NWS text).
-- The Flask app runs this same DDL at startup via schema.py; this file exists
-- so the tables can also be created by hand in the Lakebase SQL editor before
-- running notebooks/ingest_weather_embeddings.py.

CREATE TABLE IF NOT EXISTS weather_documents (
    id             TEXT PRIMARY KEY,          -- NWS alert id, or forecast:{grid}:{startTime}
    location       TEXT NOT NULL,             -- resolved "City, ST" the document was synced for
    source_type    TEXT NOT NULL CHECK (source_type IN ('alert', 'forecast')),
    headline       TEXT,
    event          TEXT,                      -- e.g. "Flash Flood Warning", "Mostly Sunny"
    narrative_text TEXT NOT NULL,             -- the free text that gets embedded
    issued_at      TIMESTAMPTZ,               -- alert "sent" / forecast "generatedAt"
    effective_at   TIMESTAMPTZ,               -- alert "effective" / forecast period start
    payload        JSONB NOT NULL,            -- raw API object, for provenance
    synced_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_weather_documents_location
ON weather_documents (location);

CREATE INDEX IF NOT EXISTS ix_weather_documents_source_type
ON weather_documents (source_type);

-- Verify
SELECT column_name, data_type
FROM information_schema.columns
WHERE table_name = 'weather_documents'
ORDER BY ordinal_position;
