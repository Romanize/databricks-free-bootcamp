"""
Weather Intelligence app (Day 2 homework).

Unstructured weather text from the National Weather Service is harvested into
Lakebase, chunked and embedded with sentence-transformers, and retrieved with
pgvector cosine similarity:

    POST /weather/sync    {"locations": ["Chicago, IL"], "limit": 50}
    POST /weather/embed   {"limit": 500}
    POST /weather/search  {"query": "flash flood risk this weekend", "top_k": 5}
    GET  /weather/search?query=...&summarize=1

Run locally:  python app.py      (needs LAKEBASE_URL in .env)
Deploy:       Databricks Apps, see app.yaml + README_WEATHER.md
"""

import datetime as dt
import logging
import os

from flask import Flask, jsonify, render_template, request

import embeddings
import lakebase
import schema
from weather_client import WeatherClient, WeatherClientError

try:  # local development convenience; not installed-critical in the app
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("weather-app")

app = Flask(__name__)

MAX_LOCATIONS = 10
MAX_SYNC_LIMIT = 200
MAX_TOP_K = 20
MAX_QUERY_LEN = 500

# Optional stretch goal: a Databricks model serving endpoint used by
# GET /weather/search?summarize=1 to write a natural-language answer over the
# retrieved chunks. Search works exactly the same when this is not configured.
SUMMARY_ENDPOINT = os.environ.get("WEATHER_SUMMARY_ENDPOINT", "")


class ValidationError(Exception):
    """Raised when user input fails validation; surfaced as a 400 with a message."""


# ---------------------------------------------------------------- helpers


def serialize(row: dict) -> dict:
    """Convert Postgres types that json can't handle (timestamps) into strings."""
    return {
        k: (v.isoformat() if isinstance(v, (dt.datetime, dt.date)) else v)
        for k, v in row.items()
    }


def payload() -> dict:
    """Return the request body as a dict, whether it arrives as JSON or a form."""
    if request.is_json:
        return request.get_json(silent=True) or {}
    return request.form.to_dict()


def require_query(data: dict) -> str:
    query = (data.get("query") or "").strip()
    if not query:
        raise ValidationError("A non-empty 'query' is required.")
    if len(query) > MAX_QUERY_LEN:
        raise ValidationError(f"Query must be {MAX_QUERY_LEN} characters or fewer.")
    return query


def clamp_int(value, default: int, low: int, high: int) -> int:
    """Parse an optional integer and clamp it into [low, high]."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, parsed))


def optional_source_type(data: dict) -> str | None:
    """Stretch goal: let retrieval filter to alerts or forecasts only."""
    value = (data.get("source_type") or "").strip()
    if not value or value == "all":
        return None
    if value not in schema.SOURCE_TYPES:
        raise ValidationError(
            f"source_type must be one of: {', '.join(schema.SOURCE_TYPES)}, or 'all'."
        )
    return value


SEARCH_SQL = """
    SELECT d.id, d.location, d.source_type, d.headline, d.event,
           d.narrative_text, d.effective_at,
           e.chunk_index, e.chunk_text,
           1 - (e.embedding <=> %s::vector) AS similarity
    FROM weather_embeddings e
    JOIN weather_documents d ON d.id = e.document_id
    {where}
    ORDER BY e.embedding <=> %s::vector
    LIMIT %s
"""


def run_search(query: str, top_k: int, source_type: str | None) -> dict:
    """Embed the query and rank weather_embeddings by cosine similarity."""
    if not lakebase.query_one("SELECT 1 AS ok FROM weather_embeddings LIMIT 1"):
        return {
            "query": query,
            "results": [],
            "message": "No embeddings yet - run /weather/sync then /weather/embed first.",
        }

    vector = embeddings.embed_query(query)
    where = "WHERE d.source_type = %s" if source_type else ""
    params = [vector] + ([source_type] if source_type else []) + [vector, top_k]

    rows = lakebase.query(SEARCH_SQL.format(where=where), tuple(params))
    return {
        "query": query,
        "top_k": top_k,
        "source_type": source_type or "all",
        "model": embeddings.MODEL_NAME,
        "results": [serialize(row) for row in rows],
    }


def summarize_results(query: str, results: list[dict]) -> str | None:
    """
    Stretch goal: ask a Databricks serving endpoint to summarize the hits.

    Returns None when no endpoint is configured or the call fails - the search
    results themselves are always returned either way.
    """
    if not SUMMARY_ENDPOINT or not results:
        return None

    context = "\n\n".join(
        f"[{row['location']} | {row['source_type']}] {row['headline']}\n{row['chunk_text']}"
        for row in results
    )
    try:
        from databricks.sdk import WorkspaceClient
        from databricks.sdk.service.serving import ChatMessage, ChatMessageRole

        response = WorkspaceClient().serving_endpoints.query(
            name=SUMMARY_ENDPOINT,
            messages=[
                ChatMessage(
                    role=ChatMessageRole.SYSTEM,
                    content=(
                        "You summarize National Weather Service text. Answer the "
                        "question using only the passages provided, in at most "
                        "four sentences. Say so if they do not answer it."
                    ),
                ),
                ChatMessage(
                    role=ChatMessageRole.USER,
                    content=f"Question: {query}\n\nPassages:\n{context}",
                ),
            ],
            max_tokens=250,
        )
        return response.choices[0].message.content
    except Exception:
        logger.exception("Summary endpoint %s failed", SUMMARY_ENDPOINT)
        return None


# ---------------------------------------------------------------- errors


@app.errorhandler(ValidationError)
def handle_validation_error(err):
    return jsonify({"error": str(err)}), 400


@app.errorhandler(WeatherClientError)
def handle_weather_client_error(err):
    return jsonify({"error": str(err)}), 502


@app.errorhandler(Exception)
def handle_exception(err):
    """Always answer with JSON so the frontend's resp.json() never sees HTML."""
    logger.exception("Unhandled exception")
    status = getattr(err, "code", 500)
    if not isinstance(status, int):
        status = 500
    message = str(err) if status != 500 else "Something went wrong on the server."
    return jsonify({"error": message}), status


# ---------------------------------------------------------------- routes


@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok"})


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/weather/stats")
def stats():
    """Document/embedding counts for the UI's status bar."""
    by_type = lakebase.query(
        "SELECT source_type, COUNT(*) AS count FROM weather_documents GROUP BY source_type"
    )
    totals = lakebase.query_one(
        """
        SELECT (SELECT COUNT(*) FROM weather_documents)  AS documents,
               (SELECT COUNT(*) FROM weather_embeddings) AS chunks,
               (SELECT COUNT(DISTINCT location) FROM weather_documents) AS locations
        """
    )
    pending = lakebase.query_one(
        f"SELECT COUNT(*) AS pending FROM ({embeddings.PENDING_SQL}) p"
    )
    return jsonify(
        {
            **totals,
            "pending": pending["pending"],
            "by_source_type": {row["source_type"]: row["count"] for row in by_type},
            "model": embeddings.MODEL_NAME,
            "embedding_dim": embeddings.EMBEDDING_DIM,
        }
    )


@app.route("/weather/documents")
def list_documents():
    """Most recently synced documents, for the UI table."""
    limit = clamp_int(request.args.get("limit"), 25, 1, 100)
    rows = lakebase.query(
        """
        SELECT d.id, d.location, d.source_type, d.headline, d.event,
               d.effective_at, d.synced_at,
               (SELECT COUNT(*) FROM weather_embeddings e WHERE e.document_id = d.id)
                   AS chunk_count
        FROM weather_documents d
        ORDER BY d.synced_at DESC, d.id
        LIMIT %s
        """,
        (limit,),
    )
    return jsonify([serialize(row) for row in rows])


@app.route("/weather/sync", methods=["POST"])
def sync_weather():
    """Harvest NWS alerts + forecasts for the given locations into Lakebase."""
    data = payload()

    raw = data.get("locations") or []
    if isinstance(raw, str):  # form posts send one newline/comma separated string
        raw = [part for part in raw.replace("\n", ";").split(";")]
    locations = [str(item).strip() for item in raw if str(item).strip()]
    if not locations:
        raise ValidationError("Provide at least one location, e.g. 'Chicago, IL'.")
    if len(locations) > MAX_LOCATIONS:
        raise ValidationError(f"At most {MAX_LOCATIONS} locations per sync.")

    limit = clamp_int(data.get("limit"), 50, 1, MAX_SYNC_LIMIT)

    client = WeatherClient()
    documents, per_location, errors = [], {}, {}
    for location in locations:
        try:
            found = client.fetch_documents(location, limit=limit)
        except WeatherClientError as err:
            # One bad location should not sink the whole sync.
            errors[location] = str(err)
            continue
        documents.extend(found)
        per_location[location] = len(found)

    # The same statewide alert can come back for two cities in one request;
    # keep the first copy so the upsert never sees a duplicate key twice.
    unique = list({doc["id"]: doc for doc in documents}.values())
    synced = schema.upsert_documents(unique)

    if not synced and errors:
        raise WeatherClientError("; ".join(f"{loc}: {msg}" for loc, msg in errors.items()))

    return jsonify(
        {
            "synced": synced,
            "locations": per_location,
            "errors": errors,
            "alerts": sum(1 for doc in unique if doc["source_type"] == "alert"),
            "forecasts": sum(1 for doc in unique if doc["source_type"] == "forecast"),
        }
    )


@app.route("/weather/embed", methods=["POST"])
def embed_weather():
    """
    Embed every document that has no vectors yet (or was re-synced since).

    The same work runs as a batch job in notebooks/ingest_weather_embeddings.py;
    this route exists so the whole pipeline can be driven from the app UI.
    """
    limit = clamp_int(payload().get("limit"), 500, 1, 5000)
    return jsonify(embeddings.embed_pending(limit=limit))


@app.route("/weather/search", methods=["POST"])
def search_weather():
    data = payload()
    query = require_query(data)
    top_k = clamp_int(data.get("top_k"), 5, 1, MAX_TOP_K)
    return jsonify(run_search(query, top_k, optional_source_type(data)))


@app.route("/weather/search", methods=["GET"])
def search_weather_get():
    """
    Same retrieval as the POST variant, plus an optional LLM summary of the
    top results when ?summarize=1 and WEATHER_SUMMARY_ENDPOINT is configured.
    """
    args = request.args
    query = require_query(args)
    top_k = clamp_int(args.get("top_k"), 5, 1, MAX_TOP_K)
    response = run_search(query, top_k, optional_source_type(args))

    if args.get("summarize") in ("1", "true", "yes"):
        response["summary"] = summarize_results(query, response["results"])
        if response["summary"] is None and not SUMMARY_ENDPOINT:
            response["summary_note"] = (
                "Set WEATHER_SUMMARY_ENDPOINT to a Databricks serving endpoint "
                "to enable summaries."
            )
    return jsonify(response)


def startup():
    """Create the Lakebase schema before serving traffic."""
    try:
        schema.init_db()
    except Exception:
        # Log and keep serving: the UI then shows a clear connection error
        # instead of the container crash-looping in Databricks Apps.
        logger.exception("Failed to initialize the Lakebase weather schema")


startup()


if __name__ == "__main__":
    host = os.getenv("FLASK_RUN_HOST", "0.0.0.0")
    port = int(os.getenv("DATABRICKS_APP_PORT", os.getenv("FLASK_RUN_PORT", 8000)))
    app.run(host=host, port=port)
