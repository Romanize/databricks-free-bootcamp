"""
Agent tracing dashboard (capstone) - the third Databricks App.

A read-only Flask app over the `agent_tool_calls` table in the **tracing**
Lakebase database: what the agent has been asking the MCP server for, which
tools it used, how long they took, and what came back empty.

It shares no state with the other two apps beyond that one table, which is why
lakebase.py and tracing.py are duplicated here rather than imported.

## What it adds over homework 3's version

Homework 3's dashboard could show *what* was called but not *which conversation*
called it - its own README listed that as the obvious next step. The trace row
now carries a `session_id`, so this dashboard groups calls into conversations:
how many tools one question needed, how long the whole exchange took, and
whether the agent hit dead ends on the way.

The `no_data` status gets its own counter for the same reason it exists in the
MCP server: a tool answering "there is nothing loaded" is not an error, but a
lot of them means the ingestion job is not keeping up, and that is worth seeing.

    GET /                        the dashboard page
    GET /api/calls?limit=&tool=&status=
    GET /api/stats
    GET /healthz
"""

import datetime as dt
import decimal
import logging
import os

from flask import Flask, jsonify, render_template, request

import tracing

try:  # local development convenience
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("agent-dashboard")

app = Flask(__name__)

MAX_LIMIT = 200
DEFAULT_LIMIT = 50

# Kept in sync with the @mcp.tool functions in mcp_server/networth_mcp_server.py;
# it only populates the filter dropdown, so a stale entry is harmless.
TOOL_NAMES = [
    "get_networth_summary",
    "get_holdings_breakdown",
    "get_networth_history",
    "get_investment_plan",
    "create_investment_plan",
    "update_investment_plan",
    "activate_investment_plan",
    "get_investment_plan_projection",
    "search_ticker_news",
    "get_ticker_sentiment",
    "get_watchlist",
    "add_to_watchlist",
    "get_alpaca_account",
    "propose_trade",
    "execute_trade",
    "list_pending_trades",
]


def serialize(value):
    """Convert Postgres types json can't handle (timestamps, numerics)."""
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, dict):
        return {k: serialize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [serialize(item) for item in value]
    return value


def clamp_int(value, default: int, low: int, high: int) -> int:
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
    message = str(err) if status != 500 else "Could not reach the tracing database."
    return jsonify({"error": message}), status


@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok"})


@app.route("/")
def index():
    return render_template("index.html", tools=TOOL_NAMES)


@app.route("/api/calls")
def calls():
    status = request.args.get("status") or None
    if status and status not in ("success", "error", "no_data"):
        return jsonify({"error": "status must be success, error or no_data."}), 400

    rows = tracing.recent_calls(
        limit=clamp_int(request.args.get("limit"), DEFAULT_LIMIT, 1, MAX_LIMIT),
        tool_name=request.args.get("tool") or None,
        status=status,
    )
    return jsonify(serialize(rows))


@app.route("/api/stats")
def stats():
    return jsonify(serialize(tracing.stats()))


def startup():
    """Create the tracing table if the MCP server has not already."""
    try:
        tracing.init_db()
    except Exception:
        # Log and keep serving: the page then shows a clear connection error
        # instead of the container crash-looping in Databricks Apps.
        logger.exception("Failed to initialize the tracing schema")


startup()


if __name__ == "__main__":
    host = os.getenv("FLASK_RUN_HOST", "0.0.0.0")
    port = int(os.getenv("DATABRICKS_APP_PORT", os.getenv("FLASK_RUN_PORT", 8000)))
    app.run(host=host, port=port)
