"""
Trade execution, gated on a single-use confirmation key.

## The flow

    1. agent  propose_trade()            -> pending_trades row, status 'pending'
                                            no key exists yet
    2. human  clicks Accept in the app   -> issue_key() mints a key, stores only
                                            its SHA-256, status 'approved'
    3. app    messages the agent with    -> "execute proposal #12, key=..."
              the plaintext key
    4. agent  execute_trade(12, key)     -> execute_confirmed() redeems it,
                                            submits to Alpaca, status 'executed'

## What the guardrail actually is

Earlier revisions of this project made the agent structurally incapable of
trading: the MCP server simply did not import the order-submitting code. That is
no longer true - `execute_trade` is a real tool and this module is imported by
the MCP server. Being precise about what replaced it matters:

**The agent can call `execute_trade` whenever it likes. It cannot succeed
without a key, and a key only comes into existence when a human clicks Accept.**

Four properties make that hold, and all four are enforced in `schema.py` rather
than here:

  * `propose_trade` never mints a key, so there is nothing to read at proposal time.
  * No tool returns `confirmation_hash` - `SAFE_TRADE_COLUMNS` is the allowlist,
    so the agent cannot fetch its own key back and confirm itself.
  * Redemption is one atomic `UPDATE ... WHERE status='approved' AND hash=...`
    that clears the hash, so a key found in conversation history is spent and a
    replay matches no row.
  * Keys expire (KEY_TTL_MINUTES), so an abandoned approval does not stay armed.

Only the hash is stored, so reading the database does not yield a usable key
either.

## What this module does NOT do

It does not touch `holdings` and it does not write a net worth report. The app
tracks snapshots of what accounts were worth at a point in time; it does not
model cash flow, position deltas or cost basis. After a trade executes, the next
report you submit picks up the new reality from Alpaca.

This file is duplicated in app/ - each Databricks App deploys from its own
folder. Keep the copies in sync.
"""

import logging
import time

import alpaca_api
import schema

logger = logging.getLogger(__name__)

# A market order usually fills in well under a second on paper. Poll briefly for
# the fill price, then give up: an unfilled order is normal outside market hours
# and must not hang the request.
FILL_POLL_ATTEMPTS = 4
FILL_POLL_SECONDS = 0.5


class TradeError(Exception):
    """A trade could not be approved or executed; the message is safe to show."""


def issue_key(trade_id: int) -> tuple[dict, str]:
    """
    Approve a proposal and mint its confirmation key. **Human-triggered only.**

    Called from the Flask route behind the Accept button and nowhere else. The
    MCP server does not expose this - if it did, the agent could approve its own
    proposals and the whole scheme would be decorative.
    """
    trade = schema.get_trade(trade_id)
    if not trade:
        raise TradeError(f"Trade #{trade_id} does not exist.")
    if trade["status"] != "pending":
        raise TradeError(
            f"Trade #{trade_id} is already {trade['status']}, so it cannot be approved again."
        )

    issued = schema.issue_confirmation_key(trade_id)
    if not issued:
        # Lost the race with a concurrent Accept. Not an error worth alarming
        # about, but definitely not a second key.
        raise TradeError(f"Trade #{trade_id} was already claimed by another request.")

    approved, key = issued
    return approved, key


def reject(trade_id: int, reason: str | None = None) -> dict:
    """Decline a proposal. Nothing is submitted and no key is ever minted."""
    trade = schema.get_trade(trade_id)
    if not trade:
        raise TradeError(f"Trade #{trade_id} does not exist.")
    if trade["status"] != "pending":
        raise TradeError(f"Trade #{trade_id} is already {trade['status']}.")

    rejected = schema.reject_trade(trade_id, reason)
    if not rejected:
        raise TradeError(f"Trade #{trade_id} was already claimed by another request.")
    return rejected


def execute_confirmed(trade_id: int, confirmation_key: str) -> dict:
    """
    Redeem a confirmation key and submit the order to Alpaca.

    This is the one function in the project that can place a real order. It is
    reachable from the MCP server, and therefore from the agent - which is fine
    precisely because it cannot get past the first line without a key a human
    caused to exist.
    """
    claimed = schema.redeem_confirmation_key(trade_id, confirmation_key)
    if not claimed:
        # Deliberately one message for every failure mode - wrong key, expired
        # key, already-spent key, wrong status, no such trade. Distinguishing
        # them would tell a caller which part of a guess was right.
        raise TradeError(
            f"The confirmation key for trade #{trade_id} is not valid. It may have "
            "expired, already been used, or never been approved. Ask the user to "
            "approve the proposal in the app again."
        )

    if not alpaca_api.is_configured():
        schema.finalize_trade(
            trade_id, "failed", error_message="Alpaca is not configured."
        )
        raise TradeError(
            "Alpaca is not configured, so the order could not be submitted. The "
            "proposal has been marked failed."
        )

    try:
        order = alpaca_api.submit_order(
            symbol=claimed["symbol"],
            side=claimed["side"],
            quantity=float(claimed["quantity"]),
            order_type=claimed["order_type"],
            limit_price=float(claimed["limit_price"]) if claimed["limit_price"] else None,
        )
    except alpaca_api.AlpacaAPIError as err:
        schema.finalize_trade(trade_id, "failed", error_message=str(err))
        raise TradeError(f"Alpaca rejected the order: {err}") from err
    except Exception as err:  # never leave a trade stuck in 'executing'
        logger.exception("Unexpected failure submitting trade #%s", trade_id)
        schema.finalize_trade(trade_id, "failed", error_message=f"{type(err).__name__}: {err}")
        raise TradeError("The order could not be submitted. The proposal was marked failed.") from err

    fill_price, warning = _await_fill(order)
    final = schema.finalize_trade(
        trade_id, "executed",
        alpaca_order_id=order["id"],
        filled_price=fill_price,
        error_message=warning,
    )

    logger.info(
        "Executed trade #%s: %s %s %s (order %s)",
        trade_id, claimed["side"], claimed["quantity"], claimed["symbol"], order["id"],
    )
    return {
        "trade": final,
        "order": order,
        "fill_price": fill_price,
        "warning": warning,
        "note": (
            "The order is with Alpaca. Your holdings and net worth report are "
            "unchanged - submit a new report to pick up the new position."
        ),
    }


def _await_fill(order: dict) -> tuple[float | None, str | None]:
    """Poll the order briefly for a fill price. Returns (price, warning)."""
    if order.get("filled_avg_price"):
        return order["filled_avg_price"], None

    for _ in range(FILL_POLL_ATTEMPTS):
        time.sleep(FILL_POLL_SECONDS)
        try:
            current = alpaca_api.get_order(order["id"])
        except alpaca_api.AlpacaAPIError as err:
            logger.warning("Could not re-read order %s: %s", order["id"], err)
            break
        if current.get("filled_avg_price"):
            return current["filled_avg_price"], None

    return None, (
        "The order was accepted but has not filled yet, which is normal outside "
        "market hours. It will fill when the market opens."
    )
