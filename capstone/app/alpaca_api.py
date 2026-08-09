"""
Adapter for Alpaca: account, positions, live prices, and paper order submission.

Every HTTP call and all parsing lives here, same split as massive_api.py and as
homework 3's weather_api.py.

## Why Alpaca is the price source and Massive is the fallback

Massive's free tier serves end-of-day data, so valuing a portfolio from it means
every report says "as of yesterday's close". Alpaca's free IEX feed is
real-time, comes with the paper account we already need for trading, and prices
a whole portfolio in **one** batched request instead of one call per symbol.
`price_map()` therefore tries Alpaca first and lets the caller fall back.

## Trading safety

`submit_order()` is the only function here that moves money. It is called from
exactly one place - `trading.execute_confirmed()` - which cannot reach it
without a single-use confirmation key that only comes into existence when a
human clicks Accept in the app. See trading.py for the full argument.

ALPACA_BASE_URL defaults to the **paper** endpoint. Pointing it at
api.alpaca.markets is what makes orders real, and nothing in this repo does that.

This file is duplicated in app/ - each Databricks App deploys from its own
folder. Keep the copies in sync.
"""

import datetime as dt
import logging
import os

import requests

import config

logger = logging.getLogger(__name__)

# Paper trading by default. This is the single line that separates play money
# from real money, which is why it is not buried in a config file.
BASE_URL = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
DATA_URL = os.environ.get("ALPACA_DATA_URL", "https://data.alpaca.markets")
TIMEOUT = float(os.environ.get("ALPACA_HTTP_TIMEOUT", 20))

# Alpaca crypto pairs are written BTC/USD; equities are bare symbols. The slash
# is how this module tells them apart, since they use different price endpoints.
CRYPTO_MARKER = "/"


class AlpacaAPIError(Exception):
    """Any failure talking to Alpaca: transport, HTTP status, or bad body."""


def credentials() -> tuple[str | None, str | None]:
    return (
        config.resolve("ALPACA_API_KEY_ID", "alpaca-api-key-id"),
        config.resolve("ALPACA_SECRET_KEY", "alpaca-secret-key"),
    )


def is_configured() -> bool:
    key, secret = credentials()
    return bool(key and secret)


def is_paper() -> bool:
    """True when pointed at the paper endpoint - surfaced in the UI and to the agent."""
    return "paper-api" in BASE_URL


def _headers() -> dict:
    key, secret = credentials()
    if not (key and secret):
        raise AlpacaAPIError(
            "Alpaca credentials are not set. Add ALPACA_API_KEY_ID and "
            "ALPACA_SECRET_KEY to the capstone secret scope with setup_secrets.py."
        )
    return {
        "APCA-API-KEY-ID": key,
        "APCA-API-SECRET-KEY": secret,
        "Accept": "application/json",
    }


def _request(method: str, url: str, **kwargs) -> dict:
    try:
        response = requests.request(
            method, url, headers=_headers(), timeout=TIMEOUT, **kwargs
        )
    except requests.Timeout as err:
        raise AlpacaAPIError(f"Alpaca timed out after {TIMEOUT}s") from err
    except requests.RequestException as err:
        raise AlpacaAPIError(f"Could not reach Alpaca: {err}") from err

    if response.status_code in (401, 403):
        raise AlpacaAPIError(
            "Alpaca rejected the credentials. Check the stored keys, and that "
            f"they are {'paper' if is_paper() else 'live'} keys for {BASE_URL}."
        )
    if not response.ok:
        # Alpaca puts a useful reason in the body on 4xx (insufficient buying
        # power, market closed, unknown symbol); surface it rather than a bare code.
        detail = response.text[:300]
        raise AlpacaAPIError(f"Alpaca returned {response.status_code}: {detail}")

    if not response.content:
        return {}
    try:
        return response.json()
    except ValueError as err:
        raise AlpacaAPIError("Alpaca returned a non-JSON body") from err


# -------------------------------------------------------------------- account


def get_account() -> dict:
    """Cash, equity and buying power for the configured account."""
    raw = _request("GET", f"{BASE_URL}/v2/account")
    return {
        "account_number": raw.get("account_number"),
        "status": raw.get("status"),
        "currency": raw.get("currency"),
        "cash": _as_float(raw.get("cash")),
        "equity": _as_float(raw.get("equity")),
        "buying_power": _as_float(raw.get("buying_power")),
        "portfolio_value": _as_float(raw.get("portfolio_value")),
        "paper": is_paper(),
    }


def get_positions() -> list[dict]:
    """Open positions, as Alpaca sees them."""
    raw = _request("GET", f"{BASE_URL}/v2/positions")
    if not isinstance(raw, list):
        return []
    return [
        {
            "symbol": item.get("symbol"),
            "quantity": _as_float(item.get("qty")),
            "avg_entry_price": _as_float(item.get("avg_entry_price")),
            "current_price": _as_float(item.get("current_price")),
            "market_value": _as_float(item.get("market_value")),
            "unrealized_pl": _as_float(item.get("unrealized_pl")),
            "unrealized_plpc": _as_float(item.get("unrealized_plpc")),
        }
        for item in raw
    ]


def _as_float(value):
    """Alpaca returns numbers as strings; None stays None."""
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------- prices


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def latest_equity_prices(symbols: list[str]) -> dict:
    """Latest trade price for a batch of equities - one request for all of them."""
    if not symbols:
        return {}

    body = _request(
        "GET",
        f"{DATA_URL}/v2/stocks/trades/latest",
        params={"symbols": ",".join(sorted({s.upper() for s in symbols})), "feed": "iex"},
    )
    prices = {}
    for symbol, trade in (body.get("trades") or {}).items():
        price = _as_float(trade.get("p"))
        if price is None:
            continue
        prices[symbol.upper()] = {
            "symbol": symbol.upper(),
            "price": price,
            "as_of": trade.get("t") or _now_iso(),
            "source": "alpaca:iex_trade",
        }
    return prices


def latest_crypto_prices(symbols: list[str]) -> dict:
    """Latest trade price for a batch of crypto pairs (BTC/USD style)."""
    if not symbols:
        return {}

    body = _request(
        "GET",
        f"{DATA_URL}/v1beta3/crypto/us/latest/trades",
        params={"symbols": ",".join(sorted({s.upper() for s in symbols}))},
    )
    prices = {}
    for symbol, trade in (body.get("trades") or {}).items():
        price = _as_float(trade.get("p"))
        if price is None:
            continue
        prices[symbol.upper()] = {
            "symbol": symbol.upper(),
            "price": price,
            "as_of": trade.get("t") or _now_iso(),
            "source": "alpaca:crypto_trade",
        }
    return prices


def price_map(symbols: list[str]) -> tuple[dict, dict]:
    """
    Prices for a mixed list of equity and crypto symbols.

    Returns (prices, errors) and never raises: a data-feed problem on one asset
    class must not cost you the valuation of the other. Symbols that came back
    with no price are listed in `errors` so the report can say what it skipped
    instead of silently valuing them at zero.
    """
    wanted = sorted({s.upper() for s in symbols if s})
    if not wanted:
        return {}, {}

    crypto = [s for s in wanted if CRYPTO_MARKER in s]
    equities = [s for s in wanted if CRYPTO_MARKER not in s]

    prices, errors = {}, {}
    for group, fetch in ((equities, latest_equity_prices), (crypto, latest_crypto_prices)):
        if not group:
            continue
        try:
            prices.update(fetch(group))
        except AlpacaAPIError as err:
            for symbol in group:
                errors[symbol] = str(err)

    for symbol in wanted:
        if symbol not in prices and symbol not in errors:
            errors[symbol] = "Alpaca returned no recent trade for this symbol."
    return prices, errors


# --------------------------------------------------------------------- orders


def submit_order(
    symbol: str,
    side: str,
    quantity: float,
    order_type: str = "market",
    limit_price: float | None = None,
    time_in_force: str = "day",
) -> dict:
    """
    Place an order. **Only the Flask app calls this, only after human approval.**

    The MCP server does not import this function - see propose_trade in
    networth_mcp_server.py, which can do no more than queue a row.
    """
    if side not in ("buy", "sell"):
        raise AlpacaAPIError(f"side must be 'buy' or 'sell', got {side!r}")
    if order_type not in ("market", "limit"):
        raise AlpacaAPIError(f"order_type must be 'market' or 'limit', got {order_type!r}")
    if order_type == "limit" and limit_price is None:
        raise AlpacaAPIError("A limit order needs a limit_price.")
    if quantity <= 0:
        raise AlpacaAPIError("quantity must be greater than zero.")

    payload = {
        "symbol": symbol.upper(),
        "qty": str(quantity),
        "side": side,
        "type": order_type,
        # Crypto trades around the clock and rejects 'day'; equities accept both.
        "time_in_force": "gtc" if CRYPTO_MARKER in symbol else time_in_force,
    }
    if order_type == "limit":
        payload["limit_price"] = str(limit_price)

    logger.info("Submitting %s %s %s to Alpaca (%s)", side, quantity, symbol, BASE_URL)
    raw = _request("POST", f"{BASE_URL}/v2/orders", json=payload)
    return {
        "id": raw.get("id"),
        "symbol": raw.get("symbol"),
        "side": raw.get("side"),
        "quantity": _as_float(raw.get("qty")),
        "status": raw.get("status"),
        "filled_quantity": _as_float(raw.get("filled_qty")),
        "filled_avg_price": _as_float(raw.get("filled_avg_price")),
        "submitted_at": raw.get("submitted_at"),
    }


def get_order(order_id: str) -> dict:
    """Re-read an order, to pick up the fill price a market order lacks at submit."""
    raw = _request("GET", f"{BASE_URL}/v2/orders/{order_id}")
    return {
        "id": raw.get("id"),
        "symbol": raw.get("symbol"),
        "side": raw.get("side"),
        "status": raw.get("status"),
        "quantity": _as_float(raw.get("qty")),
        "filled_quantity": _as_float(raw.get("filled_qty")),
        "filled_avg_price": _as_float(raw.get("filled_avg_price")),
    }
