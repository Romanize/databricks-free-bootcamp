"""
Net-worth tracker (capstone) - the main Databricks App.

CRUD over holdings, watchlist and investment plans; the net worth report
builder; the charts; the trade-approval queue; and a chat tab that proxies to
the Agent Bricks agent.

    GET    /                            the page
    GET    /api/stats                   counters for the status bar
    GET    /api/holdings                CRUD  (POST, PATCH /<id>, DELETE /<id>)
    GET    /api/watchlist               CRUD  (POST, DELETE /<symbol>)
    GET    /api/plans                   CRUD  (POST, POST /<id>/activate, DELETE /<id>)
    GET    /api/reports                 daily totals
    GET    /api/reports/monthly         month-end totals (last report per month)
    GET    /api/reports/latest          the current report and its lines
    GET    /api/reports/status          how stale the latest report is
    GET    /api/reports/draft           a prefilled, editable report to fill in
    POST   /api/reports                 submit a report
    GET    /api/reports/live            re-value now without saving
    GET    /api/charts/<name>           series for each chart
    GET    /api/news                    stored articles
    POST   /api/news/search             semantic search over the news embeddings
    POST   /api/news/sync               fetch one ticker's news now (slow: rate limited)
    POST   /api/news/embed              embed whatever is pending
    GET    /api/sentiment/highlights    the sentiment strip
    GET    /api/trades                  the approval queue
    POST   /api/trades                  propose one by hand
    POST   /api/trades/<id>/approve     ** mints the confirmation key, hands it to the agent **
    POST   /api/trades/<id>/reject
    GET    /api/broker                  Alpaca account and positions
    GET    /api/chat/status             whether the agent endpoint is configured
    POST   /api/chat                    forward a conversation to the agent
    GET    /healthz

Everything is USD. There is no currency handling anywhere.

Run locally:  python app.py      (needs LAKEBASE_URL in .env)
Deploy:       Databricks Apps, see app.yaml + ../README.md
"""

import datetime as dt
import decimal
import logging
import os

from flask import Flask, jsonify, render_template, request
from psycopg2 import errors as pg_errors

import agent_chat
import alpaca_api
import embeddings
import massive_api
import projections
import reports
import schema
import trading

try:  # local development convenience; not installed-critical in the app
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("networth-app")

app = Flask(__name__)

MAX_TOP_K = 20
MAX_QUERY_LEN = 500

# A report older than this puts the "time for a new reading" banner on the page.
# The brief asks for a month; configurable because a demo may want it sooner.
REPORT_STALE_DAYS = int(os.environ.get("REPORT_STALE_DAYS", 30))


class ValidationError(Exception):
    """Raised when user input fails validation; surfaced as a 400 with a message."""


# ---------------------------------------------------------------- helpers


def serialize(value):
    """Convert Postgres types json can't handle (timestamps, Decimal)."""
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, dict):
        return {k: serialize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [serialize(item) for item in value]
    return value


def payload() -> dict:
    """Return the request body as a dict, whether it arrives as JSON or a form."""
    if request.is_json:
        return request.get_json(silent=True) or {}
    return request.form.to_dict()


def clamp_int(value, default: int, low: int, high: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, parsed))


def required(data: dict, field: str) -> str:
    value = str(data.get(field) or "").strip()
    if not value:
        raise ValidationError(f"'{field}' is required.")
    return value


def optional_number(data: dict, field: str, minimum: float | None = None):
    """Parse an optional numeric field, rejecting text rather than coercing it."""
    raw = data.get(field)
    if raw in (None, ""):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ValidationError(f"'{field}' must be a number.")
    if minimum is not None and value < minimum:
        raise ValidationError(f"'{field}' must be at least {minimum}.")
    return value


def parse_date(value):
    """An optional ISO date, defaulting to today."""
    if not value:
        return dt.date.today()
    try:
        return dt.date.fromisoformat(str(value))
    except ValueError:
        raise ValidationError(f"{value!r} is not a valid YYYY-MM-DD date.")


# ---------------------------------------------------------------- errors


@app.errorhandler(ValidationError)
def handle_validation_error(err):
    return jsonify({"error": str(err)}), 400


@app.errorhandler(reports.ReportError)
def handle_report_error(err):
    return jsonify({"error": str(err)}), 400


@app.errorhandler(trading.TradeError)
def handle_trade_error(err):
    return jsonify({"error": str(err)}), 409


@app.errorhandler(agent_chat.ChatError)
def handle_chat_error(err):
    return jsonify({"error": str(err)}), 502


@app.errorhandler(projections.ProjectionError)
def handle_projection_error(err):
    return jsonify({"error": str(err)}), 400


@app.errorhandler(massive_api.MassiveAPIError)
def handle_massive_error(err):
    return jsonify({"error": str(err)}), 502


@app.errorhandler(alpaca_api.AlpacaAPIError)
def handle_alpaca_error(err):
    return jsonify({"error": str(err)}), 502


# Every unique constraint in the schema exists to enforce a rule the user can
# understand, so say the rule rather than leaking the constraint name. Without
# this the frontend shows "Something went wrong on the server." for what is
# really just a duplicate name.
UNIQUE_CONSTRAINT_MESSAGES = {
    "ux_holdings_alias": (
        "Another active holding already uses that alias. Aliases only have to be "
        "unique among active holdings, so deactivating the old one frees the name."
    ),
    "watchlist_symbol_key": "That symbol is already on the watchlist.",
    "ux_investment_plan_active": (
        "Another plan is already active. Activate this one instead - that "
        "deactivates the other in the same step."
    ),
    "networth_report_report_date_holding_id_key": (
        "That holding already has a line in the report for that date."
    ),
    "ticker_sentiments_symbol_article_id_key": (
        "That article's sentiment is already recorded for that symbol."
    ),
}


@app.errorhandler(pg_errors.UniqueViolation)
def handle_unique_violation(err):
    constraint = getattr(err.diag, "constraint_name", None)
    message = UNIQUE_CONSTRAINT_MESSAGES.get(
        constraint, "That record already exists."
    )
    logger.info("Unique violation on %s", constraint)
    return jsonify({"error": message}), 409


@app.errorhandler(Exception)
def handle_exception(err):
    """Always answer with JSON so the frontend's resp.json() never sees HTML."""
    logger.exception("Unhandled exception")
    status = getattr(err, "code", 500)
    if not isinstance(status, int):
        status = 500
    message = str(err) if status != 500 else "Something went wrong on the server."
    return jsonify({"error": message}), status


# ---------------------------------------------------------------- basics


@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok"})


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/stats")
def stats():
    return jsonify(
        serialize(
            {
                **schema.stats(),
                "pending_embeddings": embeddings.count_pending(),
                "embedding_model": embeddings.MODEL_NAME,
                "massive_configured": massive_api.is_configured(),
                "alpaca_configured": alpaca_api.is_configured(),
                "alpaca_paper": alpaca_api.is_paper(),
                "agent_configured": agent_chat.is_configured(),
            }
        )
    )


# ---------------------------------------------------------------- holdings


@app.route("/api/holdings")
def list_holdings():
    include_inactive = request.args.get("all") in ("1", "true", "yes")
    return jsonify(serialize(schema.list_holdings(active_only=not include_inactive)))


@app.route("/api/holdings", methods=["POST"])
def create_holding():
    """
    Add a holding.

    Holdings carry no values - what a thing is worth belongs to a report. This
    is only "what exists": an alias, a type, and a ticker for the priced kinds.
    """
    data = payload()
    holding_type = required(data, "holding_type")
    if holding_type not in schema.HOLDING_TYPES:
        raise ValidationError(f"holding_type must be one of: {', '.join(schema.HOLDING_TYPES)}.")

    symbol = (data.get("symbol") or "").strip().upper() or None
    if holding_type in schema.PRICED_TYPES and not symbol:
        raise ValidationError(f"A {holding_type} holding needs a symbol.")

    return jsonify(
        serialize(
            schema.create_holding(
                {
                    "alias": required(data, "alias"),
                    "holding_type": holding_type,
                    "symbol": symbol,
                    "institution": (data.get("institution") or "").strip() or None,
                    "notes": (data.get("notes") or "").strip() or None,
                }
            )
        )
    ), 201


@app.route("/api/holdings/<int:holding_id>", methods=["PATCH"])
def update_holding(holding_id: int):
    data = payload()
    if not schema.get_holding(holding_id):
        raise ValidationError(f"Holding {holding_id} does not exist.")

    return jsonify(
        serialize(
            schema.update_holding(
                holding_id,
                {
                    "alias": (data.get("alias") or "").strip() or None,
                    "symbol": (data.get("symbol") or "").strip().upper() or None,
                    "institution": data.get("institution"),
                    "notes": data.get("notes"),
                    "is_active": data.get("is_active"),
                },
            )
        )
    )


@app.route("/api/holdings/<int:holding_id>", methods=["DELETE"])
def delete_holding(holding_id: int):
    if not schema.delete_holding(holding_id):
        raise ValidationError(f"Holding {holding_id} does not exist.")
    return jsonify(
        {
            "deleted": holding_id,
            "note": "Deactivated. Past reports keep their lines and their labels.",
        }
    )


# ---------------------------------------------------------------- watchlist


@app.route("/api/watchlist")
def list_watchlist():
    return jsonify(
        serialize(
            {"watchlist": schema.list_watchlist(), "tracked_symbols": schema.tracked_symbols()}
        )
    )


@app.route("/api/watchlist", methods=["POST"])
def add_watchlist():
    data = payload()
    symbol = required(data, "symbol").upper()
    if len(symbol) > 20 or not all(c.isalnum() or c in ".-/" for c in symbol):
        raise ValidationError(f"{symbol!r} does not look like a ticker symbol.")
    return jsonify(
        serialize(schema.add_to_watchlist(symbol, (data.get("reason") or "").strip() or None))
    ), 201


@app.route("/api/watchlist/<symbol>", methods=["DELETE"])
def remove_watchlist(symbol: str):
    if not schema.remove_from_watchlist(symbol):
        raise ValidationError(f"{symbol.upper()} is not on the watchlist.")
    return jsonify({"removed": symbol.upper()})


# ---------------------------------------------------------------- plans


@app.route("/api/plans")
def list_plans():
    return jsonify(serialize(schema.list_plans()))


@app.route("/api/plans", methods=["POST"])
def create_plan():
    data = payload()
    record = {
        "name": required(data, "name"),
        "goal_amount": optional_number(data, "goal_amount", minimum=0) or 0,
        "expected_annual_rate": optional_number(data, "expected_annual_rate"),
        "years": clamp_int(data.get("years"), 20, 1, projections.MAX_YEARS),
        "expected_inflation": optional_number(data, "expected_inflation"),
        "monthly_contribution": optional_number(data, "monthly_contribution", minimum=0) or 0,
        "annual_contribution": optional_number(data, "annual_contribution", minimum=0) or 0,
        "start_date": data.get("start_date") or None,
        "created_by": "user",
    }
    if record["expected_annual_rate"] is None:
        raise ValidationError("'expected_annual_rate' is required (0.07 for 7%).")
    if record["expected_inflation"] is None:
        record["expected_inflation"] = 0.03

    # Validate before writing, so a nonsensical plan never reaches the database
    # and then the chart.
    projections.validate_plan(record)

    plan = schema.create_plan(record)
    if str(data.get("is_active")).lower() in ("1", "true", "yes", "on"):
        plan = schema.activate_plan(plan["id"])
    return jsonify(serialize(plan)), 201


@app.route("/api/plans/<int:plan_id>/activate", methods=["POST"])
def activate_plan(plan_id: int):
    plan = schema.activate_plan(plan_id)
    if not plan:
        raise ValidationError(f"Plan {plan_id} does not exist.")
    return jsonify(serialize(plan))


@app.route("/api/plans/<int:plan_id>", methods=["DELETE"])
def delete_plan(plan_id: int):
    if not schema.delete_plan(plan_id):
        raise ValidationError(f"Plan {plan_id} does not exist.")
    return jsonify({"deleted": plan_id})


# ---------------------------------------------------------------- reports


@app.route("/api/reports")
def list_reports():
    return jsonify(serialize(schema.report_history(clamp_int(request.args.get("limit"), 365, 1, 2000))))


@app.route("/api/reports/monthly")
def monthly_reports():
    """Month-end totals - the last report submitted in each month."""
    return jsonify(serialize(schema.monthly_history(clamp_int(request.args.get("limit"), 24, 1, 120))))


@app.route("/api/reports/latest")
def latest_report():
    report = schema.latest_report()
    if not report:
        return jsonify({"report": None, "lines": [], "message": "No report has been submitted yet."})
    return jsonify(
        serialize({"report": report, "lines": schema.report_lines(report["report_date"])})
    )


@app.route("/api/reports/status")
def report_status():
    """Drives the 'a month has passed' banner."""
    days = schema.days_since_last_report()
    report_date = schema.latest_report_date()
    return jsonify(
        serialize(
            {
                "has_report": report_date is not None,
                "last_report_date": report_date,
                "days_since": days,
                "stale_after_days": REPORT_STALE_DAYS,
                "overdue": report_date is None
                or (days is not None and days >= REPORT_STALE_DAYS),
            }
        )
    )


@app.route("/api/reports/draft")
def report_draft():
    """
    A prefilled, editable line per active holding.

    Quantities come from Alpaca where it knows them, otherwise from the last
    report; prices are live. Cash and bank balances are carried forward for you
    to correct - the app has no way to read them.
    """
    return jsonify(serialize(reports.build_draft(parse_date(request.args.get("report_date")))))


@app.route("/api/reports", methods=["POST"])
def submit_report():
    """Save a report. Re-submitting the same date replaces it."""
    data = payload()
    lines = data.get("lines")
    if not isinstance(lines, list) or not lines:
        raise ValidationError("'lines' must be a non-empty list.")
    return jsonify(serialize(reports.submit(parse_date(data.get("report_date")), lines)))


@app.route("/api/reports/live")
def live_report():
    """Re-value the latest report's quantities at current prices, without saving."""
    return jsonify(serialize(reports.live_valuation()))


# ---------------------------------------------------------------- charts


@app.route("/api/charts/networth")
def chart_networth():
    """Net worth over time. Monthly by default; ?granularity=daily for every report."""
    monthly = request.args.get("granularity", "monthly") != "daily"
    rows = (
        schema.monthly_history(clamp_int(request.args.get("limit"), 24, 2, 120))
        if monthly
        else schema.report_history(clamp_int(request.args.get("limit"), 365, 2, 2000))
    )
    return jsonify(
        serialize(
            {
                "granularity": "monthly" if monthly else "daily",
                "points": [
                    {
                        "date": row.get("month") or row["report_date"],
                        "report_date": row["report_date"],
                        "total": row["total_value"],
                        "invested": row["invested_value"],
                        "cash": row["cash_value"],
                    }
                    for row in rows
                ],
                "note": (
                    "One point per month, using the last report of that month."
                    if monthly
                    else "One point per report."
                ),
            }
        )
    )


@app.route("/api/charts/distribution")
def chart_distribution():
    """Where the money is, from the latest report."""
    report_date = schema.latest_report_date()
    if not report_date:
        return jsonify({"report_date": None, "by_type": [], "by_holding": []})

    lines = schema.report_lines(report_date)
    return jsonify(
        serialize(
            {
                "report_date": report_date,
                "by_type": schema.distribution(report_date),
                "by_holding": [
                    {"alias": line["alias"], "symbol": line["symbol"], "value": line["value"]}
                    for line in lines
                ],
            }
        )
    )


@app.route("/api/charts/projection")
def chart_projection():
    """The active plan projected from the latest net worth."""
    plan = schema.active_plan()
    if not plan:
        return jsonify({"plan": None, "message": "No active investment plan."})

    report = schema.latest_report()
    starting = float(report["total_value"]) if report else 0.0
    result = projections.project(plan, starting, points=request.args.get("points", "yearly"))
    # Same label the MCP tool attaches, so the chart and the agent describe the
    # projection's starting point identically.
    result["starting_value_source"] = (
        f"net worth report {report['report_date'].isoformat()}"
        if report
        else "no report yet - projected from zero"
    )
    return jsonify(serialize(result))


@app.route("/api/charts/sentiment")
def chart_sentiment():
    """Daily sentiment score for one ticker."""
    symbol = (request.args.get("symbol") or "").strip().upper()
    if not symbol:
        raise ValidationError("A 'symbol' query parameter is required.")
    days = clamp_int(request.args.get("days"), 90, 7, 365)
    return jsonify(
        serialize({"symbol": symbol, "days": days, "points": schema.sentiment_timeline(symbol, days)})
    )


# ---------------------------------------------------------------- news


@app.route("/api/news")
def list_news():
    symbol = (request.args.get("symbol") or "").strip().upper() or None
    return jsonify(
        serialize(schema.recent_articles(symbol, clamp_int(request.args.get("limit"), 20, 1, 100)))
    )


@app.route("/api/news/search", methods=["POST"])
def search_news():
    """Semantic search over the news embeddings - the same query the agent runs."""
    data = payload()
    query = required(data, "query")
    if len(query) > MAX_QUERY_LEN:
        raise ValidationError(f"Query must be {MAX_QUERY_LEN} characters or fewer.")

    if not schema.has_embeddings():
        return jsonify(
            {
                "query": query,
                "results": [],
                "message": "No news has been embedded yet - run the ingestion job first.",
            }
        )

    vector = embeddings.embed_query(query)
    rows = schema.search_news(
        vector,
        (data.get("symbol") or "").strip().upper() or None,
        clamp_int(data.get("top_k"), 5, 1, MAX_TOP_K),
        clamp_int(data.get("days"), 0, 0, 365) or None,
    )
    return jsonify(serialize({"query": query, "model": embeddings.MODEL_NAME, "results": rows}))


@app.route("/api/news/sync", methods=["POST"])
def sync_news():
    """
    Fetch one ticker's news right now.

    Deliberately one symbol per request: Massive's free tier is 5 calls/minute
    and the client sleeps 12.5s between calls, so a multi-symbol sync would run
    past the app's request timeout. The 2-hourly job is what covers everything.
    """
    data = payload()
    symbol = required(data, "symbol").upper()
    if not massive_api.is_configured():
        raise ValidationError("Massive is not configured. Add the API key with setup_secrets.py.")

    articles, sentiments = massive_api.fetch_news(
        symbol, limit=clamp_int(data.get("limit"), 20, 1, 50)
    )
    written = schema.upsert_articles(articles)
    scored = schema.upsert_sentiments(sentiments)
    return jsonify(
        {
            "symbol": symbol,
            "articles": written,
            "sentiments": scored,
            "pending_embeddings": embeddings.count_pending(),
            "note": f"Fetched {written} articles. Embed them to make them searchable.",
        }
    )


@app.route("/api/news/embed", methods=["POST"])
def embed_news():
    """Embed pending articles. Same code path as the notebook job."""
    return jsonify(
        serialize(embeddings.embed_pending(limit=clamp_int(payload().get("limit"), 200, 1, 2000)))
    )


@app.route("/api/sentiment/highlights")
def sentiment_highlights():
    """
    The sentiment strip: how the news reads for what you watch and what you own.

    Scoped to tracked symbols rather than everything in the table, so an article
    that happened to mention forty tickers does not fill the page.
    """
    days = clamp_int(request.args.get("days"), 14, 1, 180)
    symbols = schema.tracked_symbols()
    if not symbols:
        return jsonify({"days": days, "symbols": [], "message": "Nothing is being tracked yet."})

    return jsonify(
        serialize(
            {
                "days": days,
                "symbols": schema.sentiment_summary(symbols, days),
                "source": "Massive per-article sentiment, stored verbatim",
            }
        )
    )


# ---------------------------------------------------------------- trades


@app.route("/api/trades")
def list_trades():
    status = request.args.get("status") or None
    if status and status not in schema.TRADE_STATUSES:
        raise ValidationError(f"status must be one of: {', '.join(schema.TRADE_STATUSES)}.")
    return jsonify(
        serialize(schema.list_trades(status, clamp_int(request.args.get("limit"), 50, 1, 200)))
    )


@app.route("/api/trades", methods=["POST"])
def create_trade():
    """Propose a trade by hand. Same queue the agent's proposals land in."""
    data = payload()
    side = required(data, "side")
    if side not in ("buy", "sell"):
        raise ValidationError("side must be 'buy' or 'sell'.")

    order_type = (data.get("order_type") or "market").strip()
    if order_type not in ("market", "limit"):
        raise ValidationError("order_type must be 'market' or 'limit'.")

    quantity = optional_number(data, "quantity", minimum=0)
    if not quantity:
        raise ValidationError("'quantity' must be greater than zero.")

    limit_price = optional_number(data, "limit_price", minimum=0)
    if order_type == "limit" and limit_price is None:
        raise ValidationError("A limit order needs a limit_price.")

    return jsonify(
        serialize(
            schema.create_pending_trade(
                {
                    "symbol": required(data, "symbol").upper(),
                    "side": side,
                    "quantity": quantity,
                    "order_type": order_type,
                    "limit_price": limit_price,
                    "rationale": (data.get("rationale") or "").strip() or None,
                    "proposed_by": "user",
                }
            )
        )
    ), 201


@app.route("/api/trades/<int:trade_id>/approve", methods=["POST"])
def approve_trade(trade_id: int):
    """
    Accept a proposal: mint its confirmation key and hand it to the agent.

    **This route is the human-in-the-loop boundary.** The key does not exist
    until a person hits this endpoint, is stored only as a hash, and is spent the
    first time the agent redeems it. The agent has no tool that can reach here.

    The key is forwarded to the agent in the same request. It is never returned
    to the browser: the page has no use for it, and putting a live credential in
    a JSON response is how it ends up in a log or a screenshot.
    """
    # Checked BEFORE minting: an unusable key is still a live credential, and
    # the proposal would be left armed with no way to redeem or un-approve it
    # (reject_trade only accepts a pending trade, by design).
    if not agent_chat.is_configured():
        raise ValidationError(
            "The agent endpoint is not configured, so there is nothing that could "
            "execute this trade. Set CAPSTONE_AGENT_ENDPOINT and redeploy. The "
            "proposal is untouched and still pending."
        )

    approved, key = trading.issue_key(trade_id)

    instruction = (
        f"The user approved trade proposal #{trade_id} "
        f"({approved['side']} {approved['quantity']} {approved['symbol']}). "
        f"Call execute_trade with proposal_id={trade_id} and "
        f"confirmation_key={key} now, then tell them what happened."
    )
    try:
        reply = agent_chat.ask([{"role": "user", "content": instruction}])
    except agent_chat.ChatError as err:
        logger.exception("Could not hand trade #%s to the agent", trade_id)
        raise ValidationError(
            f"Could not reach the agent to execute the trade: {err}. The proposal "
            "is still approved - its key expires in "
            f"{schema.KEY_TTL_MINUTES} minutes."
        ) from err

    return jsonify(
        serialize(
            {
                "trade": schema.get_trade(trade_id),
                "agent_reply": reply.get("reply"),
                "note": (
                    "The confirmation key was sent to the agent, which executes the "
                    "order. Refresh the queue to see the outcome."
                ),
            }
        )
    )


@app.route("/api/trades/<int:trade_id>/reject", methods=["POST"])
def reject_trade(trade_id: int):
    reason = (payload().get("reason") or "").strip() or None
    return jsonify(serialize({"trade": trading.reject(trade_id, reason)}))


@app.route("/api/broker")
def broker():
    """Alpaca's own live view, for comparison with the last report."""
    if not alpaca_api.is_configured():
        return jsonify(
            {
                "configured": False,
                "message": "Alpaca is not configured. Add the keys with setup_secrets.py.",
            }
        )
    return jsonify(
        serialize(
            {
                "configured": True,
                "paper": alpaca_api.is_paper(),
                "account": alpaca_api.get_account(),
                "positions": alpaca_api.get_positions(),
            }
        )
    )


# ---------------------------------------------------------------- chat


@app.route("/api/chat/status")
def chat_status():
    return jsonify(agent_chat.status())


@app.route("/api/chat", methods=["POST"])
def chat():
    """Forward the conversation to the Agent Bricks agent."""
    messages = payload().get("messages")
    if not isinstance(messages, list) or not messages:
        raise ValidationError("'messages' must be a non-empty list of {role, content}.")
    return jsonify(agent_chat.ask(messages))


# ---------------------------------------------------------------- startup


def startup():
    """Create the Lakebase schema before serving traffic."""
    try:
        schema.init_db()
    except Exception:
        # Log and keep serving: the UI then shows a clear connection error
        # instead of the container crash-looping in Databricks Apps.
        logger.exception("Failed to initialize the Lakebase app schema")

    logger.info(
        "Startup: massive=%s alpaca=%s (paper=%s) agent=%s",
        massive_api.is_configured(),
        alpaca_api.is_configured(),
        alpaca_api.is_paper(),
        agent_chat.is_configured(),
    )


startup()


if __name__ == "__main__":
    host = os.getenv("FLASK_RUN_HOST", "0.0.0.0")
    port = int(os.getenv("DATABRICKS_APP_PORT", os.getenv("FLASK_RUN_PORT", 8000)))
    app.run(host=host, port=port)
