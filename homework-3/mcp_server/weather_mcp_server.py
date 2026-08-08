"""
Weather-prediction MCP server (homework 3).

Exposes weather tools over MCP (Model Context Protocol) so a Databricks Agent
Bricks agent can call them like any other tool:

    - get_current_weather(location, units)              current conditions
    - get_forecast(location, days, units)               multi-day forecast
    - predict_umbrella_needed(location, date)           derived judgement call
    - get_severe_weather_alerts(location)               NWS alerts (US only)
    - compare_locations_weather(locations, date, units) which city looks better

The tools are deliberately thin: every HTTP call and every bit of response
parsing lives in weather_api.py (the equivalent of the lab example's
alpaca_broker.py). What lives *here* is the derived reasoning - the umbrella
thresholds and the comfort score - which is pure functions over already-parsed
data, no network access.

Both upstream APIs (Open-Meteo, api.weather.gov) are keyless, so this server
needs no Databricks secret and no credentials of any kind. See README.md.

Deploy as its own Databricks App using the app.yaml next to this file - the same
FastMCP + streamable-HTTP entrypoint pattern documented at
https://docs.databricks.com/aws/en/agents/mcp-tools/custom-mcp - then register
its URL as an external MCP server for Agent Bricks.

Run locally:
    python weather_mcp_server.py
"""

import logging
import os
import time

from fastmcp import FastMCP

import schema
import weather_api
from weather_api import WeatherAPIError

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("weather-mcp-server")

mcp = FastMCP("weather-prediction")

MAX_COMPARE_LOCATIONS = 5

# --- Umbrella thresholds -----------------------------------------------------
# Tuned for imperial units, which is why predict_umbrella_needed always queries
# in Fahrenheit/inches/mph regardless of what the caller prefers elsewhere.
CHANCE_LIKELY_PCT = 60      # precipitation probability at or above this -> yes
CHANCE_POSSIBLE_PCT = 30    # ...and at or above this -> maybe
AMOUNT_LIKELY_IN = 0.10     # expected accumulation at or above this -> yes
AMOUNT_POSSIBLE_IN = 0.02   # ...and at or above this -> maybe
WIND_TOO_STRONG_MPH = 20    # above this an umbrella turns inside out

# --- Comfort score weights (compare_locations_weather) -----------------------
COMFORT_IDEAL_LOW_F = 60
COMFORT_IDEAL_HIGH_F = 80


# ---------------------------------------------------------------- helpers


def _error(message: str) -> dict:
    """Uniform error payload, so the agent never has to read a stack trace."""
    return {"status": "error", "message": message}


def _ok(payload: dict) -> dict:
    return {"status": "success", **payload}


def _summary(tool_name: str, result: dict) -> str | None:
    """One human-readable line describing the result, for the dashboard table."""
    if result["status"] == "error":
        return None
    if tool_name == "get_current_weather":
        return f"{result.get('conditions')}, {result.get('temperature')}"
    if tool_name == "get_forecast":
        days = result.get("days") or []
        return f"{len(days)} days ({days[0]['date']} to {days[-1]['date']})" if days else None
    if tool_name == "predict_umbrella_needed":
        return result.get("recommendation")
    if tool_name == "get_severe_weather_alerts":
        count = result.get("count", 0)
        if not result.get("supported", True):
            return "outside NWS coverage"
        return f"{count} active alert(s)" + (
            f": {result['alerts'][0].get('event')}" if count else ""
        )
    if tool_name == "compare_locations_weather":
        best = result.get("best") or {}
        return f"best: {best.get('location')} ({best.get('score')})"
    return None


def _logged_location(args: dict, result: dict) -> str | None:
    """Pick the most useful location string to file this call under."""
    resolved = result.get("location") or (result.get("best") or {}).get("location")
    if resolved:
        return resolved
    # On an error the resolved name does not exist, so log what was asked for -
    # a dashboard full of failed lookups is exactly what you want to see.
    if args.get("location"):
        return str(args["location"])
    if args.get("locations"):
        return ", ".join(str(item) for item in args["locations"])[:200]
    return None


def _run(tool_name: str, args: dict, work) -> dict:
    """
    Execute one tool's real work, then shape and log the outcome.

    Centralizing this is what keeps the @mcp.tool functions to a couple of
    lines each: every tool gets the same error contract (WeatherAPIError and
    anything unforeseen become a clean status "error"), the same timing, and
    the same audit row in Lakebase - without repeating try/except five times.

    `work` is a zero-argument callable returning the success payload.
    """
    started = time.perf_counter()
    try:
        result = _ok(work())
    except WeatherAPIError as err:
        result = _error(str(err))
    except Exception:
        logger.exception("%s failed with args %r", tool_name, args)
        result = _error(f"Unexpected failure in {tool_name}.")

    schema.record_call(
        tool_name=tool_name,
        arguments=args,
        status=result["status"],
        location=_logged_location(args, result),
        verdict=result.get("verdict"),
        summary=_summary(tool_name, result),
        error_message=result.get("message"),
        duration_ms=int((time.perf_counter() - started) * 1000),
    )
    return result


def _num(value, default: float = 0.0) -> float:
    """Coerce a possibly-missing forecast field to a float."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _umbrella_verdict(day: dict) -> dict:
    """
    Decide whether rain gear is worth carrying, from one day of forecast data.

    Pure function, no I/O. The rules, in order:

      1. Chance >= 60% or expected accumulation >= 0.10 in  -> "yes"
      2. Chance >= 30% or expected accumulation >= 0.02 in  -> "maybe"
      3. otherwise                                          -> "no"

    Then the recommendation is adjusted for *what kind* of weather it is, since
    "some precipitation" is not the same advice every time:

      * frozen precipitation (snow codes) -> an umbrella is the wrong tool;
        recommend a waterproof coat and boots instead.
      * thunderstorms (WMO 95/96/99)      -> upgrade to "yes"; advise a rain
        jacket and staying indoors during lightning rather than an umbrella.
      * max wind above 20 mph             -> a hand-held umbrella will invert;
        recommend a hooded rain jacket instead.

    Returns a dict with verdict, rain_gear_recommended, rain_gear,
    recommendation, confidence and the list of reasons behind the call.
    """
    chance = _num(day.get("precipitation_chance_pct"))
    amount = _num(day.get("precipitation_amount"))
    wind = _num(day.get("max_wind_speed"))
    code = day.get("weather_code")

    reasons = [
        f"{chance:.0f}% chance of precipitation "
        f"(yes at >= {CHANCE_LIKELY_PCT}%, maybe at >= {CHANCE_POSSIBLE_PCT}%).",
        f"{amount:.2f} in expected accumulation "
        f"(yes at >= {AMOUNT_LIKELY_IN} in, maybe at >= {AMOUNT_POSSIBLE_IN} in).",
    ]

    if chance >= CHANCE_LIKELY_PCT or amount >= AMOUNT_LIKELY_IN:
        verdict = "yes"
    elif chance >= CHANCE_POSSIBLE_PCT or amount >= AMOUNT_POSSIBLE_IN:
        verdict = "maybe"
    else:
        verdict = "no"

    wet = verdict in ("yes", "maybe")
    gear = "umbrella"

    if code in weather_api.SNOW_CODES:
        gear = "waterproof coat and boots"
        reasons.append(
            f"Precipitation falls as snow ({weather_api.describe_code(code)}), "
            "so an umbrella is not the right gear."
        )
    elif code in weather_api.THUNDERSTORM_CODES:
        verdict, wet = "yes", True
        gear = "rain jacket, and shelter during lightning"
        reasons.append(
            f"Thunderstorms are forecast ({weather_api.describe_code(code)}); "
            "a metal-framed umbrella is a bad idea in lightning."
        )
    elif wet and wind > WIND_TOO_STRONG_MPH:
        gear = "hooded rain jacket"
        reasons.append(
            f"Winds up to {wind:.0f} mph exceed the {WIND_TOO_STRONG_MPH} mph "
            "limit where a hand-held umbrella inverts."
        )

    if verdict == "yes":
        recommendation = f"Yes - take a {gear}."
    elif verdict == "maybe":
        recommendation = f"Possibly - a packable {gear} is worth having."
    else:
        recommendation = "No - precipitation is unlikely, leave the umbrella home."

    # Low confidence inside the grey band either side of a threshold, where a
    # small forecast revision would flip the answer.
    borderline = (
        abs(chance - CHANCE_LIKELY_PCT) <= 10 or abs(chance - CHANCE_POSSIBLE_PCT) <= 10
    )
    confidence = "medium" if borderline else "high"

    return {
        "verdict": verdict,
        # True for "yes" *and* "maybe" - it answers "carry something?", while
        # `rain_gear` answers "carry what?" and `verdict` carries the nuance.
        "rain_gear_recommended": wet,
        "rain_gear": gear,
        "recommendation": recommendation,
        "confidence": confidence,
        "reasons": reasons,
    }


def _comfort_score(day: dict) -> dict:
    """
    Score one forecast day from 0 (miserable) to 100 (ideal), no I/O.

    Starts at 100 and subtracts penalties: distance of the daytime high outside
    the 60-80 F comfort band (1.5 points per degree), precipitation probability
    (0.5 points per percent), and wind above 15 mph (1 point per mph). The
    weights are arbitrary but fixed, so the ranking between cities is consistent.
    """
    high = _num(day.get("temp_high"), COMFORT_IDEAL_LOW_F)
    chance = _num(day.get("precipitation_chance_pct"))
    wind = _num(day.get("max_wind_speed"))

    penalties = []
    score = 100.0

    if high < COMFORT_IDEAL_LOW_F:
        gap = COMFORT_IDEAL_LOW_F - high
        score -= gap * 1.5
        penalties.append(f"{gap:.0f} F colder than the {COMFORT_IDEAL_LOW_F} F comfort floor")
    elif high > COMFORT_IDEAL_HIGH_F:
        gap = high - COMFORT_IDEAL_HIGH_F
        score -= gap * 1.5
        penalties.append(f"{gap:.0f} F hotter than the {COMFORT_IDEAL_HIGH_F} F comfort ceiling")

    if chance:
        score -= chance * 0.5
        penalties.append(f"{chance:.0f}% chance of precipitation")
    if wind > 15:
        score -= wind - 15
        penalties.append(f"windy at {wind:.0f} mph")

    return {
        "score": round(max(0.0, min(100.0, score)), 1),
        "penalties": penalties or ["nothing notable working against it"],
    }


# ---------------------------------------------------------------- tools


@mcp.tool
def get_current_weather(location: str, units: str = "imperial") -> dict:
    """
    Get the current observed weather for a location.

    Args:
        location: Place name ("Chicago", "Austin, TX", "London, UK") or a raw
            "lat,lon" pair such as "41.88,-87.63".
        units: "imperial" for F/mph/inch (default) or "metric" for C/kmh/mm.

    Returns:
        A dict with status, the resolved location, observed_at, temperature,
        feels_like, humidity_pct, precipitation, wind_speed, wind_direction_deg,
        conditions text and the units used. On failure: status "error" plus a
        message explaining what went wrong.
    """
    return _run(
        "get_current_weather",
        {"location": location, "units": units},
        lambda: weather_api.get_current_conditions(location, units),
    )


@mcp.tool
def get_forecast(location: str, days: int = 3, units: str = "imperial") -> dict:
    """
    Get a multi-day forecast for a location.

    Args:
        location: Place name or "lat,lon" pair.
        days: How many days ahead to return, starting today. Clamped to 1-16.
        units: "imperial" for F/mph/inch (default) or "metric" for C/kmh/mm.

    Returns:
        A dict with status, the resolved location, and a `days` list where each
        entry has date, conditions, temp_high, temp_low,
        precipitation_chance_pct, precipitation_amount, max_wind_speed, sunrise
        and sunset. On failure: status "error" plus a message.
    """
    return _run(
        "get_forecast",
        {"location": location, "days": days, "units": units},
        lambda: weather_api.get_daily_forecast(location, days, units),
    )


@mcp.tool
def predict_umbrella_needed(location: str, date: str = "today") -> dict:
    """
    Decide whether someone should take rain gear, and explain why.

    This is a judgement call, not a passthrough of the forecast: it applies
    fixed thresholds to the precipitation probability and expected accumulation,
    then adjusts the advice for snow, thunderstorms and wind - because "60%
    chance of precipitation" means an umbrella in a drizzle, a rain jacket in a
    gale, and boots in a snowstorm.

    Rules: "yes" at >= 60% chance or >= 0.10 in accumulation; "maybe" at >= 30%
    or >= 0.02 in; otherwise "no". Thunderstorms force "yes" and recommend
    shelter over an umbrella; snow recommends a waterproof coat and boots; winds
    above 20 mph recommend a hooded rain jacket, since a hand-held umbrella
    inverts. Always evaluated in imperial units.

    Args:
        location: Place name or "lat,lon" pair.
        date: "today" (default), "tomorrow", or an ISO "YYYY-MM-DD" date within
            the next 16 days, interpreted in the location's own timezone.

    Returns:
        A dict with status, location, date, verdict ("yes"/"maybe"/"no"),
        rain_gear_recommended, rain_gear, recommendation, confidence, a `reasons`
        list naming the thresholds that drove the call, the `forecast` day it
        was based on, and the `thresholds` themselves. On failure: status
        "error" plus a message - including when the requested date falls outside
        the forecast window.
    """
    def work() -> dict:
        forecast = weather_api.get_daily_forecast(
            location, days=weather_api.MAX_FORECAST_DAYS, units="imperial"
        )
        dates = [day["date"] for day in forecast["days"]]
        target = weather_api.resolve_date(date, dates)
        day = forecast["days"][dates.index(target)]

        return {
            "location": forecast["location"],
            "date": target,
            **_umbrella_verdict(day),
            "forecast": day,
            "thresholds": {
                "chance_yes_pct": CHANCE_LIKELY_PCT,
                "chance_maybe_pct": CHANCE_POSSIBLE_PCT,
                "amount_yes_in": AMOUNT_LIKELY_IN,
                "amount_maybe_in": AMOUNT_POSSIBLE_IN,
                "wind_inverts_umbrella_mph": WIND_TOO_STRONG_MPH,
            },
            "units": forecast["units"],
            "source": forecast["source"],
        }

    return _run(
        "predict_umbrella_needed", {"location": location, "date": date}, work
    )


@mcp.tool
def get_severe_weather_alerts(location: str, limit: int = 10) -> dict:
    """
    Get active National Weather Service severe-weather alerts for a US location.

    Args:
        location: Place name or "lat,lon" pair. United States only - for any
            other country the call succeeds with an empty list and `supported`
            set to False.
        limit: Maximum number of alerts to return. Clamped to 1-50.

    Returns:
        A dict with status, location, supported, count and an `alerts` list
        where each entry has event, headline, severity, urgency, area,
        description, instruction, effective and expires. An empty list means no
        alerts are active right now, which is the normal case. On failure:
        status "error" plus a message.
    """
    return _run(
        "get_severe_weather_alerts",
        {"location": location, "limit": limit},
        lambda: weather_api.get_active_alerts(location, limit),
    )


@mcp.tool
def compare_locations_weather(
    locations: list[str], date: str = "today", units: str = "imperial"
) -> dict:
    """
    Compare the forecast across several locations for one day and rank them.

    Each location is scored 0-100 by a fixed comfort heuristic: a daytime high
    inside 60-80 F scores best, and precipitation probability and strong wind
    subtract from the score. Use it for "where should I go this weekend"
    questions. Scoring is always done in imperial units; `units` only controls
    how the numbers are reported back.

    Args:
        locations: Two to five place names or "lat,lon" pairs.
        date: "today" (default), "tomorrow", or an ISO "YYYY-MM-DD" date.
        units: "imperial" (default) or "metric", for the reported values.

    Returns:
        A dict with status, date, a `ranking` list ordered best-first (each with
        location, score, reasons, conditions, temp_high, temp_low,
        precipitation_chance_pct), the `best` entry, and an `errors` map for any
        location that could not be resolved. On failure: status "error".
    """
    def work() -> dict:
        if not locations or len(locations) < 2:
            raise WeatherAPIError("Provide at least two locations to compare.")
        if len(locations) > MAX_COMPARE_LOCATIONS:
            raise WeatherAPIError(
                f"Compare at most {MAX_COMPARE_LOCATIONS} locations at a time."
            )

        ranking, errors = [], {}
        for location in locations:
            try:
                forecast = weather_api.get_daily_forecast(
                    location, days=weather_api.MAX_FORECAST_DAYS, units=units
                )
                dates = [day["date"] for day in forecast["days"]]
                target = weather_api.resolve_date(date, dates)
                day = forecast["days"][dates.index(target)]
            except WeatherAPIError as err:
                # One unresolvable city should not sink the whole comparison.
                errors[location] = str(err)
                continue

            ranking.append(
                {
                    "location": forecast["location"],
                    "date": target,
                    **_comfort_score(day),
                    "conditions": day["conditions"],
                    "temp_high": day["temp_high"],
                    "temp_low": day["temp_low"],
                    "precipitation_chance_pct": day["precipitation_chance_pct"],
                    "max_wind_speed": day["max_wind_speed"],
                    "units": forecast["units"],
                }
            )

        if not ranking:
            raise WeatherAPIError(
                "None of the locations could be resolved: "
                + "; ".join(f"{loc}: {msg}" for loc, msg in errors.items())
            )

        ranking.sort(key=lambda row: row["score"], reverse=True)
        return {
            "date": ranking[0]["date"],
            "ranking": ranking,
            "best": ranking[0],
            "errors": errors,
            "source": "open-meteo",
        }

    return _run(
        "compare_locations_weather",
        {"locations": locations, "date": date, "units": units},
        work,
    )


def startup() -> None:
    """Create the tool-call table before serving, if logging is switched on."""
    if not schema.logging_available():
        logger.info("Tool-call logging is off; running without Lakebase.")
        return
    try:
        schema.init_db()
    except Exception:
        # Log and keep going: the weather tools do not need the database, so a
        # Lakebase problem must not stop this app from serving the agent.
        logger.exception("Could not initialize the Lakebase tool-call table")


if __name__ == "__main__":
    startup()

    # Databricks Apps routes external HTTP traffic to this port via app.yaml;
    # streamable-http is the transport the Databricks MCP client expects.
    port = int(os.getenv("DATABRICKS_APP_PORT", os.getenv("PORT", 8000)))
    logger.info("Starting weather MCP server on port %s", port)
    mcp.run(transport="http", host="0.0.0.0", port=port)
