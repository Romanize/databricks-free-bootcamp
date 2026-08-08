"""
Client for the National Weather Service API (api.weather.gov).

The NWS API needs no API key - it only asks for a descriptive User-Agent - so
unlike massive_client.py in the lab example there is no secret to resolve here.

Two document flavours are harvested, both free text:
  * alerts    - GET /alerts/active?area={ST}      -> description + instruction
  * forecasts - GET /gridpoints/{o}/{x},{y}/forecast -> detailedForecast

City names are turned into lat/lon with Open-Meteo's free geocoding API, since
NWS itself only accepts coordinates. A "lat,lon" string skips that lookup.
"""

import hashlib
import os
from typing import Any

import requests

NWS_BASE_URL = os.environ.get("NWS_API_BASE_URL", "https://api.weather.gov")
GEOCODE_URL = os.environ.get(
    "GEOCODE_API_URL", "https://geocoding-api.open-meteo.com/v1/search"
)

# NWS asks every client to identify itself with a contact address.
USER_AGENT = os.environ.get(
    "WEATHER_USER_AGENT",
    "databricks-free-bootcamp-hw2 (robert.g.martinez.a@gmail.com)",
)

_DEFAULT_TIMEOUT = 30


class WeatherClientError(Exception):
    """Raised when a location cannot be resolved or the NWS API refuses a call."""


def _stable_id(*parts: str) -> str:
    """Short deterministic id, used when the source has no id of its own."""
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:32]


def _text(value: Any) -> str:
    return (value or "").strip() if isinstance(value, str) else ""


class WeatherClient:
    """Thin wrapper around api.weather.gov that returns normalized documents."""

    def __init__(self, base_url: str | None = None, timeout: int = _DEFAULT_TIMEOUT):
        self.base_url = (base_url or NWS_BASE_URL).rstrip("/")
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers.update(
            {"User-Agent": USER_AGENT, "Accept": "application/geo+json"}
        )
        self._points_cache: dict[str, dict] = {}

    # ------------------------------------------------------------- transport

    def _get(self, url: str, params: dict | None = None) -> Any:
        if url.startswith("/"):
            url = f"{self.base_url}{url}"
        resp = self._session.get(url, params=params, timeout=self.timeout)
        if resp.status_code >= 400:
            raise WeatherClientError(
                f"NWS request failed ({resp.status_code}) for {resp.url}: {resp.text[:200]}"
            )
        return resp.json()

    # ------------------------------------------------------------- locations

    def geocode(self, location: str) -> tuple[float, float, str]:
        """Resolve "Chicago, IL" (or a raw "41.88,-87.63") to lat/lon + label."""
        location = location.strip()
        if not location:
            raise WeatherClientError("Location must not be empty.")

        # A raw coordinate pair is used as-is, no geocoding call needed.
        parts = location.split(",")
        if len(parts) == 2:
            try:
                lat, lon = float(parts[0]), float(parts[1])
                return lat, lon, f"{lat:.4f},{lon:.4f}"
            except ValueError:
                pass  # not coordinates - fall through to the name lookup

        name = parts[0].strip()
        state = parts[1].strip() if len(parts) > 1 else ""
        data = requests.get(
            GEOCODE_URL,
            params={"name": name, "count": 10, "language": "en", "format": "json"},
            headers={"User-Agent": USER_AGENT},
            timeout=self.timeout,
        ).json()

        matches = [r for r in (data.get("results") or []) if r.get("country_code") == "US"]
        if state:
            # admin1 is the full state name ("Illinois"); accept either form.
            wanted = state.lower()
            matches = [
                r
                for r in matches
                if wanted in _text(r.get("admin1")).lower()
                or wanted == _text(r.get("admin1_id")).lower()
            ] or matches
        if not matches:
            raise WeatherClientError(f"Could not geocode location: {location!r}")

        best = matches[0]
        return float(best["latitude"]), float(best["longitude"]), location

    def resolve_point(self, lat: float, lon: float) -> dict:
        """GET /points/{lat},{lon} -> the NWS grid + city/state for a coordinate."""
        key = f"{lat:.4f},{lon:.4f}"
        if key in self._points_cache:
            return self._points_cache[key]

        props = self._get(f"/points/{key}")["properties"]
        relative = (props.get("relativeLocation") or {}).get("properties") or {}
        point = {
            "grid_id": props["gridId"],
            "grid_x": props["gridX"],
            "grid_y": props["gridY"],
            "forecast_url": props["forecast"],
            "city": _text(relative.get("city")),
            "state": _text(relative.get("state")),
            "lat": lat,
            "lon": lon,
        }
        self._points_cache[key] = point
        return point

    # ------------------------------------------------------------- harvesting

    def fetch_alerts(self, state: str) -> list[dict]:
        """Active alerts for a state, newest first (as returned by the API)."""
        if not state:
            return []
        data = self._get("/alerts/active", params={"area": state})
        return data.get("features") or []

    def fetch_forecast(self, point: dict) -> tuple[list[dict], str]:
        """Multi-day narrative forecast periods for a grid point."""
        data = self._get(point["forecast_url"])
        props = data.get("properties") or {}
        periods = props.get("periods") or []
        generated_at = _text(props.get("generatedAt")) or _text(props.get("updated"))
        return periods, generated_at

    # ------------------------------------------------------------ normalizing

    @staticmethod
    def normalize_alert(feature: dict, location: str) -> dict | None:
        """Turn one /alerts/active feature into a weather_documents row."""
        props = feature.get("properties") or {}
        description = _text(props.get("description"))
        instruction = _text(props.get("instruction"))
        if not description and not instruction:
            return None  # nothing to embed

        event = _text(props.get("event")) or "Weather Alert"
        area = _text(props.get("areaDesc"))

        # The event name and affected areas are prepended so the embedded text
        # carries the "what" and "where" a user is likely to search for, not
        # just the body copy.
        body = [f"{event} for {area}." if area else f"{event}.", description]
        if instruction:
            body.append(f"Instructions: {instruction}")

        return {
            "id": _text(props.get("id")) or _stable_id("alert", event, area, description),
            "location": location,
            "source_type": "alert",
            "headline": _text(props.get("headline")) or event,
            "event": event,
            "narrative_text": "\n\n".join(part for part in body if part),
            "issued_at": _text(props.get("sent")) or _text(props.get("effective")) or None,
            "effective_at": _text(props.get("effective")) or _text(props.get("onset")) or None,
            "payload": props,
        }

    @staticmethod
    def normalize_forecast_period(
        period: dict, point: dict, location: str, generated_at: str
    ) -> dict | None:
        """Turn one forecast period into a weather_documents row."""
        detailed = _text(period.get("detailedForecast"))
        if not detailed:
            return None

        name = _text(period.get("name")) or f"Period {period.get('number')}"
        short = _text(period.get("shortForecast"))
        start = _text(period.get("startTime")) or None

        # Stable across re-syncs: the grid point plus the period's start time.
        doc_id = "forecast:{}:{},{}:{}".format(
            point["grid_id"], point["grid_x"], point["grid_y"], start or name
        )

        return {
            "id": doc_id,
            "location": location,
            "source_type": "forecast",
            "headline": f"{name}: {short}" if short else name,
            "event": short or "Forecast",
            "narrative_text": f"{location} forecast for {name}: {detailed}",
            "issued_at": generated_at or None,
            "effective_at": start,
            "payload": {**period, "grid": {k: point[k] for k in ("grid_id", "grid_x", "grid_y")}},
        }

    # ------------------------------------------------------------------ entry

    def fetch_documents(self, location: str, limit: int = 50) -> list[dict]:
        """
        All documents for one location: active alerts for its state plus the
        narrative forecast for its grid point.

        `limit` caps the documents returned per location. Alerts get first call
        on half the budget and forecasts fill the rest, so a state with a busy
        alert feed never crowds the forecast out entirely (and vice versa).
        """
        lat, lon, label = self.geocode(location)
        point = self.resolve_point(lat, lon)
        # Prefer the city/state NWS itself reports, so "Chicago, IL" and
        # "41.88,-87.63" end up stored under the same location label.
        if point["city"] and point["state"]:
            label = f"{point['city']}, {point['state']}"

        alerts, forecasts = [], []
        for feature in self.fetch_alerts(point["state"]):
            doc = self.normalize_alert(feature, label)
            if doc:
                alerts.append(doc)

        periods, generated_at = self.fetch_forecast(point)
        for period in periods:
            doc = self.normalize_forecast_period(period, point, label, generated_at)
            if doc:
                forecasts.append(doc)

        half = max(1, limit // 2)
        picked = alerts[:half] + forecasts[: limit - min(len(alerts), half)]
        return picked[:limit]
