"""
Net-worth MCP server (capstone).

A FastMCP server exposing seventeen tools over MCP streamable HTTP, registered in
Databricks as an external MCP server and used by an Agent Bricks agent. Same
shape as homework 3's weather MCP server, with a portfolio behind it.

    https://<app-name>-<id>.databricksapps.com/mcp

## The three rules this server is built around

**1. Numbers come from SQL, never from a vector search.**
Net worth, holdings, plan projections and trades are answered by typed queries
against Lakebase. Only news is retrieved by embedding similarity. Doing it the
other way round - RAG over your balances - is how an agent ends up confidently
inventing a portfolio, because a nearest-neighbour hit is *always* returned
whether or not it is relevant.

**2. The agent can execute a trade, but only with a key a human minted.**
`propose_trade` queues a row and mints nothing. `execute_trade` needs a
confirmation key that comes into existence only when a person clicks Accept in
the app, is stored only as a hash, expires, and is spent on first use. So the
agent may call `execute_trade` freely and will get nowhere without one. See
trading.py for the full argument.

**3. Writes are narrow and reversible.**
The agent can add to the watchlist and create, update or activate an investment
plan - all reversible, none of them moves money. It cannot delete anything, and
it cannot submit a net worth report: a report is a statement about the world
that the user has to make.

## Guardrails

Every tool returns a dict with a `status` of `success`, `error` or `no_data`,
and never raises across the MCP boundary. `no_data` is its own status on
purpose: "the news index is empty" and "the news search failed" lead to very
different sentences, and collapsing them into one is what produces an agent
that answers from memory. Numeric answers carry `as_of` and `source` so the
agent can state how fresh a price is instead of implying it is live.

Everything is USD; there is no currency anywhere in the system.
"""

import logging
import os
import time

from fastmcp import Context, FastMCP

import alpaca_api
import embeddings
import massive_api
import pricing
import projections
import reports
import schema
import tracing
import trading

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("networth-mcp")

mcp = FastMCP("networth-tracker")

MAX_TOP_K = 20
MAX_QUERY_LEN = 500
DEFAULT_SENTIMENT_DAYS = 30

# A report older than this is stale enough that the agent should say so before
# quoting it. Matches the app's "time for a new reading" banner.
REPORT_STALE_DAYS = int(os.environ.get("REPORT_STALE_DAYS", 30))

# Attached to the tools whose result the net worth app can draw. The app watches
# the stream for these tool names and renders the chart itself from the numbers
# below, so the agent must not try to draw one in text - and must not read the
# series out point by point either, because the user is already looking at it.
# Harmless in the Playground, where there is no chart: it is a statement about
# the client, not an instruction to produce anything.
CHART_NOTE = (
    "The net worth app draws this result as a chart for the user automatically. "
    "Describe what it shows and what to do about it - do not list every point "
    "and do not attempt ASCII art."
)


class ToolError(Exception):
    """A user-facing failure: returned as status 'error' with this message."""


class NoData(Exception):
    """The question is answerable, but nothing has been loaded yet."""


def _session_id(ctx) -> str | None:
    """
    Best-effort MCP session id for the trace row.

    Defensive by design: the attribute has moved between FastMCP versions, and
    losing a trace label must never break a working tool call.
    """
    for attribute in ("session_id", "client_id", "request_id"):
        try:
            value = getattr(ctx, attribute, None)
            if value:
                return str(value)
        except Exception:  # pragma: no cover - context internals vary
            continue
    return None


def _run(tool_name: str, arguments: dict, fn, ctx=None, symbol: str | None = None) -> dict:
    """
    Run one tool, time it, trace it, and never let an exception escape.

    Mirrors homework 3's wrapper. The agent gets a sentence, the dashboard gets
    a row, and the process keeps serving.
    """
    started = time.monotonic()
    status, summary, error_message, result = "success", None, None, None

    try:
        result = fn()
        summary = result.pop("_summary", None)
        result["status"] = "success"
    except NoData as err:
        status = "no_data"
        summary = str(err)
        result = {
            "status": "no_data",
            "message": str(err),
            # Spelled out so the agent says "there is no data yet" rather than
            # treating an empty result as licence to answer from memory.
            "guidance": (
                "Do not estimate or answer from prior knowledge. Tell the user "
                "this data has not been loaded yet."
            ),
        }
    except (ToolError, trading.TradeError, reports.ReportError,
            massive_api.MassiveAPIError, alpaca_api.AlpacaAPIError,
            projections.ProjectionError) as err:
        status = "error"
        error_message = str(err)
        result = {"status": "error", "message": str(err)}
    except Exception as err:  # unforeseen bug: a sentence, not a stack trace
        logger.exception("Tool %s failed", tool_name)
        status = "error"
        error_message = f"{type(err).__name__}: {err}"
        result = {
            "status": "error",
            "message": "The tool hit an unexpected internal error. Nothing was changed.",
        }

    duration_ms = int((time.monotonic() - started) * 1000)
    tracing.record_call(
        tool_name=tool_name,
        arguments=arguments,
        status=status,
        session_id=_session_id(ctx),
        symbol=symbol,
        summary=summary,
        error_message=error_message,
        duration_ms=duration_ms,
    )
    result.setdefault("duration_ms", duration_ms)
    return result


def _float(value, default=None):
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp(value, default: int, low: int, high: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, parsed))


def _iso(value):
    return value.isoformat() if value is not None and hasattr(value, "isoformat") else value


# ------------------------------------------------------------------ net worth


@mcp.tool
def get_networth_summary(refresh_prices: bool = False, ctx: Context | None = None) -> dict:
    """
    The user's net worth, from the most recent report they submitted.

    Args:
        refresh_prices: also re-value the portfolio at current prices and report
            the difference. This does NOT save anything - it is a read-only
            "what would it be right now" comparison, and it can only re-price
            tickers, not bank balances.

    A report is a snapshot the user fills in; there is no live position data
    between reports. Always state the report date. If no report exists, returns
    status "no_data".
    """

    def work():
        report = schema.latest_report()
        if not report:
            raise NoData(
                "No net worth report exists yet. The user needs to submit one in "
                "the app (Overview -> Start a report)."
            )

        lines = schema.report_lines(report["report_date"])
        age = schema.days_since_last_report()
        payload = {
            "report_date": _iso(report["report_date"]),
            "days_old": age,
            "is_stale": age is not None and age > REPORT_STALE_DAYS,
            "currency": "USD",
            "total_value": _float(report["total_value"]),
            "invested_value": _float(report["invested_value"]),
            "cash_value": _float(report["cash_value"]),
            "holdings_count": report["holdings_count"],
            "top_holdings": [
                {
                    "alias": line["alias"],
                    "type": line["holding_type"],
                    "symbol": line["symbol"],
                    "quantity": _float(line["quantity"]),
                    "price": _float(line["price"]),
                    "price_as_of": _iso(line["price_as_of"]),
                    "price_source": line["price_source"],
                    "value": _float(line["value"]),
                }
                for line in lines[:10]
            ],
            "_summary": f"USD {_float(report['total_value']):,.2f} as of {report['report_date']}",
        }

        if payload["is_stale"]:
            payload["staleness_warning"] = (
                f"This report is {age} days old. Say so before quoting it, and "
                "suggest submitting a fresh one."
            )

        if refresh_prices:
            try:
                payload["live_valuation"] = reports.live_valuation()
            except reports.ReportError as err:
                payload["live_valuation"] = {"error": str(err)}
        return payload

    return _run("get_networth_summary", {"refresh_prices": refresh_prices}, work, ctx)


@mcp.tool
def get_holdings_breakdown(group_by: str = "type", ctx: Context | None = None) -> dict:
    """
    How the portfolio is distributed, from the latest report.

    Args:
        group_by: "type" (ticker/crypto/cash/bank/wallet) or "holding"
            (every line individually).

    Use this for "what is my allocation", "am I too concentrated", "how much of
    my net worth is cash".
    """

    def work():
        if group_by not in ("type", "holding"):
            raise ToolError("group_by must be 'type' or 'holding'.")

        report = schema.latest_report()
        if not report:
            raise NoData("No net worth report exists yet, so there is nothing to break down.")

        total = _float(report["total_value"], 0) or 0
        if group_by == "type":
            groups = [
                {
                    "group": row["holding_type"],
                    "value": _float(row["value"]),
                    "holdings": row["holdings"],
                    "percent": round(_float(row["value"], 0) / total * 100, 2) if total else None,
                }
                for row in schema.distribution(report["report_date"])
            ]
        else:
            groups = [
                {
                    "group": line["alias"],
                    "symbol": line["symbol"],
                    "type": line["holding_type"],
                    "quantity": _float(line["quantity"]),
                    "value": _float(line["value"]),
                    "percent": round(_float(line["value"], 0) / total * 100, 2) if total else None,
                }
                for line in schema.report_lines(report["report_date"])
            ]

        largest = groups[0] if groups else None
        return {
            "report_date": _iso(report["report_date"]),
            "total_value": total,
            "group_by": group_by,
            "groups": groups,
            # Stated as a fact the agent can quote rather than a judgement. The
            # tool does not decide what "too concentrated" means.
            "largest_share_percent": largest["percent"] if largest else None,
            "chart": CHART_NOTE,
            "_summary": f"{len(groups)} groups by {group_by}, total {total:,.2f}",
        }

    return _run("get_holdings_breakdown", {"group_by": group_by}, work, ctx)


@mcp.tool
def get_networth_history(monthly: bool = True, limit: int = 24, ctx: Context | None = None) -> dict:
    """
    Net worth over time.

    Args:
        monthly: True for one point per month (the last report in each month),
            False for every individual report.
        limit: how many points to return.

    Use this for "how has my net worth changed", "am I saving more than last
    year", "when did it drop".
    """

    def work():
        points = (
            schema.monthly_history(_clamp(limit, 24, 1, 120))
            if monthly
            else schema.report_history(_clamp(limit, 24, 1, 365))
        )
        if not points:
            raise NoData("No reports exist yet, so there is no history to show.")

        first, last = points[0], points[-1]
        change = _float(last["total_value"], 0) - _float(first["total_value"], 0)
        return {
            "granularity": "monthly" if monthly else "per report",
            "points": [
                {
                    "date": _iso(point.get("month") or point["report_date"]),
                    "report_date": _iso(point["report_date"]),
                    "total_value": _float(point["total_value"]),
                    "invested_value": _float(point["invested_value"]),
                    "cash_value": _float(point["cash_value"]),
                }
                for point in points
            ],
            "change_over_window": round(change, 2),
            "chart": CHART_NOTE,
            "note": (
                "Monthly points are the last report submitted in each month. "
                "Reports are irregular, so gaps are gaps in reporting, not in value."
            ),
            "_summary": f"{len(points)} points, change {change:,.2f}",
        }

    return _run("get_networth_history", {"monthly": monthly, "limit": limit}, work, ctx)


# ------------------------------------------------------------ investment plan


@mcp.tool
def get_investment_plan(ctx: Context | None = None) -> dict:
    """
    The active investment plan and every saved alternative.

    Call this before offering to create or change a plan, so you can tell the
    user what they already have rather than asking them to repeat it.
    """

    def work():
        plans = schema.list_plans()
        if not plans:
            raise NoData(
                "No investment plan exists yet. Offer to create one - you need a "
                "goal amount, an expected annual return, a horizon in years, and "
                "how much they contribute monthly."
            )

        def describe(plan):
            return {
                "plan_id": plan["id"],
                "name": plan["name"],
                "is_active": plan["is_active"],
                "goal_amount": _float(plan["goal_amount"]),
                "expected_annual_rate": _float(plan["expected_annual_rate"]),
                "expected_inflation": _float(plan["expected_inflation"]),
                "years": plan["years"],
                "monthly_contribution": _float(plan["monthly_contribution"]),
                "annual_contribution": _float(plan["annual_contribution"]),
                "created_by": plan["created_by"],
            }

        active = next((p for p in plans if p["is_active"]), None)
        return {
            "active_plan": describe(active) if active else None,
            "plans": [describe(p) for p in plans],
            "note": (
                "Rates are decimals: 0.07 means 7%."
                + ("" if active else " No plan is active, so nothing is being charted.")
            ),
            "_summary": f"{len(plans)} plans, active: {active['name'] if active else 'none'}",
        }

    return _run("get_investment_plan", {}, work, ctx)


def _plan_fields(
    name, goal_amount, expected_annual_rate, years, expected_inflation,
    monthly_contribution, annual_contribution, required: bool,
) -> dict:
    """Validate and collect plan inputs shared by create and update."""
    fields = {
        "name": (name or "").strip() or None,
        "goal_amount": _float(goal_amount),
        "expected_annual_rate": _float(expected_annual_rate),
        "years": None if years is None else _clamp(years, 20, 1, projections.MAX_YEARS),
        "expected_inflation": _float(expected_inflation),
        "monthly_contribution": _float(monthly_contribution),
        "annual_contribution": _float(annual_contribution),
    }

    if required:
        missing = [
            key for key in ("name", "goal_amount", "expected_annual_rate", "years")
            if fields[key] is None
        ]
        if missing:
            raise ToolError(
                f"A new plan needs {', '.join(missing)}. Ask the user for the "
                "missing values rather than guessing them."
            )
        if fields["expected_inflation"] is None:
            fields["expected_inflation"] = 0.03
        for key in ("monthly_contribution", "annual_contribution"):
            if fields[key] is None:
                fields[key] = 0.0

    if fields["goal_amount"] is not None and fields["goal_amount"] <= 0:
        raise ToolError("goal_amount must be greater than zero.")
    for key in ("monthly_contribution", "annual_contribution"):
        if fields[key] is not None and fields[key] < 0:
            raise ToolError(f"{key} cannot be negative.")
    return fields


@mcp.tool
def create_investment_plan(
    name: str,
    goal_amount: float,
    expected_annual_rate: float,
    years: int,
    monthly_contribution: float = 0,
    annual_contribution: float = 0,
    expected_inflation: float = 0.03,
    activate: bool = False,
    ctx: Context | None = None,
) -> dict:
    """
    Create an investment plan from values the user gave you.

    Args:
        name: what to call it, e.g. "Retire at 60".
        goal_amount: the target, in USD.
        expected_annual_rate: as a DECIMAL - 0.07 means 7%.
        years: horizon, 1-80.
        monthly_contribution: USD added every month.
        annual_contribution: USD added once a year, on top of the monthly amount.
        expected_inflation: as a decimal, default 0.03.
        activate: make this the plan that gets charted and projected.

    Never invent these numbers. Ask for anything the user has not told you -
    especially the expected return, where a guess quietly changes the answer by
    hundreds of thousands. Echo back what you saved.
    """

    def work():
        fields = _plan_fields(
            name, goal_amount, expected_annual_rate, years, expected_inflation,
            monthly_contribution, annual_contribution, required=True,
        )
        # Validate before writing, so a nonsensical plan never reaches the
        # database and then the chart.
        projections.validate_plan(fields)

        plan = schema.create_plan({**fields, "created_by": "agent"})
        if activate:
            plan = schema.activate_plan(plan["id"])

        return {
            "plan_id": plan["id"],
            "name": plan["name"],
            "goal_amount": _float(plan["goal_amount"]),
            "expected_annual_rate": _float(plan["expected_annual_rate"]),
            "years": plan["years"],
            "expected_inflation": _float(plan["expected_inflation"]),
            "monthly_contribution": _float(plan["monthly_contribution"]),
            "annual_contribution": _float(plan["annual_contribution"]),
            "is_active": plan["is_active"],
            "note": (
                "Plan saved and made active."
                if plan["is_active"]
                else "Plan saved but NOT active - the charts still use the previous "
                     "plan. Offer to activate it."
            ),
            "_summary": f"created plan {plan['name']} (active={plan['is_active']})",
        }

    return _run(
        "create_investment_plan",
        {"name": name, "goal_amount": goal_amount, "expected_annual_rate": expected_annual_rate,
         "years": years, "activate": activate},
        work, ctx,
    )


@mcp.tool
def update_investment_plan(
    plan_id: int,
    name: str | None = None,
    goal_amount: float | None = None,
    expected_annual_rate: float | None = None,
    years: int | None = None,
    monthly_contribution: float | None = None,
    annual_contribution: float | None = None,
    expected_inflation: float | None = None,
    ctx: Context | None = None,
) -> dict:
    """
    Change an existing plan. Only the fields you pass are modified.

    Args:
        plan_id: from get_investment_plan.
        (everything else): the values to change. Rates are decimals.

    Use this for "what if I put in another $200 a month" when the user wants it
    saved. For a hypothetical they have not committed to, do not write anything -
    say what it would do instead.
    """

    def work():
        existing = schema.get_plan(plan_id)
        if not existing:
            raise ToolError(f"No plan with id {plan_id}. Call get_investment_plan first.")

        fields = _plan_fields(
            name, goal_amount, expected_annual_rate, years, expected_inflation,
            monthly_contribution, annual_contribution, required=False,
        )
        if not any(value is not None for value in fields.values()):
            raise ToolError("Nothing to update - pass at least one field to change.")

        # Validate the plan as it will be after the patch, not just the patch.
        merged = {**dict(existing), **{k: v for k, v in fields.items() if v is not None}}
        projections.validate_plan(merged)

        plan = schema.update_plan(plan_id, fields)
        changed = [key for key, value in fields.items() if value is not None]
        return {
            "plan_id": plan["id"],
            "name": plan["name"],
            "changed_fields": changed,
            "goal_amount": _float(plan["goal_amount"]),
            "expected_annual_rate": _float(plan["expected_annual_rate"]),
            "years": plan["years"],
            "monthly_contribution": _float(plan["monthly_contribution"]),
            "is_active": plan["is_active"],
            "note": "Updated. Re-run the projection to show the user what changed.",
            "_summary": f"updated plan {plan_id}: {', '.join(changed)}",
        }

    return _run("update_investment_plan", {"plan_id": plan_id}, work, ctx)


@mcp.tool
def activate_investment_plan(plan_id: int, ctx: Context | None = None) -> dict:
    """
    Make one plan the active one. Every other plan is deactivated.

    The active plan is what the app charts and what
    get_investment_plan_projection uses, so confirm with the user before
    switching - it changes what they see on the Plan tab.
    """

    def work():
        if not schema.get_plan(plan_id):
            raise ToolError(f"No plan with id {plan_id}. Call get_investment_plan first.")

        plan = schema.activate_plan(plan_id)
        return {
            "plan_id": plan["id"],
            "name": plan["name"],
            "is_active": plan["is_active"],
            "note": "This is now the plan the app charts and projects.",
            "_summary": f"activated plan {plan['name']}",
        }

    return _run("activate_investment_plan", {"plan_id": plan_id}, work, ctx)


@mcp.tool
def get_investment_plan_projection(points: str = "yearly", ctx: Context | None = None) -> dict:
    """
    Project the active investment plan forward from the current net worth.

    Args:
        points: "yearly" (default) or "monthly" for every step.

    Returns nominal and inflation-adjusted ("real") values, total contributions,
    and whether the goal is reached inside the plan's horizon. Always report the
    real figure alongside the nominal one - they answer different questions.
    """

    def work():
        plan = schema.active_plan()
        if not plan:
            raise NoData(
                "No active investment plan. Offer to create one with "
                "create_investment_plan, or activate an existing one."
            )

        report = schema.latest_report()
        starting_value = _float(report["total_value"], 0) if report else 0.0

        result = projections.project(plan, starting_value, points=points)
        result["plan_id"] = plan["id"]
        result["chart"] = CHART_NOTE
        result["starting_value_source"] = (
            f"net worth report {_iso(report['report_date'])}" if report
            else "no report yet - projected from zero"
        )
        goal = result["goal_reached"]
        result["_summary"] = (
            f"{plan['name']}: {result['final_nominal']:,.0f} nominal / "
            f"{result['final_real']:,.0f} real after {result['years']}y"
        )
        if goal.get("goal_set") and not goal.get("reached_nominal"):
            result["shortfall_note"] = (
                "The goal is not reached inside the plan horizon at these "
                "assumptions. Say so plainly."
            )
        return result

    return _run("get_investment_plan_projection", {"points": points}, work, ctx)



@mcp.tool
def project_scenario(
    years: int,
    goal_amount: float | None = None,
    starting_value: float | None = None,
    expected_annual_rate: float = 0.07,
    monthly_contribution: float = 0,
    annual_contribution: float = 0,
    expected_inflation: float = 0.03,
    name: str = "Scenario",
    ctx: Context | None = None,
) -> dict:
    """
    Project a what-if that is NOT saved anywhere, and solve what it would take.

    Use this for hypotheticals - "I am 32 with $200k, what would it take to have
    $2M by 40?", "what if I put in $3k a month instead?", "what if returns are
    only 5%?". Nothing is written to the database, so you never need permission
    to run it and you never need to create a plan just to answer a question.
    Use create_investment_plan only when the user asks to keep the scenario.

    Args:
        years: horizon in years, 1-80. For "retire at 40 and I am 32", that is 8.
        goal_amount: the target in USD. Give it whenever the user named one -
            it is what makes the answer a number instead of a curve.
        starting_value: USD to start from. Leave it out to start from the user's
            latest net worth report.
        expected_annual_rate: as a DECIMAL - 0.07 means 7%. Defaults to 0.07.
        monthly_contribution: USD added every month in this scenario.
        annual_contribution: USD added once a year on top.
        expected_inflation: as a decimal, defaults to 0.03.
        name: a label for the chart, e.g. "Retire at 40".

    Returns the same nominal / real / contributed series as the plan projection,
    plus `required_monthly_contribution`: the smallest monthly amount that
    actually reaches the goal. Answer with that number - "you would need about
    $X a month" is the answer to "what can I do", where a curve is not.

    You must state every value in `assumptions`, because those are the ones the
    user did not give you and the answer moves a long way when they are wrong.
    """

    def work():
        if starting_value is not None and _float(starting_value, 0) < 0:
            raise ToolError("starting_value cannot be negative.")

        assumptions = []
        if starting_value is None:
            report = schema.latest_report()
            if not report:
                raise NoData(
                    "No starting value was given and no net worth report exists "
                    "yet. Ask the user what they are starting from, or pass "
                    "starting_value explicitly."
                )
            start = _float(report["total_value"], 0) or 0.0
            source = f"net worth report {_iso(report['report_date'])}"
        else:
            start = _float(starting_value, 0) or 0.0
            source = "given by the user"

        plan = {
            "name": (name or "Scenario").strip() or "Scenario",
            "goal_amount": _float(goal_amount),
            "expected_annual_rate": _float(expected_annual_rate, 0.07),
            "years": _clamp(years, 10, 1, projections.MAX_YEARS),
            "expected_inflation": _float(expected_inflation, 0.03),
            "monthly_contribution": max(_float(monthly_contribution, 0) or 0.0, 0.0),
            "annual_contribution": max(_float(annual_contribution, 0) or 0.0, 0.0),
        }
        if plan["goal_amount"] is not None and plan["goal_amount"] <= 0:
            raise ToolError("goal_amount must be greater than zero.")

        # Only the ones the caller did not set: an assumption the user made
        # themselves is not an assumption, and listing it back as one reads as
        # though the tool invented it.
        if expected_annual_rate == 0.07:
            assumptions.append("7% expected annual return")
        if expected_inflation == 0.03:
            assumptions.append("3% annual inflation")
        assumptions.append(f"starting value {start:,.0f} ({source})")

        result = projections.project(plan, start)
        result["saved"] = False
        result["starting_value_source"] = source
        result["assumptions"] = assumptions
        result["chart"] = CHART_NOTE

        # The point of the whole tool for a "what would it take" question. Both
        # are given because they are different answers: hitting $2M of 2034
        # dollars is a much smaller ask than $2M of today's.
        if plan["goal_amount"]:
            needed = projections.required_monthly_contribution(plan, start)
            needed_real = projections.required_monthly_contribution(plan, start, real=True)
            result["required_monthly_contribution"] = needed
            result["required_monthly_contribution_real"] = needed_real
            result["required_note"] = (
                "Monthly contribution needed to reach the goal within the "
                "horizon. '_real' reaches it in today's money, which is the "
                "honest target for a retirement number. null means the goal is "
                "out of reach at these assumptions no matter the contribution - "
                "say so, and suggest a longer horizon or a smaller goal."
            )

        result["_summary"] = (
            f"{plan['name']}: {start:,.0f} -> {result['final_nominal']:,.0f} "
            f"nominal over {plan['years']}y"
            + (
                f", needs {result['required_monthly_contribution']:,.0f}/mo"
                if plan["goal_amount"] and result.get("required_monthly_contribution") is not None
                else ""
            )
        )
        return result

    return _run(
        "project_scenario",
        {
            "years": years, "goal_amount": goal_amount, "starting_value": starting_value,
            "expected_annual_rate": expected_annual_rate,
            "monthly_contribution": monthly_contribution,
            "annual_contribution": annual_contribution,
            "expected_inflation": expected_inflation, "name": name,
        },
        work,
        ctx,
    )


# ----------------------------------------------------------------------- news


@mcp.tool
def search_ticker_news(
    query: str, symbol: str | None = None, top_k: int = 5,
    days: int | None = None, ctx: Context | None = None,
) -> dict:
    """
    Semantic search over stored news articles for watched and held tickers.

    Args:
        query: what to look for, in natural language.
        symbol: restrict to one ticker, e.g. "AAPL".
        top_k: how many passages to return (1-20).
        days: only consider articles published in the last N days.

    This is the ONLY tool backed by embeddings. It searches news the
    every-2-hours job has already ingested - it cannot fetch new articles, and
    it knows nothing about tickers that are neither held nor on the watchlist.
    """

    def work():
        text = (query or "").strip()
        if not text:
            raise ToolError("A non-empty 'query' is required.")
        if len(text) > MAX_QUERY_LEN:
            raise ToolError(f"Query must be {MAX_QUERY_LEN} characters or fewer.")

        if not schema.has_embeddings():
            raise NoData(
                "The news index is empty - the ingestion job has not run yet. "
                "There is no news to search."
            )

        limit = _clamp(top_k, 5, 1, MAX_TOP_K)
        vector = embeddings.embed_query(text)
        rows = schema.search_news(vector, symbol, limit, days)

        if not rows:
            scope = f" for {symbol.upper()}" if symbol else ""
            window = f" in the last {days} days" if days else ""
            raise NoData(
                f"No stored articles{scope}{window} match that query. The index "
                "only covers tickers on the watchlist or currently held."
            )

        return {
            "query": text,
            "symbol": symbol.upper() if symbol else None,
            "top_k": limit,
            "model": embeddings.MODEL_NAME,
            "results": [
                {
                    "title": row["title"],
                    "publisher": row["publisher"],
                    "published_utc": _iso(row["published_utc"]),
                    "article_url": row["article_url"],
                    "tickers": row["tickers"],
                    "passage": row["chunk_text"],
                    "similarity": round(_float(row["similarity"], 0), 4),
                    "sentiment": row["sentiment"],
                    "sentiment_reasoning": row["sentiment_reasoning"],
                }
                for row in rows
            ],
            "_summary": f"{len(rows)} passages for {text[:60]!r}",
        }

    return _run(
        "search_ticker_news",
        {"query": query, "symbol": symbol, "top_k": top_k, "days": days},
        work, ctx, symbol=symbol.upper() if symbol else None,
    )


@mcp.tool
def get_ticker_sentiment(
    symbol: str | None = None, days: int = 30, ctx: Context | None = None
) -> dict:
    """
    News sentiment for one ticker or for everything being tracked.

    Args:
        symbol: one ticker, or omit for every tracked ticker.
        days: lookback window, default 30.

    Sentiment is assigned per article by the Massive news API and stored
    verbatim - it is not computed here and not inferred by a model. The score
    runs -1 (all negative) to +1 (all positive). Always report the article
    count: a +1.0 score from two articles is not a signal.
    """

    def work():
        window = _clamp(days, DEFAULT_SENTIMENT_DAYS, 1, 365)
        rows = schema.sentiment_summary([symbol] if symbol else None, window)

        if not rows:
            scope = f" for {symbol.upper()}" if symbol else ""
            raise NoData(
                f"No sentiment data{scope} in the last {window} days. Either the "
                "news job has not run, or no articles mentioned it."
            )

        result = {
            "days": window,
            "symbols": [
                {
                    "symbol": row["symbol"],
                    "articles": row["articles"],
                    "positive": row["positive"],
                    "neutral": row["neutral"],
                    "negative": row["negative"],
                    "score": _float(row["score"]),
                    "latest_article_at": _iso(row["latest_article_at"]),
                    "confidence": "low" if row["articles"] < 3 else "normal",
                }
                for row in rows
            ],
            "source": "Massive news API per-article insights, stored verbatim",
            "_summary": f"sentiment for {len(rows)} symbols over {window}d",
        }

        if symbol:
            result["timeline"] = [
                {"day": _iso(row["day"]), "articles": row["articles"], "score": _float(row["score"])}
                for row in schema.sentiment_timeline(symbol, window)
            ]
        return result

    return _run(
        "get_ticker_sentiment", {"symbol": symbol, "days": days}, work, ctx,
        symbol=symbol.upper() if symbol else None,
    )


# ------------------------------------------------------------------ watchlist


@mcp.tool
def get_watchlist(ctx: Context | None = None) -> dict:
    """
    The tickers being tracked: the watchlist plus everything currently held.

    News is ingested for exactly this set, so it also tells you what
    search_ticker_news can possibly know about.
    """

    def work():
        watchlist = schema.list_watchlist()
        tracked = schema.tracked_symbols()
        return {
            "watchlist": [
                {"symbol": row["symbol"], "reason": row["reason"], "added_at": _iso(row["added_at"])}
                for row in watchlist
            ],
            "tracked_symbols": tracked,
            "note": (
                "News is ingested for every tracked symbol (watchlist + holdings) "
                "on a 2-hour schedule."
            ),
            "_summary": f"{len(watchlist)} watched, {len(tracked)} tracked in total",
        }

    return _run("get_watchlist", {}, work, ctx)


@mcp.tool
def add_to_watchlist(
    symbol: str, reason: str | None = None, ctx: Context | None = None
) -> dict:
    """
    Add a ticker to the watchlist so the next news run starts ingesting it.

    Args:
        symbol: the ticker, e.g. "NVDA".
        reason: why it is being watched - shown in the app.

    Safe and reversible: this only schedules news collection. It does not buy
    anything and does not change the portfolio. Tell the user the news will
    appear after the next ingestion run, not immediately.
    """

    def work():
        clean = (symbol or "").strip().upper()
        if not clean:
            raise ToolError("A non-empty 'symbol' is required.")
        if len(clean) > 20 or not all(c.isalnum() or c in ".-/" for c in clean):
            raise ToolError(f"{clean!r} does not look like a ticker symbol.")

        row = schema.add_to_watchlist(clean, reason)
        return {
            "symbol": row["symbol"],
            "reason": row["reason"],
            "added_at": _iso(row["added_at"]),
            "note": (
                "Added. News for this ticker will be ingested on the next "
                "2-hourly run; there is nothing to search for it yet."
            ),
            "_summary": f"watchlist += {row['symbol']}",
        }

    return _run("add_to_watchlist", {"symbol": symbol, "reason": reason}, work, ctx, symbol=symbol)


# --------------------------------------------------------------------- broker


@mcp.tool
def get_alpaca_account(ctx: Context | None = None) -> dict:
    """
    Read-only view of the Alpaca brokerage account: cash, equity, buying power
    and open positions.

    This is the broker's live view, which will differ from the net worth report -
    the report is a snapshot from whenever it was submitted, and it also covers
    bank accounts and wallets Alpaca knows nothing about.
    """

    def work():
        if not alpaca_api.is_configured():
            raise NoData("Alpaca is not configured, so there is no brokerage account to read.")

        account = alpaca_api.get_account()
        positions = alpaca_api.get_positions()
        return {
            "account": account,
            "positions": positions,
            "paper": account.get("paper"),
            "note": (
                "Paper trading account." if account.get("paper")
                else "LIVE trading account - real money."
            ),
            "_summary": f"equity {account.get('equity')}, {len(positions)} positions",
        }

    return _run("get_alpaca_account", {}, work, ctx)


@mcp.tool
def propose_trade(
    symbol: str,
    side: str,
    quantity: float,
    order_type: str = "market",
    limit_price: float | None = None,
    rationale: str | None = None,
    ctx: Context | None = None,
) -> dict:
    """
    Queue a trade for the user to approve. THIS DOES NOT PLACE AN ORDER.

    Args:
        symbol: ticker to trade, e.g. "AAPL".
        side: "buy" or "sell".
        quantity: number of shares (may be fractional).
        order_type: "market" or "limit".
        limit_price: required when order_type is "limit".
        rationale: why you are proposing this - the user reads it before deciding.

    The proposal appears in the app's approval queue. When the user clicks
    Accept there, they will send you a confirmation key; only then can you call
    execute_trade. You cannot approve your own proposal and you cannot obtain
    the key any other way, so never claim an order has been placed here. Report
    the proposal id and that it is waiting for them in the app.
    """

    def work():
        clean = (symbol or "").strip().upper()
        if not clean:
            raise ToolError("A non-empty 'symbol' is required.")
        if side not in ("buy", "sell"):
            raise ToolError("side must be 'buy' or 'sell'.")
        if order_type not in ("market", "limit"):
            raise ToolError("order_type must be 'market' or 'limit'.")

        qty = _float(quantity)
        if qty is None or qty <= 0:
            raise ToolError("quantity must be a positive number.")
        if order_type == "limit" and _float(limit_price) is None:
            raise ToolError("A limit order needs a limit_price.")

        trade = schema.create_pending_trade(
            {
                "symbol": clean, "side": side, "quantity": qty,
                "order_type": order_type, "limit_price": _float(limit_price),
                "rationale": rationale, "proposed_by": "agent",
            }
        )

        # Context that helps the human decide, not a decision made for them.
        estimate = None
        try:
            quote = pricing.resolve_prices([clean])[0].get(clean)
            if quote:
                estimate = {
                    "price": quote["price"],
                    "notional": round(quote["price"] * qty, 2),
                    "as_of": quote["as_of"],
                    "source": quote["source"],
                }
        except Exception:  # a missing estimate must not block the proposal
            logger.warning("Could not estimate notional for %s", clean, exc_info=True)

        return {
            "proposal_id": trade["id"],
            "symbol": trade["symbol"],
            "side": trade["side"],
            "quantity": _float(trade["quantity"]),
            "order_type": trade["order_type"],
            "limit_price": _float(trade["limit_price"]),
            "trade_status": trade["status"],
            "estimate": estimate,
            "executed": False,
            "message": (
                f"Proposal #{trade['id']} is queued for approval. No order has "
                "been placed. The user must approve it in the app; they will "
                "then give you a confirmation key to pass to execute_trade."
            ),
            "_summary": f"proposed {side} {qty} {clean} (id {trade['id']})",
        }

    return _run(
        "propose_trade",
        {"symbol": symbol, "side": side, "quantity": quantity,
         "order_type": order_type, "limit_price": limit_price},
        work, ctx, symbol=(symbol or "").upper() or None,
    )


@mcp.tool
def execute_trade(
    proposal_id: int, confirmation_key: str, ctx: Context | None = None
) -> dict:
    """
    Submit an approved proposal to Alpaca, using the key the user gave you.

    Args:
        proposal_id: the id from propose_trade.
        confirmation_key: the key the user received when they clicked Accept.

    ONLY call this when the user has given you a confirmation key in this
    conversation. The key is single-use and expires, so if it is rejected do not
    retry or guess - ask them to approve the proposal again in the app. Never
    invent a key, and never tell the user a trade executed unless this tool
    returned success.

    This does not change their holdings or net worth report - it places the
    order. The next report they submit will reflect the new position.
    """

    def work():
        key = (confirmation_key or "").strip()
        if not key:
            raise ToolError(
                "A confirmation key is required. The user gets one by approving "
                "the proposal in the app."
            )
        result = trading.execute_confirmed(int(proposal_id), key)
        trade = result["trade"]
        return {
            "proposal_id": trade["id"],
            "symbol": trade["symbol"],
            "side": trade["side"],
            "quantity": _float(trade["quantity"]),
            "trade_status": trade["status"],
            "alpaca_order_id": trade["alpaca_order_id"],
            "fill_price": result["fill_price"],
            "executed": True,
            "warning": result["warning"],
            "note": result["note"],
            "_summary": (
                f"executed {trade['side']} {_float(trade['quantity'])} "
                f"{trade['symbol']} (order {trade['alpaca_order_id']})"
            ),
        }

    # The key is deliberately NOT recorded in the trace arguments - a trace row
    # is read by a dashboard and would otherwise persist a live credential.
    return _run("execute_trade", {"proposal_id": proposal_id}, work, ctx)


@mcp.tool
def list_pending_trades(
    status: str = "pending", limit: int = 20, ctx: Context | None = None
) -> dict:
    """
    Trade proposals and their outcomes.

    Args:
        status: "pending", "approved", "executing", "executed", "rejected",
            "failed", or "all" for everything.
        limit: how many to return.

    Use this to answer "did my trade go through?" - the answer is whatever this
    returns, never an assumption that an earlier proposal was approved. Note
    this never returns confirmation keys; only the user can give you one.
    """

    def work():
        wanted = None if status == "all" else status
        if wanted and wanted not in schema.TRADE_STATUSES:
            raise ToolError(
                f"status must be one of: {', '.join(schema.TRADE_STATUSES)}, or 'all'."
            )

        rows = schema.list_trades(wanted, _clamp(limit, 20, 1, 100))
        if not rows:
            raise NoData(
                f"No trades with status {status!r}."
                if wanted else "No trade proposals have ever been made."
            )

        return {
            "status_filter": status,
            "trades": [
                {
                    "proposal_id": row["id"],
                    "symbol": row["symbol"],
                    "side": row["side"],
                    "quantity": _float(row["quantity"]),
                    "order_type": row["order_type"],
                    "limit_price": _float(row["limit_price"]),
                    "trade_status": row["status"],
                    "rationale": row["rationale"],
                    "proposed_by": row["proposed_by"],
                    "filled_price": _float(row["filled_price"]),
                    "error_message": row["error_message"],
                    "awaiting_key": row["status"] == "approved" and not row["key_expired"],
                    "created_at": _iso(row["created_at"]),
                    "decided_at": _iso(row["decided_at"]),
                    "executed_at": _iso(row["executed_at"]),
                }
                for row in rows
            ],
            "_summary": f"{len(rows)} trades with status {status}",
        }

    return _run("list_pending_trades", {"status": status, "limit": limit}, work, ctx)


# ------------------------------------------------------------------- startup


def startup() -> None:
    """
    Prepare both databases before serving.

    Failures are logged, not fatal: an MCP server that refuses to start because
    the tracing database is unreachable would trade every answer for an audit
    row, which is exactly backwards.
    """
    try:
        schema.init_db()
    except Exception:
        logger.exception("Could not initialize the app schema; tools will report errors")

    if tracing.tracing_available():
        try:
            tracing.init_db()
        except Exception:
            logger.exception("Could not initialize the tracing schema; tracing disabled")


startup()


if __name__ == "__main__":
    port = int(os.getenv("DATABRICKS_APP_PORT", os.getenv("PORT", 8000)))
    logger.info(
        "Starting networth MCP server on port %s (alpaca=%s, massive=%s, paper=%s)",
        port, alpaca_api.is_configured(), massive_api.is_configured(), alpaca_api.is_paper(),
    )
    mcp.run(transport="http", host="0.0.0.0", port=port)
