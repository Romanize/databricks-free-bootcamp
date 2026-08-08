"""
Weather agent dashboard (homework 3, stretch goal).

A small read-only Flask app over the `weather_tool_calls` table that the MCP
server writes to: what the Agent Bricks agent has been asking for, which tools
it called, how long they took and what failed.

It shares no state with the MCP server other than the Lakebase table - the two
are separate Databricks Apps, deployed from separate folders, which is why
lakebase.py and schema.py are duplicated here rather than imported.

    GET /                      the dashboard page
    GET /api/calls?limit=&tool= recent tool calls as JSON
    GET /api/stats             headline counters
    GET /healthz

Run locally:  python app.py      (needs LAKEBASE_URL in .env)
Deploy:       Databricks Apps, see app.yaml + ../README.md
"""

import datetime as dt
import decimal
import logging
import os

from flask import Flask, jsonify, render_template, request

import lakebase
import schema

try:  # local development convenience; not installed-critical in the app
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("weather-dashboard")

app = Flask(__name__)

MAX_LIMIT = 200
DEFAULT_LIMIT = 50


def serialize(value):
    """Convert Postgres types json can't handle (timestamps, numerics)."""
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, dict):
        return {k: serialize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [serialize(item) for item in value]
    return value


def clamp_int(value, default: int, low: int, high: int) -> int:
    """Parse an optional integer and clamp it into [low, high]."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, parsed))


@app.errorhandler(Exception)
def handle_exception(err):
    """Always answer with JSON so the frontend's resp.json() never sees HTML."""
    logger.exception("Unhandled exception")
    status = getattr(err, "code", 500)
    if not isinstance(status, int):
        status = 500
    message = str(err) if status != 500 else "Could not reach the tool-call log."
    return jsonify({"error": message}), status


@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok"})


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/calls")
def api_calls():
    """Recent MCP tool calls, newest first, optionally filtered by tool name."""
    limit = clamp_int(request.args.get("limit"), DEFAULT_LIMIT, 1, MAX_LIMIT)
    tool = (request.args.get("tool") or "").strip() or None
    return jsonify([serialize(row) for row in schema.recent_calls(limit, tool)])


@app.route("/api/stats")
def api_stats():
    """Counters for the status bar: totals, error rate, per-tool breakdown."""
    return jsonify(serialize(schema.stats()))


def startup():
    """Make sure the table exists, so the dashboard works before the first call."""
    if not lakebase.is_configured():
        logger.warning("No Lakebase URL configured; the dashboard will show an error.")
        return
    try:
        schema.init_db()
    except Exception:
        # Log and keep serving: the UI then shows a clear connection error
        # instead of the container crash-looping in Databricks Apps.
        logger.exception("Failed to initialize the Lakebase tool-call table")


startup()


if __name__ == "__main__":
    host = os.getenv("FLASK_RUN_HOST", "0.0.0.0")
    port = int(os.getenv("DATABRICKS_APP_PORT", os.getenv("FLASK_RUN_PORT", 8000)))
    app.run(host=host, port=port)
