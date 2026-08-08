"""
Lakebase-powered AI support app (Day 1 homework).

A small Flask app backed by Lakebase (Databricks-managed Postgres). Users can
browse support tickets, read and add messages, open new tickets, change a
ticket's status and delete tickets. All data lives in Lakebase - nothing is
hard-coded in the UI.

Run locally:  python app.py      (needs LAKEBASE_URL in .env)
Deploy:       Databricks Apps, see app.yaml + README.md
"""

import datetime as dt
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
logger = logging.getLogger("support-app")

app = Flask(__name__)

MAX_TITLE_LEN = 200
MAX_MESSAGE_LEN = 2000


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


def require_text(data: dict, field: str, label: str, max_len: int) -> str:
    value = (data.get(field) or "").strip()
    if not value:
        raise ValidationError(f"{label} is required.")
    if len(value) > max_len:
        raise ValidationError(f"{label} must be {max_len} characters or fewer.")
    return value


def require_choice(data: dict, field: str, label: str, allowed: list[str], default=None) -> str:
    value = (data.get(field) or default or "").strip()
    if value not in allowed:
        raise ValidationError(f"{label} must be one of: {', '.join(allowed)}.")
    return value


def require_user(data: dict, field: str, label: str) -> int:
    raw = data.get(field)
    try:
        user_id = int(raw)
    except (TypeError, ValueError):
        raise ValidationError(f"{label} is required.")
    if not lakebase.query_one("SELECT user_id FROM users WHERE user_id = %s", (user_id,)):
        raise ValidationError(f"{label} is not a known user.")
    return user_id


def get_ticket_or_404(ticket_id: int) -> dict:
    row = lakebase.query_one(
        """
        SELECT t.ticket_id, t.title, t.status, t.priority, t.category,
               t.created_by, u.username AS created_by_username,
               t.created_at, t.updated_at,
               (SELECT COUNT(*) FROM ticket_messages m WHERE m.ticket_id = t.ticket_id)
                   AS message_count
        FROM tickets t
        JOIN users u ON u.user_id = t.created_by
        WHERE t.ticket_id = %s
        """,
        (ticket_id,),
    )
    if not row:
        raise ValidationError(f"Ticket {ticket_id} does not exist.")
    return serialize(row)


# ---------------------------------------------------------------- errors


@app.errorhandler(ValidationError)
def handle_validation_error(err):
    return jsonify({"error": str(err)}), 400


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


@app.route("/api/meta")
def meta():
    """Dropdown options for the UI: users plus the allowed enum values."""
    users = lakebase.query(
        "SELECT user_id, username, email FROM users ORDER BY username"
    )
    return jsonify(
        {
            "users": users,
            "statuses": schema.STATUSES,
            "priorities": schema.PRIORITIES,
            "categories": schema.CATEGORIES,
        }
    )


@app.route("/api/stats")
def stats():
    """Ticket counts by status, plus a total, for the stats bar."""
    rows = lakebase.query("SELECT status, COUNT(*) AS count FROM tickets GROUP BY status")
    by_status = {status: 0 for status in schema.STATUSES}
    for row in rows:
        by_status[row["status"]] = row["count"]
    return jsonify({"by_status": by_status, "total": sum(by_status.values())})


@app.route("/api/tickets")
def list_tickets():
    """All tickets, newest first, optionally filtered by status and/or priority."""
    where, params = [], []

    status = request.args.get("status")
    if status and status != "all":
        if status not in schema.STATUSES:
            raise ValidationError(f"Unknown status filter: {status}")
        where.append("t.status = %s")
        params.append(status)

    priority = request.args.get("priority")
    if priority and priority != "all":
        if priority not in schema.PRIORITIES:
            raise ValidationError(f"Unknown priority filter: {priority}")
        where.append("t.priority = %s")
        params.append(priority)

    clause = f"WHERE {' AND '.join(where)}" if where else ""
    rows = lakebase.query(
        f"""
        SELECT t.ticket_id, t.title, t.status, t.priority, t.category,
               t.created_by, u.username AS created_by_username,
               t.created_at, t.updated_at,
               (SELECT COUNT(*) FROM ticket_messages m WHERE m.ticket_id = t.ticket_id)
                   AS message_count
        FROM tickets t
        JOIN users u ON u.user_id = t.created_by
        {clause}
        ORDER BY t.updated_at DESC, t.ticket_id DESC
        """,
        tuple(params),
    )
    return jsonify([serialize(row) for row in rows])


@app.route("/api/tickets", methods=["POST"])
def create_ticket():
    data = payload()
    title = require_text(data, "title", "Title", MAX_TITLE_LEN)
    status = require_choice(data, "status", "Status", schema.STATUSES, "open")
    priority = require_choice(data, "priority", "Priority", schema.PRIORITIES, "medium")
    category = require_choice(data, "category", "Category", schema.CATEGORIES, "general")
    created_by = require_user(data, "created_by", "Created by")

    row = lakebase.execute(
        """
        INSERT INTO tickets (title, status, priority, category, created_by)
        VALUES (%s, %s, %s, %s, %s)
        RETURNING ticket_id
        """,
        (title, status, priority, category, created_by),
    )
    ticket = get_ticket_or_404(row["ticket_id"])

    # An opening message is optional; when given it becomes the first message.
    first_message = (data.get("message_text") or "").strip()
    if first_message:
        if len(first_message) > MAX_MESSAGE_LEN:
            raise ValidationError(f"Message must be {MAX_MESSAGE_LEN} characters or fewer.")
        lakebase.execute(
            "INSERT INTO ticket_messages (ticket_id, message_text, author) VALUES (%s, %s, %s)",
            (ticket["ticket_id"], first_message, created_by),
        )
        ticket["message_count"] = 1

    return jsonify(ticket), 201


@app.route("/api/tickets/<int:ticket_id>", methods=["PATCH"])
def update_ticket_status(ticket_id):
    """Update a ticket's status (the only field the UI edits after creation)."""
    get_ticket_or_404(ticket_id)
    status = require_choice(payload(), "status", "Status", schema.STATUSES)

    lakebase.execute(
        "UPDATE tickets SET status = %s, updated_at = now() WHERE ticket_id = %s",
        (status, ticket_id),
    )
    return jsonify(get_ticket_or_404(ticket_id))


@app.route("/api/tickets/<int:ticket_id>", methods=["DELETE"])
def delete_ticket(ticket_id):
    """Delete a ticket; its messages go with it via ON DELETE CASCADE."""
    get_ticket_or_404(ticket_id)
    lakebase.execute("DELETE FROM tickets WHERE ticket_id = %s", (ticket_id,))
    return jsonify({"deleted": ticket_id})


@app.route("/api/tickets/<int:ticket_id>/messages")
def list_messages(ticket_id):
    get_ticket_or_404(ticket_id)
    rows = lakebase.query(
        """
        SELECT m.message_id, m.ticket_id, m.message_text,
               m.author, u.username AS author_username, m.created_at
        FROM ticket_messages m
        JOIN users u ON u.user_id = m.author
        WHERE m.ticket_id = %s
        ORDER BY m.created_at, m.message_id
        """,
        (ticket_id,),
    )
    return jsonify([serialize(row) for row in rows])


@app.route("/api/tickets/<int:ticket_id>/messages", methods=["POST"])
def add_message(ticket_id):
    get_ticket_or_404(ticket_id)
    data = payload()
    text = require_text(data, "message_text", "Message", MAX_MESSAGE_LEN)
    author = require_user(data, "author", "Author")

    row = lakebase.execute(
        """
        INSERT INTO ticket_messages (ticket_id, message_text, author)
        VALUES (%s, %s, %s)
        RETURNING message_id
        """,
        (ticket_id, text, author),
    )
    # Touch the ticket so it sorts to the top of the list as recently active.
    lakebase.execute(
        "UPDATE tickets SET updated_at = now() WHERE ticket_id = %s", (ticket_id,)
    )
    return jsonify({"message_id": row["message_id"], "ticket_id": ticket_id}), 201


def startup():
    """Create the Lakebase schema and seed sample data before serving traffic."""
    try:
        schema.init_db()
    except Exception:
        # Log and keep serving: the UI then shows a clear connection error
        # instead of the container crash-looping in Databricks Apps.
        logger.exception("Failed to initialize the Lakebase schema")


startup()


if __name__ == "__main__":
    host = os.getenv("FLASK_RUN_HOST", "0.0.0.0")
    port = int(os.getenv("DATABRICKS_APP_PORT", os.getenv("FLASK_RUN_PORT", 8000)))
    app.run(host=host, port=port)
