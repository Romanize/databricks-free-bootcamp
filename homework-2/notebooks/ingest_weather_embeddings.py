# Databricks notebook source
# MAGIC %md
# MAGIC # Ingest Weather Documents -> Vector Embeddings (Lakebase)
# MAGIC
# MAGIC Day 2 homework batch job. It:
# MAGIC
# MAGIC 1. Harvests unstructured weather text (NWS active alerts + narrative
# MAGIC    forecasts) for a list of locations into `weather_documents`.
# MAGIC 2. Reads the documents that have no embeddings yet (or were re-synced
# MAGIC    since they were last embedded).
# MAGIC 3. Chunks the narrative text (800 chars / 100 overlap) and embeds each
# MAGIC    chunk with `sentence-transformers/all-MiniLM-L6-v2` (384-dim).
# MAGIC 4. Upserts the vectors into `weather_embeddings` via psycopg2 +
# MAGIC    `execute_values`, casting each vector with `%s::vector`.
# MAGIC
# MAGIC Everything runs in plain Python - **no `spark.write.jdbc`**, which is not
# MAGIC supported against this Lakebase instance. It reuses the same Lakebase
# MAGIC secret (`database` / `lakebase-url`) as the Flask app, so no new secrets
# MAGIC are needed.
# MAGIC
# MAGIC Runs unchanged as a Databricks notebook or as a local script:
# MAGIC
# MAGIC ```
# MAGIC python notebooks/ingest_weather_embeddings.py --locations "Chicago, IL" "Austin, TX"
# MAGIC ```

# COMMAND ----------

# DBTITLE 1,Install dependencies (notebook only)
# MAGIC %pip install -q 'databricks-sdk>=0.30.0' psycopg2-binary sentence-transformers requests

# COMMAND ----------

# MAGIC %md
# MAGIC ## Config
# MAGIC
# MAGIC In a notebook these come from widgets; as a script they come from CLI
# MAGIC flags. Both land in the same variables.

# COMMAND ----------

import argparse
import logging
import os
import sys

# The pipeline modules (lakebase.py, weather_client.py, embeddings.py) live in
# the parent folder, next to app.py. __file__ is undefined in a notebook, so
# fall back to the working directory and add both candidates to the path.
try:
    HERE = os.path.dirname(os.path.abspath(__file__))
except NameError:  # Databricks notebook
    HERE = os.getcwd()

for candidate in (os.path.dirname(HERE), HERE):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

DEFAULT_LOCATIONS = ["Chicago, IL", "Austin, TX", "Miami, FL", "Denver, CO"]

try:
    dbutils  # noqa: F821  - defined only inside a Databricks notebook
    dbutils.widgets.text("locations", ",".join(DEFAULT_LOCATIONS), "Locations (comma separated)")
    dbutils.widgets.text("sync_limit", "50", "Max documents per location")
    dbutils.widgets.text("embed_limit", "1000", "Max documents to embed per run")
    dbutils.widgets.dropdown("skip_sync", "false", ["true", "false"], "Skip harvest, embed only")

    LOCATIONS = [p.strip() for p in dbutils.widgets.get("locations").split(",") if p.strip()]
    SYNC_LIMIT = int(dbutils.widgets.get("sync_limit"))
    EMBED_LIMIT = int(dbutils.widgets.get("embed_limit"))
    SKIP_SYNC = dbutils.widgets.get("skip_sync") == "true"
except NameError:
    parser = argparse.ArgumentParser(description="Harvest + embed NWS weather text into Lakebase")
    parser.add_argument("--locations", nargs="+", default=DEFAULT_LOCATIONS)
    parser.add_argument("--sync-limit", type=int, default=50)
    parser.add_argument("--embed-limit", type=int, default=1000)
    parser.add_argument("--skip-sync", action="store_true", help="Embed existing documents only")
    args = parser.parse_args()

    LOCATIONS = args.locations
    SYNC_LIMIT = args.sync_limit
    EMBED_LIMIT = args.embed_limit
    SKIP_SYNC = args.skip_sync

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Make sure the schema exists
# MAGIC
# MAGIC `schema.init_db()` is the same DDL the Flask app runs at startup: the
# MAGIC `vector` extension, `weather_documents`, `weather_embeddings` with a
# MAGIC `VECTOR(384)` column, and the HNSW cosine index. It is idempotent, so it
# MAGIC is safe to call here too (the SQL is also in `sql/` to run by hand).

# COMMAND ----------

import embeddings
import lakebase
import schema
from schema import upsert_documents
from weather_client import WeatherClient

schema.init_db()
print(f"Schema ready. Model {embeddings.MODEL_NAME} -> {embeddings.EMBEDDING_DIM}-dim vectors")
print(f"Chunking: size={embeddings.CHUNK_SIZE} overlap={embeddings.CHUNK_OVERLAP}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. Harvest unstructured weather text
# MAGIC
# MAGIC Same client and same upsert the Flask `POST /weather/sync` route uses, so
# MAGIC the job and the app can be run in any order without creating duplicates -
# MAGIC `weather_documents.id` is the dedup key.

# COMMAND ----------

if SKIP_SYNC:
    print("skip_sync=true - embedding existing documents only")
else:
    client = WeatherClient()
    harvested = []
    for location in LOCATIONS:
        try:
            documents = client.fetch_documents(location, limit=SYNC_LIMIT)
        except Exception as err:
            print(f"  {location}: FAILED ({err})")
            continue
        harvested.extend(documents)
        alerts = sum(1 for d in documents if d["source_type"] == "alert")
        print(f"  {location}: {len(documents)} documents ({alerts} alerts)")

    unique = list({doc["id"]: doc for doc in harvested}.values())
    print(f"Upserting {len(unique)} unique documents into weather_documents...")
    print(f"Wrote {upsert_documents(unique)} rows")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. Chunk + embed + write vectors
# MAGIC
# MAGIC `embeddings.embed_pending()` selects the documents whose newest embedding
# MAGIC is older than their `synced_at` (or that have none at all), chunks the
# MAGIC narrative text, embeds the chunks in batches, and upserts them with
# MAGIC `execute_values` using the `(%s, %s, %s, %s, %s::vector, %s, now())`
# MAGIC template. Chunks left behind by a now-shorter document are deleted.

# COMMAND ----------

pending = embeddings.fetch_pending(EMBED_LIMIT)
print(f"{len(pending)} documents pending embedding")

result = embeddings.embed_pending(limit=EMBED_LIMIT)
print(f"Embedded {result['chunks']} chunks across {result['documents']} documents")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3. Verify

# COMMAND ----------

summary = lakebase.query_one(
    """
    SELECT (SELECT COUNT(*) FROM weather_documents)  AS documents,
           (SELECT COUNT(*) FROM weather_embeddings) AS chunks,
           (SELECT COUNT(DISTINCT location) FROM weather_documents) AS locations
    """
)
print(summary)

for row in lakebase.query(
    """
    SELECT source_type, COUNT(*) AS documents, AVG(length(narrative_text))::int AS avg_chars
    FROM weather_documents GROUP BY source_type ORDER BY source_type
    """
):
    print(row)

# A sanity-check retrieval against the vectors that were just written.
sample_query = "flash flood risk this weekend"
rows = lakebase.query(
    """
    SELECT d.location, d.source_type, d.headline,
           1 - (e.embedding <=> %s::vector) AS similarity
    FROM weather_embeddings e
    JOIN weather_documents d ON d.id = e.document_id
    ORDER BY e.embedding <=> %s::vector
    LIMIT 5
    """,
    (embeddings.embed_query(sample_query),) * 2,
)
print(f"\nTop matches for {sample_query!r}:")
for row in rows:
    print(f"  {row['similarity']:.3f}  [{row['source_type']}] {row['location']} - {row['headline']}")
