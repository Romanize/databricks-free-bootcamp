"""
Adapter for the Massive market-data API (https://massive.com).

Every HTTP call and all parsing lives here; the MCP tools, the Flask app and the
news notebook stay thin. Same split as homework 3's weather_api.py.

Two endpoints are used:

  GET /v2/reference/news            articles + per-ticker sentiment
  GET /v2/aggs/ticker/{sym}/prev    previous trading day's close

## The free tier shapes this file

Massive's free "Stocks Basic" plan allows **5 API calls per minute** and serves
**end-of-day data**. Both facts have consequences that are handled here rather
than being left for a caller to rediscover:

  * A module-level rate limiter serializes every request and sleeps so calls are
    at least MASSIVE_MIN_INTERVAL seconds apart (12.5s by default = 4.8/min,
    just under the limit). The 2-hour news job walking 20 tickers therefore
    takes ~4 minutes, which is fine for a background job and is why the news
    pipeline is a job rather than something the app does inline on a click.
  * `previous_close()` is named for what it returns. It is *not* a live quote,
    and it carries `as_of` so a report can state which session it priced from.
    Alpaca is the preferred price source (see alpaca_api.py); this is the
    fallback for when Alpaca is not configured.

## Sentiment

Massive returns an `insights` array per article, already attributed per ticker
with a `sentiment` and a `sentiment_reasoning` string. That is stored verbatim.
Nothing here computes sentiment, so every score in `ticker_sentiments` traces
back to a specific article rather than to a model's opinion of one.
"""

import logging
import os
import threading
import time

import requests

import config

logger = logging.getLogger(__name__)

BASE_URL = os.environ.get("MASSIVE_BASE_URL", "https://api.massive.com")
TIMEOUT = float(os.environ.get("MASSIVE_HTTP_TIMEOUT", 20))

# 5 requests/minute on the free tier. 12.5s between calls is 4.8/min - close
# enough to use the quota, far enough to never trip it.
MIN_INTERVAL = float(os.environ.get("MASSIVE_MIN_INTERVAL", 12.5))

# The Massive lineage accepts either an Authorization: Bearer header or an
# apiKey query parameter. The header is the default because it keeps the key out
# of URLs, proxy logs and tracebacks. Set MASSIVE_AUTH_STYLE=query to switch.
AUTH_STYLE = os.environ.get("MASSIVE_AUTH_STYLE", "header").lower()

VALID_SENTIMENTS = {"positive", "neutral", "negative"}

_rate_lock = threading.Lock()
_last_request_at = 0.0


class MassiveAPIError(Exception):
    """Any failure talking to Massive: transport, HTTP status, or bad body."""


def api_key() -> str | None:
    """
    The API key, or None when it has not been configured.

    Returning None rather than raising lets callers degrade politely - the app
    shows "news pipeline not configured" instead of 500-ing, and the MCP news
    tools answer `no_data` instead of erroring.
    """
    return config.resolve("MASSIVE_API_KEY", "massive-api-key")


def is_configured() -> bool:
    return bool(api_key())


def _throttle() -> None:
    """Block until the free tier's rate limit allows another request."""
    global _last_request_at
    with _rate_lock:
        wait = MIN_INTERVAL - (time.monotonic() - _last_request_at)
        if wait > 0:
            logger.debug("Massive rate limit: sleeping %.1fs", wait)
            time.sleep(wait)
        _last_request_at = time.monotonic()


def _get(path: str, params: dict | None = None, retries: int = 2) -> dict:
    """One rate-limited GET, with a bounded retry on 429 and 5xx."""
    key = api_key()
    if not key:
        raise MassiveAPIError(
            "MASSIVE_API_KEY is not set. Add it to the capstone secret scope "
            "with setup_secrets.py, or run with the news pipeline disabled."
        )

    params = dict(params or {})
    headers = {"Accept": "application/json"}
    if AUTH_STYLE == "query":
        params["apiKey"] = key
    else:
        headers["Authorization"] = f"Bearer {key}"

    url = f"{BASE_URL}{path}"
    for attempt in range(retries + 1):
        _throttle()
        try:
            response = requests.get(url, params=params, headers=headers, timeout=TIMEOUT)
        except requests.Timeout as err:
            raise MassiveAPIError(f"Massive timed out after {TIMEOUT}s") from err
        except requests.RequestException as err:
            raise MassiveAPIError(f"Could not reach Massive: {err}") from err

        if response.status_code == 401:
            raise MassiveAPIError("Massive rejected the API key (401). Check the stored secret.")
        if response.status_code == 429 or response.status_code >= 500:
            if attempt < retries:
                # The limiter already paces us; a 429 means something else used
                # the quota, so back off well past one interval.
                backoff = MIN_INTERVAL * (attempt + 1)
                logger.warning(
                    "Massive returned %s, retrying in %.0fs", response.status_code, backoff
                )
                time.sleep(backoff)
                continue
            raise MassiveAPIError(
                f"Massive returned {response.status_code} after {retries + 1} attempts. "
                "The free tier allows 5 requests per minute."
            )
        if not response.ok:
            raise MassiveAPIError(f"Massive returned {response.status_code}: {response.text[:200]}")

        try:
            return response.json()
        except ValueError as err:
            raise MassiveAPIError("Massive returned a non-JSON body") from err

    raise MassiveAPIError("Massive request failed")  # unreachable, keeps type checkers happy


# ----------------------------------------------------------------------- news


def _normalize_article(raw: dict) -> dict | None:
    """
    Turn one Massive article into a ticker_news row.

    Returns None for an article with no id or no title - there is nothing
    useful to embed and nothing stable to dedup on.
    """
    article_id = raw.get("id")
    title = (raw.get("title") or "").strip()
    if not article_id or not title:
        return None

    description = (raw.get("description") or "").strip()
    publisher = raw.get("publisher") or {}

    return {
        "id": article_id,
        "title": title,
        "description": description or None,
        # What actually gets chunked and embedded. Title first: it is the
        # densest sentence in a news item, so it survives every chunk boundary.
        "embed_text": f"{title}\n\n{description}".strip(),
        "article_url": raw.get("article_url"),
        "publisher": publisher.get("name"),
        "author": raw.get("author"),
        "tickers": [t.upper() for t in (raw.get("tickers") or []) if t],
        "keywords": raw.get("keywords") or [],
        "published_utc": raw.get("published_utc"),
        "payload": raw,
    }


def _normalize_insights(raw: dict) -> list[dict]:
    """Pull the per-ticker sentiment rows out of one article's `insights`."""
    rows = []
    for insight in raw.get("insights") or []:
        symbol = (insight.get("ticker") or "").upper()
        sentiment = (insight.get("sentiment") or "").lower()
        # Massive documents positive/neutral/negative; anything else would
        # violate the CHECK constraint, so drop it rather than fail the batch.
        if not symbol or sentiment not in VALID_SENTIMENTS:
            continue
        rows.append(
            {
                "symbol": symbol,
                "article_id": raw.get("id"),
                "sentiment": sentiment,
                "sentiment_reasoning": insight.get("sentiment_reasoning"),
                "published_utc": raw.get("published_utc"),
            }
        )
    return rows


def fetch_news(
    symbol: str, limit: int = 20, published_after: str | None = None
) -> tuple[list[dict], list[dict]]:
    """
    Fetch recent articles for one ticker.

    `published_after` is an ISO date or datetime; passing the newest
    `published_utc` already stored makes the 2-hour job incremental, so each run
    pulls only what appeared since the last one.

    Returns (articles, sentiments) ready for schema.upsert_articles() and
    schema.upsert_sentiments().
    """
    params = {
        "ticker": symbol.upper(),
        "limit": max(1, min(int(limit), 1000)),
        "sort": "published_utc",
        "order": "desc",
    }
    if published_after:
        params["published_utc.gt"] = published_after

    body = _get("/v2/reference/news", params)
    results = body.get("results") or []

    articles, sentiments = [], []
    for raw in results:
        article = _normalize_article(raw)
        if not article:
            continue
        articles.append(article)
        sentiments.extend(_normalize_insights(raw))

    logger.info(
        "Massive news %s: %s articles, %s sentiment rows", symbol, len(articles), len(sentiments)
    )
    return articles, sentiments


# --------------------------------------------------------------------- prices


def previous_close(symbol: str) -> dict:
    """
    The previous trading day's close for one ticker.

    Returns {"symbol", "price", "as_of", "source"}. `as_of` is the close of the
    bar, not the moment of the call, so a report that used this price can say
    which session it came from.
    """
    body = _get(f"/v2/aggs/ticker/{symbol.upper()}/prev", {"adjusted": "true"})
    results = body.get("results") or []
    if not results:
        raise MassiveAPIError(
            f"Massive returned no previous close for {symbol.upper()}. "
            "Check the symbol, or the market may not have opened yet on the free tier."
        )

    bar = results[0]
    close = bar.get("c")
    if close is None:
        raise MassiveAPIError(f"Massive returned a bar with no close price for {symbol.upper()}")

    timestamp = bar.get("t")
    as_of = None
    if timestamp:
        as_of = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(timestamp / 1000))

    return {
        "symbol": symbol.upper(),
        "price": float(close),
        "as_of": as_of,
        "source": "massive:prev_close",
    }


def previous_closes(symbols: list[str]) -> tuple[dict, dict]:
    """
    Previous close for several tickers.

    One request per symbol - Massive has no batch previous-close endpoint - so
    at 12.5s apart this is slow by construction. Returns (prices, errors) and
    never raises: one bad ticker must not cost you a whole valuation run.
    """
    prices, errors = {}, {}
    for symbol in symbols:
        try:
            prices[symbol.upper()] = previous_close(symbol)
        except MassiveAPIError as err:
            errors[symbol.upper()] = str(err)
    return prices, errors
