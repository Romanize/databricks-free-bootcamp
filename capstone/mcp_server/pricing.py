"""
One place that answers "what is this holding worth right now?".

Both the Flask app (generating a report, applying a trade fill) and the MCP
server (answering "what is my networth?") need prices, and they must agree, so
the source order lives here rather than in either caller:

  1. **Alpaca** - real-time IEX trade prices, one batched request for the whole
     portfolio. Preferred whenever credentials exist.
  2. **Massive previous close** - end-of-day, one request per symbol at 12.5s
     apart on the free tier. Used only for what Alpaca could not price.

Every quote carries `source` and `as_of`, and those columns are written into
`networth_report`. That is deliberate: a report priced from yesterday's
close and one priced from a live feed are different claims, and the app and the
agent both have to be able to say which they are looking at.

Symbols that neither source can price come back in `errors`. The report draft
leaves those lines blank and says why, rather than filling in a zero - see
reports.build_draft().
"""

import logging

import alpaca_api
import massive_api

logger = logging.getLogger(__name__)


def resolve_prices(symbols: list[str]) -> tuple[dict, dict]:
    """
    Price a list of symbols, Alpaca first and Massive for the remainder.

    Returns ({SYMBOL: {price, as_of, source}}, {SYMBOL: reason}) and never
    raises - a pricing outage should degrade a report, not fail it.
    """
    wanted = sorted({s.upper() for s in symbols if s})
    if not wanted:
        return {}, {}

    prices: dict = {}
    errors: dict = {}

    if alpaca_api.is_configured():
        try:
            prices, errors = alpaca_api.price_map(wanted)
        except Exception as err:  # defensive: price_map already swallows its own
            logger.warning("Alpaca pricing failed entirely: %s", err)
            errors = {symbol: str(err) for symbol in wanted}
    else:
        errors = {symbol: "Alpaca is not configured." for symbol in wanted}

    missing = [symbol for symbol in wanted if symbol not in prices]
    if missing and massive_api.is_configured():
        # Crypto pairs (BTC/USD) are not Massive tickers, so do not waste a
        # rate-limited call on them.
        equities = [s for s in missing if alpaca_api.CRYPTO_MARKER not in s]
        if equities:
            logger.info("Falling back to Massive previous close for %s", equities)
            fallback, fallback_errors = massive_api.previous_closes(equities)
            prices.update(fallback)
            for symbol in fallback:
                errors.pop(symbol, None)
            errors.update(fallback_errors)

    return prices, errors


def price_holdings(holdings: list[dict], priced_types: tuple = ("ticker", "crypto")):
    """Convenience wrapper: pull the symbols out of holdings, then price them."""
    symbols = [
        h["symbol"] for h in holdings if h.get("holding_type") in priced_types and h.get("symbol")
    ]
    return resolve_prices(symbols)
