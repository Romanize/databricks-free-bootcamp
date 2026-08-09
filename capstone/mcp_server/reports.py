"""
Building and submitting a net worth report.

Since `holdings` carries no values, a report is not something the app can
generate on its own - it is something you fill in. This module produces the
**draft** (one prefilled line per active holding) and validates the **submission**
that comes back.

## Where each prefilled number comes from

For a ticker or crypto holding:

  * **quantity** - from Alpaca's open positions when they are configured and hold
    that symbol, otherwise carried forward from the last report, otherwise blank.
    Alpaca wins because it is the only source that knows about trades the app
    never saw.
  * **price**    - live from Alpaca, falling back to Massive's previous close.

For a cash, bank or wallet holding there is nothing to fetch: the value is
carried forward from the last report as a starting point, and you correct it.
That is the honest behaviour - the app cannot know your bank balance, and
carrying the old number forward silently would be worse than showing it as a
number you are expected to check. Every line therefore reports its own
`quantity_source` and `price_source` so the form can say where each figure came
from.

Nothing here writes anything. `submit()` is the only function that touches the
database, and it writes exactly the lines it was given.
"""

import datetime as dt
import logging

import alpaca_api
import pricing
import schema

logger = logging.getLogger(__name__)


class ReportError(Exception):
    """A report could not be built or submitted; the message is safe to show."""


def _positions_by_symbol() -> dict:
    """Alpaca's open positions, keyed by symbol. Empty when unconfigured."""
    if not alpaca_api.is_configured():
        return {}
    try:
        return {p["symbol"].upper(): p for p in alpaca_api.get_positions() if p.get("symbol")}
    except alpaca_api.AlpacaAPIError as err:
        logger.warning("Could not read Alpaca positions for the draft: %s", err)
        return {}


def build_draft(report_date=None) -> dict:
    """
    One prefilled, editable line per active holding.

    If a report already exists for `report_date`, its saved lines win over every
    other source - you are editing that report, not starting a new one.
    """
    report_date = report_date or dt.date.today()

    holdings = schema.list_holdings(active_only=True)
    if not holdings:
        raise ReportError(
            "There are no active holdings to report on. Add some on the Holdings tab first."
        )

    previous = schema.latest_line_by_holding()
    existing = {line["holding_id"]: line for line in schema.report_lines(report_date)}
    positions = _positions_by_symbol()
    prices, price_errors = pricing.price_holdings(holdings)

    lines, warnings = [], []
    for holding in holdings:
        prior = previous.get(holding["id"])
        saved = existing.get(holding["id"])
        line = {
            "holding_id": holding["id"],
            "alias": holding["alias"],
            "holding_type": holding["holding_type"],
            "symbol": holding["symbol"],
            "institution": holding["institution"],
            "quantity": None,
            "price": None,
            "price_as_of": None,
            "price_source": "manual",
            "value": None,
            "notes": saved["notes"] if saved else None,
            "quantity_source": "none",
            "editing_existing": saved is not None,
        }

        if holding["holding_type"] in schema.PRICED_TYPES:
            symbol = (holding["symbol"] or "").upper()
            position = positions.get(symbol)

            if saved is not None:
                line["quantity"] = _f(saved["quantity"])
                line["quantity_source"] = "this report"
            elif position and position.get("quantity") is not None:
                line["quantity"] = position["quantity"]
                line["quantity_source"] = "alpaca position"
            elif prior and prior["quantity"] is not None:
                line["quantity"] = _f(prior["quantity"])
                line["quantity_source"] = f"carried from {prior['report_date']}"

            quote = prices.get(symbol)
            if quote:
                line["price"] = quote["price"]
                line["price_as_of"] = quote.get("as_of")
                line["price_source"] = quote.get("source") or "market"
            elif prior and prior["price"] is not None:
                line["price"] = _f(prior["price"])
                line["price_source"] = f"carried from {prior['report_date']}"
                warnings.append(
                    f"No live price for {symbol} ({holding['alias']}) - carried "
                    f"forward from {prior['report_date']}. Check it before submitting."
                )
            else:
                warnings.append(
                    f"No price available for {symbol} ({holding['alias']}) - "
                    f"{price_errors.get(symbol, 'unknown reason')}. Enter a value by hand."
                )

            if line["quantity"] is not None and line["price"] is not None:
                line["value"] = round(line["quantity"] * line["price"], 2)
        else:
            if saved is not None:
                line["value"] = _f(saved["value"])
                line["price_source"] = "this report"
            elif prior is not None:
                line["value"] = _f(prior["value"])
                line["price_source"] = f"carried from {prior['report_date']}"
                warnings.append(
                    f"{holding['alias']} is carried forward from "
                    f"{prior['report_date']} - update it if the balance has moved."
                )

        lines.append(line)

    return {
        "report_date": report_date.isoformat(),
        "editing_existing": bool(existing),
        "lines": lines,
        "warnings": warnings,
        "draft_total": round(sum(line["value"] or 0 for line in lines), 2),
        "note": (
            "Every figure is editable. Values are USD. Submitting replaces the "
            "report for this date - there is at most one per day."
        ),
    }


def _f(value):
    return None if value is None else float(value)


def submit(report_date, submitted_lines: list[dict]) -> dict:
    """
    Validate and write a report.

    Lines arrive from the browser, so nothing here trusts them: each one must
    name an active holding, and each must carry a value that parses.
    """
    if not submitted_lines:
        raise ReportError("A report needs at least one line.")

    report_date = report_date or dt.date.today()
    active = {h["id"]: h for h in schema.list_holdings(active_only=True)}

    lines, skipped = [], []
    for raw in submitted_lines:
        try:
            holding_id = int(raw.get("holding_id"))
        except (TypeError, ValueError):
            raise ReportError(f"Bad holding_id in submitted line: {raw.get('holding_id')!r}")

        holding = active.get(holding_id)
        if not holding:
            # Deactivated between drawing the draft and submitting it.
            skipped.append(holding_id)
            continue

        quantity = _number(raw.get("quantity"), f"quantity for {holding['alias']}")
        price = _number(raw.get("price"), f"price for {holding['alias']}")
        value = _number(raw.get("value"), f"value for {holding['alias']}")

        # A priced line can be given as quantity x price; derive the value so the
        # two can never disagree in the stored row.
        if value is None and quantity is not None and price is not None:
            value = quantity * price
        if value is None:
            raise ReportError(
                f"{holding['alias']} has no value. Enter one, or remove the holding."
            )

        lines.append(
            {
                "holding_id": holding_id,
                "quantity": quantity,
                "price": price,
                "price_as_of": raw.get("price_as_of"),
                "price_source": (raw.get("price_source") or "manual")[:60],
                "value": value,
                "notes": (raw.get("notes") or None),
            }
        )

    if not lines:
        raise ReportError("None of the submitted lines matched an active holding.")

    report = schema.write_report(report_date, lines)
    return {
        "report": report,
        "lines": len(lines),
        "skipped_inactive": skipped,
        "report_date": report_date.isoformat()
        if hasattr(report_date, "isoformat") else str(report_date),
    }


def _number(value, label: str):
    """Parse an optional number, rejecting text rather than coercing it to zero."""
    if value in (None, "", "null"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ReportError(f"{label} must be a number, got {value!r}.")


def live_valuation() -> dict:
    """
    What the portfolio would be worth right now, without saving anything.

    Uses the latest report's quantities - since `holdings` has none - re-priced
    at current prices. Manual holdings carry their last reported value, which is
    the honest thing to do: nothing has re-read your bank balance.
    """
    report_date = schema.latest_report_date()
    if not report_date:
        raise ReportError("No report exists yet, so there is nothing to re-value.")

    lines = schema.report_lines(report_date)
    symbols = [line["symbol"] for line in lines
               if line["holding_type"] in schema.PRICED_TYPES and line["symbol"]]
    prices, errors = pricing.resolve_prices(symbols)

    total, repriced, stale = 0.0, 0, []
    for line in lines:
        value = _f(line["value"]) or 0.0
        if line["holding_type"] in schema.PRICED_TYPES:
            quote = prices.get((line["symbol"] or "").upper())
            quantity = _f(line["quantity"])
            if quote and quantity is not None:
                value = round(quantity * quote["price"], 2)
                repriced += 1
            else:
                stale.append(line["symbol"])
        total += value

    return {
        "based_on_report": report_date.isoformat(),
        "total_value": round(total, 2),
        "repriced_holdings": repriced,
        "not_repriced": stale,
        "unpriced_reasons": errors,
        "note": (
            "Live figure, not saved. Quantities come from the report of "
            f"{report_date}; cash and bank balances are its values unchanged."
        ),
    }
