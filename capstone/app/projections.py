"""
Investment-plan projection maths.

Pure functions, no database and no HTTP, so the Flask chart endpoint and the MCP
`get_investment_plan_projection` tool produce identical numbers from identical
inputs. That matters more than it looks: the agent quoting a different
retirement number than the chart on the page would be a credibility hole.

## The model

Monthly compounding from the current net worth:

    r          = (1 + expected_annual_rate) ** (1/12) - 1
    value(t+1) = value(t) * (1 + r) + monthly_contribution
                 + annual_contribution   (once every twelfth month)

Two series come back:

  * **nominal** - the account balance you would actually see.
  * **real**    - the same balance in today's money,
                  nominal / (1 + expected_inflation) ** (years elapsed).

Both are reported because they answer different questions, and quoting only the
nominal one is the single most common way a projection like this misleads: at 3%
inflation over 30 years, a "$1M" balance buys about $410k of today's groceries.

## What this is not

A deterministic compounding curve, not a simulation. Real returns are not a
constant monthly rate, and sequence-of-returns risk is invisible here. The
system prompt makes the agent say so rather than presenting the number as a
forecast.
"""

import datetime as dt

# Guard rails on the inputs. A 500% expected return or a 200-year horizon is a
# typo, and a projection built on one is worse than no projection.
MAX_YEARS = 80
MAX_RATE = 1.0  # 100% annual
MIN_RATE = -0.5


class ProjectionError(ValueError):
    """Raised when plan inputs cannot produce a meaningful projection."""


def _as_float(value, default: float = 0.0) -> float:
    """Plans come back from Postgres as Decimal; charts want float."""
    if value is None:
        return default
    return float(value)


def validate_plan(plan: dict) -> None:
    rate = _as_float(plan.get("expected_annual_rate"))
    years = int(plan.get("years") or 0)
    inflation = _as_float(plan.get("expected_inflation"))

    if not 0 < years <= MAX_YEARS:
        raise ProjectionError(f"years must be between 1 and {MAX_YEARS}, got {years}.")
    if not MIN_RATE <= rate <= MAX_RATE:
        raise ProjectionError(
            f"expected_annual_rate must be between {MIN_RATE} and {MAX_RATE} "
            f"(as a decimal, so 0.07 for 7%), got {rate}."
        )
    if not MIN_RATE <= inflation <= MAX_RATE:
        raise ProjectionError(
            f"expected_inflation must be between {MIN_RATE} and {MAX_RATE}, got {inflation}."
        )


def project(plan: dict, starting_value: float, points: str = "yearly") -> dict:
    """
    Run the projection for one plan from a starting net worth.

    `points` is "yearly" (one row per year, what the chart plots) or "monthly"
    (every step, for anyone who wants the detail).
    """
    validate_plan(plan)

    annual_rate = _as_float(plan["expected_annual_rate"])
    inflation = _as_float(plan.get("expected_inflation"))
    years = int(plan["years"])
    monthly_contribution = _as_float(plan.get("monthly_contribution"))
    annual_contribution = _as_float(plan.get("annual_contribution"))
    goal = _as_float(plan.get("goal_amount"))

    monthly_rate = (1 + annual_rate) ** (1 / 12) - 1
    months = years * 12
    today = dt.date.today()

    value = float(starting_value)
    contributed = 0.0
    series = []
    goal_month_nominal = None
    goal_month_real = None

    for month in range(months + 1):
        if month > 0:
            value = value * (1 + monthly_rate) + monthly_contribution
            contributed += monthly_contribution
            if month % 12 == 0 and annual_contribution:
                value += annual_contribution
                contributed += annual_contribution

        elapsed_years = month / 12
        real = value / ((1 + inflation) ** elapsed_years) if inflation else value

        if goal and goal_month_nominal is None and value >= goal:
            goal_month_nominal = month
        if goal and goal_month_real is None and real >= goal:
            goal_month_real = month

        if points == "monthly" or month % 12 == 0:
            series.append(
                {
                    "month": month,
                    "year": round(elapsed_years, 2),
                    # Approximate: months are 30.44 days on average. The chart
                    # labels years, so day-level drift does not matter.
                    "date": (today + dt.timedelta(days=round(month * 30.44))).isoformat(),
                    "nominal": round(value, 2),
                    "real": round(real, 2),
                    "contributed": round(contributed, 2),
                }
            )

    final = series[-1]
    return {
        "plan_name": plan.get("name"),
        "starting_value": round(float(starting_value), 2),
        "years": years,
        "expected_annual_rate": annual_rate,
        "expected_inflation": inflation,
        "monthly_contribution": monthly_contribution,
        "annual_contribution": annual_contribution,
        "goal_amount": goal or None,
        "final_nominal": final["nominal"],
        "final_real": final["real"],
        "total_contributed": final["contributed"],
        "growth": round(final["nominal"] - float(starting_value) - final["contributed"], 2),
        "goal_reached": _goal_summary(goal, goal_month_nominal, goal_month_real, months),
        "series": series,
        "basis": (
            "Deterministic monthly compounding. 'real' is discounted to today's "
            "money at the plan's inflation rate. Not a forecast - actual returns "
            "vary year to year and the order they arrive in matters."
        ),
    }


def _goal_summary(goal: float, nominal_month, real_month, horizon_months: int) -> dict:
    """Whether and when the goal is hit, in nominal and in today's money."""
    if not goal:
        return {"goal_set": False}

    return {
        "goal_set": True,
        "goal_amount": goal,
        "reached_nominal": nominal_month is not None,
        "reached_nominal_in_months": nominal_month,
        "reached_nominal_in_years": round(nominal_month / 12, 1) if nominal_month else None,
        "reached_real": real_month is not None,
        "reached_real_in_months": real_month,
        "reached_real_in_years": round(real_month / 12, 1) if real_month else None,
        "horizon_months": horizon_months,
        "note": (
            "Reached in nominal terms but not in today's money - inflation eats "
            "the difference."
            if nominal_month is not None and real_month is None
            else None
        ),
    }
