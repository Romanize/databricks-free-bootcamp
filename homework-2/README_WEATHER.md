# Homework 2 - Weather Intelligence: Unstructured Data → Lakebase Vector Search → REST API

An end-to-end RAG pipeline over free-text weather data:

```
api.weather.gov  ──►  weather_documents  ──►  chunk + embed  ──►  weather_embeddings
   (harvest)            (Lakebase)            (MiniLM, 384d)      (pgvector, HNSW)
                                                                        │
                                          POST /weather/search  ◄───────┘
```

## Data source: National Weather Service (api.weather.gov)

I picked the NWS API for the reasons the assignment suggests, and one more that
mattered in practice:

- **No API key.** Nothing to plumb through a secret scope, so the work stayed on
  harvesting / vectorizing / retrieval. The only requirement is a descriptive
  `User-Agent` (set in `app.yaml` as `WEATHER_USER_AGENT`).

One small helper is not NWS: NWS only accepts coordinates, so city names are
resolved to lat/lon with **Open-Meteo's free geocoding API**. It is used purely
as a geocoder - no weather data comes from it. Passing `"41.88,-87.63"` instead
of `"Chicago, IL"` skips that call entirely.

## Schema decisions

### `weather_documents` - one row per harvested text item

| column                      | why                                                                                                                                                                                                                               |
| --------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `id` (TEXT PK)              | dedup key. Alerts use the NWS `properties.id` (a URN). Forecast periods have no id, so they get a stable composite: `forecast:{gridId}:{gridX},{gridY}:{startTime}` - the same grid point and period always maps to the same row. |
| `location`                  | resolved `"City, ST"` as reported by NWS `relativeLocation`, so `"Chicago, IL"` and `"41.88,-87.63"` land under one label.                                                                                                        |
| `source_type`               | `alert` or `forecast`, CHECK-constrained. Drives the retrieval filter.                                                                                                                                                            |
| `headline`, `event`         | the alert headline / event name (`"Flash Flood Warning"`), or the forecast period name + short forecast.                                                                                                                          |
| `narrative_text`            | **the text that gets embedded** (see below).                                                                                                                                                                                      |
| `issued_at`, `effective_at` | alert `sent` / `effective`; forecast `generatedAt` / period `startTime`.                                                                                                                                                          |
| `payload` (JSONB)           | the raw API object, for provenance.                                                                                                                                                                                               |
| `synced_at`                 | bumped **only when `narrative_text` actually changed** - this is what drives re-embedding.                                                                                                                                        |

### `weather_embeddings` - one row per chunk

```sql
id TEXT PRIMARY KEY,                     -- "{document_id}#{chunk_index}"
document_id TEXT REFERENCES weather_documents(id) ON DELETE CASCADE,
chunk_index INT, chunk_text TEXT,
embedding VECTOR(384) NOT NULL,
model_name TEXT, created_at TIMESTAMPTZ,
UNIQUE (document_id, chunk_index)
```

- **Model: `sentence-transformers/all-MiniLM-L6-v2`, 384 dimensions** - the same
  model as the reference news pipeline, so both tables stay queryable with the
  same distance-operator conventions. `embeddings.MODEL_DIMENSIONS` maps model →
  dimension and `schema.py` builds `VECTOR(n)` from it, so swapping models means
  changing one env var and recreating the table.
- **Chunking: `CHUNK_SIZE=800`, `CHUNK_OVERLAP=100`** (the lab's defaults, kept
  on purpose). Most NWS text does not need chunking at all - a forecast period
  is ~150-250 characters and lands in a single chunk. Chunking only earns its
  keep on long alert bodies where `description` + `instruction` runs past 800
  characters, which is common for winter storm and extreme heat products.

### Incremental re-embedding

`POST /weather/sync` upserts on `id` (so re-running it never duplicates rows) and
only bumps `synced_at` when the narrative text actually changed. The embed step
then selects exactly the stale work:

```sql
WHERE newest_embedding_created_at IS NULL OR newest_embedding_created_at < d.synced_at
```

A re-synced document that got shorter also has its leftover high-index chunks
deleted, so `weather_embeddings` never keeps orphan text.

## Running the pipeline

You can easily use the app which is internally calling some API's we prepared for
fetching the documents, creating chunks, embedding data and adding to lakebase.

Or you can directly use the `ingest_weather_embeddings.py` notebook. Installation of
new packages is mandatory, along with set up the secrets using `setup_secrets.py`.
The app service role is also required to grant permissions on the specific app role.

Point it at whichever Lakebase instance/database/role you want homework 2 to
use; nothing here reads another homework's secret. To use a different
scope or key, change `LAKEBASE_SECRET_SCOPE` / `LAKEBASE_SECRET_KEY` in
`app.yaml` and the matching constants in `setup_secrets.py`.

Then upload this folder and deploy; `app.py` runs `schema.init_db()` at
startup, which enables `pgvector` and creates both tables and the HNSW index.
If you prefer to create them by hand, `sql/01…` and `sql/02…` contain the
same DDL.

## To run the notebooks

python notebooks/ingest_weather_embeddings.py --locations "Chicago, IL" "Austin, TX"
python notebooks/ingest_weather_embeddings.py --skip-sync # embed only, do not get new results

## Endpoints

| method | path                                            | notes                                                                                                                                     |
| ------ | ----------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------- |
| `POST` | `/weather/sync`                                 | `{"locations": [...], "limit": 50}` → harvest + upsert. Bad locations are reported per-location in `errors` instead of failing the batch. |
| `POST` | `/weather/embed`                                | `{"limit": 500}` → chunk + embed pending documents.                                                                                       |
| `POST` | `/weather/search`                               | `{"query": ..., "top_k": 5, "source_type": "alert"}` → cosine ranking.                                                                    |
| `GET`  | `/weather/search?query=...&top_k=5&summarize=1` | same, plus an optional LLM summary.                                                                                                       |
| `GET`  | `/weather/stats`, `/weather/documents`          | counts and recent rows, for the UI.                                                                                                       |
| `GET`  | `/healthz`                                      | liveness.                                                                                                                                 |

**Edge cases handled in search:** empty `weather_embeddings` returns `200` with
an empty `results` array and an explanatory `message` (not an error); a missing
or blank `query` returns `400`; `top_k` is clamped to 1–20 and a non-numeric
value falls back to 5; an unknown `source_type` returns `400`.

The model is loaded **once per process** behind an `lru_cache` in
`embeddings.get_model()` - shared by the query path and the ingest path, never
re-loaded per request.

## Stretch goals attempted

- **Dedup / upsert on `id`** - re-running `/weather/sync` refreshes rows instead
  of duplicating them, and only re-embeds what actually changed.
- **Two text types with a retrieval filter** - `source_type` (`alert` /
  `forecast` / `all`) on both search variants.
- **Basic RAG summary** - `GET /weather/search?summarize=1` feeds the retrieved
  chunks to a Databricks model serving endpoint. It is opt-in: set
  `WEATHER_SUMMARY_ENDPOINT` in `app.yaml` (e.g.
  `databricks-meta-llama-3-3-70b-instruct`). Unset, search behaves identically
  and the response carries a `summary_note` explaining how to enable it.

## Known limitations / what I'd do next

- Alerts change by the minute; a Databricks Job running the notebook every
  15 minutes would keep the index fresh. The job would work as-is
  (`--skip-sync` is there for embed-only runs), I just haven't wired the
  schedule.
- Extending possible locations to outside of US would require changing API
  as NWS only provides info from US.
- Allow users to subscribe to certain locations, and once synced with news,
  send an LLM provived announcement to alert from weather changes or risks
