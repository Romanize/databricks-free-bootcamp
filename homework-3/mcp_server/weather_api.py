"""
Weather API adapter for the homework-3 MCP server.

This module is the equivalent of alpaca_broker.py in the Day 3 lab example: it
owns *all* HTTP calls and response parsing, and hands back plain dictionaries.
The MCP tool functions in weather_mcp_server.py stay thin - they call in here,
they never touch `requests` themselves.

Data sources (both free, both keyless):
  * Open-Meteo  - geocoding + current conditions + daily forecast (worldwide)
      https://open-meteo.com/en/docs
  * NWS         - active severe-weather alerts (United States only)
      https://www.weather.gov/documentation/services-web-api

Because neither API needs credentials there is no secret to resolve here. Had
we picked a keyed provider (e.g. WeatherAPI.com), this is where a _secret()
helper reading WorkspaceClient().secrets.get_secret() would live, exactly as in
mcp_server/alpaca_broker.py - see README.md.
"""

import datetime as dt
import logging
import os
from typing import Any

import requests

logger = logging.getLogger("weather-api")

GEOCODE_URL = os.environ.get(
    "GEOCODE_API_URL", "https://geocoding-api.open-meteo.com/v1/search"
)
FORECAST_URL = os.environ.get(
    "OPEN_METEO_API_URL", "https://api.open-meteo.com/v1/forecast"
)
NWS_BASE_URL = os.environ.get("NWS_API_BASE_URL", "https://api.weather.gov")

# NWS asks every client to identify itself with a contact address. Open-Meteo
# does not require it, but sending it costs nothing.
USER_AGENT = os.environ.get(
    "WEATHER_USER_AGENT",
    "databricks-free-bootcamp-hw3 (robert.g.martinez.a@gmail.com)",
)

TIMEOUT = int(os.environ.get("WEATHER_HTTP_TIMEOUT", "20"))
MAX_FORECAST_DAYS = 16  # Open-Meteo's ceiling for the daily endpoint.

UNIT_PRESETS = {
    "imperial": {
        "temperature_unit": "fahrenheit",
        "wind_speed_unit": "mph",
        "precipitation_unit": "inch",
    },
    "metric": {
        "temperature_unit": "celsius",
        "wind_speed_unit": "kmh",
        "precipitation_unit": "mm",
    },
}
DEFAULT_UNITS = os.environ.get("WEATHER_DEFAULT_UNITS", "imperial")

# WMO weather interpretation codes -> human readable conditions.
# https://open-meteo.com/en/docs (see "Weather variable documentation")
WMO_CODES = {
    0: "Clear sky",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Depositing rime fog",
    51: "Light drizzle",
    53: "Moderate drizzle",
    55: "Dense drizzle",
    56: "Light freezing drizzle",
    57: "Dense freezing drizzle",
    61: "Slight rain",
    63: "Moderate rain",
    65: "Heavy rain",
    66: "Light freezing rain",
    67: "Heavy freezing rain",
    71: "Slight snowfall",
    73: "Moderate snowfall",
    75: "Heavy snowfall",
    77: "Snow grains",
    80: "Slight rain showers",
    81: "Moderate rain showers",
    82: "Violent rain showers",
    85: "Slight snow showers",
    86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm with slight hail",
    99: "Thunderstorm with heavy hail",
}

# Code groups the prediction logic in weather_mcp_server.py reasons about.
SNOW_CODES = {71, 73, 75, 77, 85, 86}
THUNDERSTORM_CODES = {95, 96, 99}
FREEZING_CODES = {56, 57, 66, 67}


# Open-Meteo's geocoder returns full region names ("Illinois"), so a US state
# abbreviation in the query ("Springfield, IL") has to be expanded before the
# region match below - otherwise "Springfield, IL" silently picks Missouri.
US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana",
    "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan",
    "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
    "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey",
    "NM": "New Mexico", "NY": "New York", "NC": "North Carolina",
    "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon",
    "PA": "Pennsylvania", "PR": "Puerto Rico", "RI": "Rhode Island",
    "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee",
    "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia",
    "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin",
    "WY": "Wyoming",
}

# The same trick for the country abbreviations people actually type.
COUNTRY_ALIASES = {
    "UK": "United Kingdom",
    "GB": "United Kingdom",
    "USA": "United States",
    "US": "United States",
    "UAE": "United Arab Emirates",
}

class WeatherAPIError(Exception):
    """Raised when a location cannot be resolved or an upstream API refuses a call."""


# ------------------------------------------------------------------ transport

_session = requests.Session()
_session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})


def _get(url: str, params: dict | None = None) -> Any:
    """GET a JSON document, turning every transport failure into WeatherAPIError."""
    try:
        resp = _session.get(url, params=params, timeout=TIMEOUT)
    except requests.RequestException as err:
        raise WeatherAPIError(f"Could not reach {url}: {err}") from err

    if resp.status_code >= 400:
        raise WeatherAPIError(
            f"Weather API request failed ({resp.status_code}) for {resp.url}: "
            f"{resp.text[:200]}"
        )
    try:
        return resp.json()
    except ValueError as err:
        raise WeatherAPIError(f"Weather API returned non-JSON from {resp.url}") from err


# ------------------------------------------------------------------ helpers


def _units(units: str | None) -> dict:
    """Map "imperial"/"metric" onto Open-Meteo's unit query parameters."""
    key = (units or DEFAULT_UNITS).strip().lower()
    if key not in UNIT_PRESETS:
        raise WeatherAPIError(
            f"Unknown units '{units}'. Use one of: {', '.join(UNIT_PRESETS)}."
        )
    return UNIT_PRESETS[key]


def describe_code(code: Any) -> str:
    """Turn a WMO weather code into a short phrase, e.g. 61 -> "Slight rain"."""
    try:
        return WMO_CODES.get(int(code), f"Unknown conditions (WMO code {code})")
    except (TypeError, ValueError):
        return "Unknown conditions"


def _coordinate_pair(location: str) -> tuple[float, float] | None:
    """Parse a raw "41.88,-87.63" string; return None if it is not one."""
    parts = location.replace(" ", "").split(",")
    if len(parts) != 2:
        return None
    try:
        lat, lon = float(parts[0]), float(parts[1])
    except ValueError:
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise WeatherAPIError(
            f"Coordinates out of range: {location}. Expected 'lat,lon' within "
            "[-90,90] and [-180,180]."
        )
    return lat, lon


def resolve_date(target: str | None, available: list[str]) -> str:
    """
    Resolve a caller-supplied date against the dates a forecast actually covers.

    Accepts None/""/"today", "tomorrow", or an ISO "YYYY-MM-DD" string. Dates are
    interpreted in the *location's* local timezone, which is what `available`
    holds (Open-Meteo is queried with timezone=auto).

    Raises WeatherAPIError when the requested date is outside the forecast range,
    so the agent can tell the user rather than silently answering about a
    different day.
    """
    if not available:
        raise WeatherAPIError("The forecast came back empty.")

    key = (target or "today").strip().lower()
    if key in ("", "today"):
        return available[0]
    if key == "tomorrow":
        if len(available) < 2:
            raise WeatherAPIError("Tomorrow is outside the fetched forecast range.")
        return available[1]

    try:
        requested = dt.date.fromisoformat(key).isoformat()
    except ValueError as err:
        raise WeatherAPIError(
            f"Could not read date '{target}'. Use 'today', 'tomorrow', or YYYY-MM-DD."
        ) from err

    if requested not in available:
        raise WeatherAPIError(
            f"{requested} is outside the available forecast "
            f"({available[0]} to {available[-1]})."
        )
    return requested


# ------------------------------------------------------------------ geocoding


def geocode(location: str) -> dict:
    """
    Resolve a free-text location to coordinates.

    Accepts a place name ("Chicago", "Austin, TX", "Paris, France") or a raw
    "lat,lon" pair, which is passed straight through without an API call.

    Returns a dict with name, latitude, longitude, timezone, country_code and a
    display `label`.
    """
    location = (location or "").strip()
    if not location:
        raise WeatherAPIError("Location must not be empty.")

    pair = _coordinate_pair(location)
    if pair:
        lat, lon = pair
        return {
            "name": f"{lat},{lon}",
            "label": f"{lat},{lon}",
            "latitude": lat,
            "longitude": lon,
            "timezone": None,
            "country_code": None,
            "admin1": None,
        }

    # Open-Meteo's geocoder matches on the city name only, so "Austin, TX" is
    # searched as "Austin" and the region is used to pick the right hit.
    name, _, region = location.partition(",")
    name, region = name.strip(), region.strip()

    payload = _get(
        GEOCODE_URL, {"name": name, "count": 10, "language": "en", "format": "json"}
    )
    results = payload.get("results") or []
    if not results:
        raise WeatherAPIError(
            f"Could not find a location named '{location}'. Try 'City, State' "
            "or a 'lat,lon' pair."
        )

    match = results[0]
    if region:
        # "IL" -> "illinois", "UK" -> "united kingdom", so both the abbreviation
        # and the spelled-out name hit the same branch.
        wanted = {
            region.lower(),
            US_STATES.get(region.upper(), region).lower(),
            COUNTRY_ALIASES.get(region.upper(), region).lower(),
        }
        for candidate in results:
            fields = [
                candidate.get("admin1") or "",
                candidate.get("country") or "",
                candidate.get("country_code") or "",
            ]
            if any(field.lower() in wanted for field in fields if field):
                match = candidate
                break
        else:
            raise WeatherAPIError(
                f"Found '{name}' but not in '{region}'. Closest matches: "
                + "; ".join(
                    f"{c.get('name')}, {c.get('admin1')}, {c.get('country_code')}"
                    for c in results[:3]
                )
            )

    label = ", ".join(
        part
        for part in (match.get("name"), match.get("admin1"), match.get("country_code"))
        if part
    )
    return {
        "name": match.get("name"),
        "label": label,
        "latitude": match["latitude"],
        "longitude": match["longitude"],
        "timezone": match.get("timezone"),
        "country_code": match.get("country_code"),
        "admin1": match.get("admin1"),
    }


# ------------------------------------------------------------------ Open-Meteo

CURRENT_FIELDS = [
    "temperature_2m",
    "apparent_temperature",
    "relative_humidity_2m",
    "precipitation",
    "weather_code",
    "wind_speed_10m",
    "wind_direction_10m",
    "is_day",
]

DAILY_FIELDS = [
    "weather_code",
    "temperature_2m_max",
    "temperature_2m_min",
    "precipitation_probability_max",
    "precipitation_sum",
    "wind_speed_10m_max",
    "sunrise",
    "sunset",
]


def get_current_conditions(location: str, units: str | None = None) -> dict:
    """
    Current observed conditions for a location.

    Returns a dict with the resolved location, temperature, feels_like, humidity,
    precipitation, conditions text, wind speed/direction and observation time.
    """
    place = geocode(location)
    unit_params = _units(units)

    payload = _get(
        FORECAST_URL,
        {
            "latitude": place["latitude"],
            "longitude": place["longitude"],
            "current": ",".join(CURRENT_FIELDS),
            "timezone": "auto",
            **unit_params,
        },
    )
    current = payload.get("current") or {}
    if not current:
        raise WeatherAPIError(f"No current conditions returned for {place['label']}.")

    units_out = payload.get("current_units") or {}
    return {
        "location": place["label"],
        "latitude": payload.get("latitude"),
        "longitude": payload.get("longitude"),
        "timezone": payload.get("timezone"),
        "observed_at": current.get("time"),
        "temperature": current.get("temperature_2m"),
        "feels_like": current.get("apparent_temperature"),
        "humidity_pct": current.get("relative_humidity_2m"),
        "precipitation": current.get("precipitation"),
        "wind_speed": current.get("wind_speed_10m"),
        "wind_direction_deg": current.get("wind_direction_10m"),
        "weather_code": current.get("weather_code"),
        "conditions": describe_code(current.get("weather_code")),
        "is_daytime": bool(current.get("is_day")),
        "units": {
            "temperature": units_out.get("temperature_2m"),
            "wind_speed": units_out.get("wind_speed_10m"),
            "precipitation": units_out.get("precipitation"),
        },
        "source": "open-meteo",
    }


def get_daily_forecast(location: str, days: int = 3, units: str | None = None) -> dict:
    """
    Multi-day forecast for a location.

    `days` is clamped to 1..16 (Open-Meteo's limit). Returns a dict with the
    resolved location and a `days` list, one entry per calendar day in the
    location's own timezone.
    """
    try:
        days = int(days)
    except (TypeError, ValueError):
        raise WeatherAPIError(f"'days' must be a whole number, got {days!r}.")
    days = max(1, min(MAX_FORECAST_DAYS, days))

    place = geocode(location)
    unit_params = _units(units)

    payload = _get(
        FORECAST_URL,
        {
            "latitude": place["latitude"],
            "longitude": place["longitude"],
            "daily": ",".join(DAILY_FIELDS),
            "forecast_days": days,
            "timezone": "auto",
            **unit_params,
        },
    )
    daily = payload.get("daily") or {}
    dates = daily.get("time") or []
    if not dates:
        raise WeatherAPIError(f"No forecast returned for {place['label']}.")

    units_out = payload.get("daily_units") or {}

    def column(name: str, index: int):
        values = daily.get(name) or []
        return values[index] if index < len(values) else None

    forecast_days = []
    for index, date in enumerate(dates):
        code = column("weather_code", index)
        forecast_days.append(
            {
                "date": date,
                "conditions": describe_code(code),
                "weather_code": code,
                "temp_high": column("temperature_2m_max", index),
                "temp_low": column("temperature_2m_min", index),
                "precipitation_chance_pct": column(
                    "precipitation_probability_max", index
                ),
                "precipitation_amount": column("precipitation_sum", index),
                "max_wind_speed": column("wind_speed_10m_max", index),
                "sunrise": column("sunrise", index),
                "sunset": column("sunset", index),
            }
        )

    return {
        "location": place["label"],
        "latitude": payload.get("latitude"),
        "longitude": payload.get("longitude"),
        "timezone": payload.get("timezone"),
        "days": forecast_days,
        "units": {
            "temperature": units_out.get("temperature_2m_max"),
            "wind_speed": units_out.get("wind_speed_10m_max"),
            "precipitation": units_out.get("precipitation_sum"),
        },
        "source": "open-meteo",
    }


# ------------------------------------------------------------------ NWS alerts


def get_active_alerts(location: str, limit: int = 10) -> dict:
    """
    Active NWS severe-weather alerts covering a point. United States only.

    Returns a dict with the resolved location and an `alerts` list (event,
    headline, severity, urgency, description, instruction, effective, expires).
    Outside the US the list simply comes back empty, with `supported` False.
    """
    place = geocode(location)
    limit = max(1, min(50, int(limit or 10)))

    country = (place.get("country_code") or "").upper()
    if country and country != "US":
        return {
            "location": place["label"],
            "supported": False,
            "alerts": [],
            "count": 0,
            "note": (
                "The National Weather Service only covers United States "
                f"locations; {place['label']} is in {country}."
            ),
            "source": "nws",
        }

    # NOTE: /alerts/active rejects a `limit` query parameter, so the response is
    # trimmed here instead.
    payload = _get(
        f"{NWS_BASE_URL}/alerts/active",
        {"point": f"{place['latitude']},{place['longitude']}"},
    )

    alerts = []
    for feature in (payload.get("features") or [])[:limit]:
        props = feature.get("properties") or {}
        alerts.append(
            {
                "id": props.get("id"),
                "event": props.get("event"),
                "headline": props.get("headline"),
                "severity": props.get("severity"),
                "urgency": props.get("urgency"),
                "certainty": props.get("certainty"),
                "area": props.get("areaDesc"),
                "description": props.get("description"),
                "instruction": props.get("instruction"),
                "effective": props.get("effective"),
                "expires": props.get("expires"),
            }
        )

    return {
        "location": place["label"],
        "supported": True,
        "count": len(alerts),
        "alerts": alerts,
        "source": "nws",
    }
