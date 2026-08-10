"""
Local smoke test for the net-worth MCP server - run this before deploying.

Starts no network server: it drives the FastMCP instance in-process with
fastmcp's in-memory client, exactly like homework 3's smoke test, so what it
exercises is the real MCP surface (tool discovery, argument schemas, JSON
serialization) and not just the Python functions underneath.

Three kinds of check:

  1. **Success paths** - every tool answers with the shape the prompt promises.
  2. **Failure paths** - `error` and `no_data` are the statuses the system prompt
     makes promises about, so bad arguments and empty tables are tested too.
  3. **Key security** - the confirmation-key guard is the only thing standing
     between the agent and a real order, so it gets its own section: the key must
     never leak through a tool result, and forged, replayed and expired keys must
     all be refused.

It needs a database. It does NOT need Alpaca or Massive credentials - the tools
that use them answer `no_data` when unconfigured, which is itself worth testing.

    createdb capstone && psql capstone -c 'CREATE EXTENSION vector'
    createdb capstone_tracing
    export LAKEBASE_URL=postgresql://localhost:5432/capstone
    export LAKEBASE_TRACING_URL=postgresql://localhost:5432/capstone_tracing

    pip install -r requirements.txt
    python smoke_test.py --seed

`--seed` inserts a small portfolio, a net worth report and one fake news article
so the success paths have something to return. It is idempotent.
"""

import argparse
import asyncio
import datetime as dt
import json
import logging

from fastmcp import Client

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass

import alpaca_api
import embeddings
import schema
import tracing
from lakebase import app_db
from networth_mcp_server import mcp, startup

logging.basicConfig(level=logging.WARNING)

PASS, FAIL = "PASS", "FAIL"


def seed() -> None:
    """A small, realistic portfolio so the success paths have data."""
    print("Seeding sample data...")

    existing = {h["alias"].lower(): h for h in schema.list_holdings(active_only=False)}
    wanted = [
        {"alias": "Apple", "holding_type": "ticker", "symbol": "AAPL"},
        {"alias": "Microsoft", "holding_type": "ticker", "symbol": "MSFT"},
        {"alias": "Emergency fund", "holding_type": "bank", "institution": "Ally"},
        {"alias": "Brokerage cash", "holding_type": "cash"},
    ]
    for holding in wanted:
        if holding["alias"].lower() not in existing:
            schema.create_holding(holding)

    holdings = {h["alias"]: h for h in schema.list_holdings()}
    schema.add_to_watchlist("NVDA", "AI capex cycle")

    if not schema.active_plan():
        plan = schema.create_plan(
            {
                "name": "Retire at 60",
                "goal_amount": 1_200_000,
                "expected_annual_rate": 0.07,
                "years": 25,
                "expected_inflation": 0.03,
                "monthly_contribution": 1500,
                "annual_contribution": 5000,
            }
        )
        schema.activate_plan(plan["id"])

    # A fake article, so search and sentiment have something to find without
    # spending the Massive free tier's 5-per-minute quota on a smoke test.
    title = "Apple beats on services revenue, guides higher"
    body = (
        "Apple reported quarterly results ahead of consensus, driven by services "
        "growth and stronger iPhone demand in emerging markets. Management guided "
        "next quarter above the street."
    )
    schema.upsert_articles([{
        "id": "smoke-test-article-1",
        "title": title,
        "description": body,
        "embed_text": f"{title}\n\n{body}",
        "article_url": "https://example.invalid/aapl-earnings",
        "publisher": "Smoke Test Wire",
        "tickers": ["AAPL"],
        "keywords": ["earnings", "services"],
        "published_utc": "2026-08-01T12:00:00Z",
        "payload": {"smoke": True},
    }])
    schema.upsert_sentiments([{
        "symbol": "AAPL",
        "article_id": "smoke-test-article-1",
        "sentiment": "positive",
        "sentiment_reasoning": "Results and guidance both came in above consensus.",
        "published_utc": "2026-08-01T12:00:00Z",
    }])

    pending = embeddings.count_pending()
    if pending:
        print(f"  embedding {pending} article(s) - downloads the model on first run...")
        embeddings.embed_pending()

    # Two reports a month apart, so the history and monthly views have shape.
    today = dt.date.today()
    for offset, price_aapl, cash in ((35, 210.0, 5000), (0, 231.5, 5200)):
        report_date = today - dt.timedelta(days=offset)
        schema.write_report(report_date, [
            {"holding_id": holdings["Apple"]["id"], "quantity": 40, "price": price_aapl,
             "price_source": "smoke_test", "value": 40 * price_aapl},
            {"holding_id": holdings["Microsoft"]["id"], "quantity": 15, "price": 512.10,
             "price_source": "smoke_test", "value": 15 * 512.10},
            {"holding_id": holdings["Emergency fund"]["id"], "value": 18000,
             "price_source": "manual"},
            {"holding_id": holdings["Brokerage cash"]["id"], "value": cash,
             "price_source": "manual"},
        ])
    print("Seeded.\n")


def build_cases() -> list[tuple[str, dict, str]]:
    """(tool, arguments, expected status) - expectations adapt to what is loaded."""
    has_report = schema.latest_report() is not None
    has_plan = schema.active_plan() is not None
    has_news = schema.has_embeddings()
    broker = "success" if alpaca_api.is_configured() else "no_data"
    # On a truly fresh database no proposal has ever been made, and `no_data` is
    # the correct answer then - the expectation has to adapt, like the others.
    has_trades = bool(schema.list_trades(limit=1))

    return [
        # ---- success paths (or a clean no_data when nothing has been loaded)
        ("get_networth_summary", {}, "success" if has_report else "no_data"),
        ("get_holdings_breakdown", {"group_by": "type"}, "success" if has_report else "no_data"),
        ("get_holdings_breakdown", {"group_by": "holding"}, "success" if has_report else "no_data"),
        ("get_networth_history", {"monthly": True}, "success" if has_report else "no_data"),
        ("get_networth_history", {"monthly": False}, "success" if has_report else "no_data"),
        ("get_investment_plan", {}, "success" if has_plan else "no_data"),
        ("get_investment_plan_projection", {}, "success" if has_plan else "no_data"),
        # A what-if writes nothing, so it answers on an empty database too.
        ("project_scenario", {"years": 8, "goal_amount": 2000000,
                              "starting_value": 200000}, "success"),
        ("project_scenario", {"years": 20, "starting_value": 0,
                              "monthly_contribution": 1500}, "success"),
        ("search_ticker_news", {"query": "earnings beat and guidance"},
         "success" if has_news else "no_data"),
        ("search_ticker_news", {"query": "earnings", "symbol": "AAPL", "top_k": 3},
         "success" if has_news else "no_data"),
        ("get_ticker_sentiment", {"symbol": "AAPL"}, "success" if has_news else "no_data"),
        ("get_watchlist", {}, "success"),
        ("add_to_watchlist", {"symbol": "TSLA", "reason": "smoke test"}, "success"),
        ("get_alpaca_account", {}, broker),
        ("list_pending_trades", {"status": "all"}, "success" if has_trades else "no_data"),

        # ---- plan writes
        ("create_investment_plan", {
            "name": "Smoke test plan", "goal_amount": 500000,
            "expected_annual_rate": 0.06, "years": 15, "monthly_contribution": 800,
        }, "success"),

        # ---- deliberate failures
        ("get_holdings_breakdown", {"group_by": "sector"}, "error"),
        ("search_ticker_news", {"query": ""}, "error"),
        ("add_to_watchlist", {"symbol": "not a ticker!"}, "error"),
        ("propose_trade", {"symbol": "AAPL", "side": "hold", "quantity": 1}, "error"),
        ("propose_trade", {"symbol": "AAPL", "side": "buy", "quantity": -5}, "error"),
        ("propose_trade", {"symbol": "AAPL", "side": "buy", "quantity": 1,
                           "order_type": "limit"}, "error"),
        ("get_ticker_sentiment", {"symbol": "ZZZZ"}, "no_data"),
        ("search_ticker_news", {"query": "anything", "symbol": "ZZZZ"}, "no_data"),
        # A rate of 900% is a typo, and a projection built on one is worse than none.
        ("create_investment_plan", {
            "name": "Nonsense", "goal_amount": 1000,
            "expected_annual_rate": 9.0, "years": 10,
        }, "error"),
        # Out of reach at any contribution: the tool must say so, not solve to a
        # number, and it is still a successful answer.
        ("project_scenario", {"years": 1, "goal_amount": 1e12,
                              "starting_value": 1000}, "success"),
        ("project_scenario", {"years": 8, "goal_amount": -5,
                              "starting_value": 1000}, "error"),
        ("project_scenario", {"years": 8, "starting_value": -1}, "error"),
        ("update_investment_plan", {"plan_id": 999999, "goal_amount": 1}, "error"),
        ("activate_investment_plan", {"plan_id": 999999}, "error"),
    ]


async def check_key_security(client, results: list) -> None:
    """
    The confirmation-key guard - the only thing between the agent and an order.

    Everything here is run through the MCP client, i.e. exactly the surface the
    agent sees, because a leak that only shows up in the tool payload is the
    whole risk.
    """
    print("\n--- confirmation key security " + "-" * 44)

    def verdict(label: str, ok: bool, detail: str = "") -> None:
        print(f"[{PASS if ok else FAIL}] {label:<58}{detail}")
        results.append(ok)

    # A fresh proposal, made the way the agent makes one.
    proposal = (await client.call_tool(
        "propose_trade",
        {"symbol": "AAPL", "side": "buy", "quantity": 2, "rationale": "key security test"},
    )).data
    trade_id = proposal.get("proposal_id")
    verdict("propose_trade reports executed=False", proposal.get("executed") is False)

    # The strongest form of "no leak at proposal time": no key exists yet at all.
    verdict("no key is minted by propose_trade", _key_column(trade_id) is None)
    verdict("propose_trade exposes no key-shaped field", not _key_fields(proposal))

    listed = (await client.call_tool("list_pending_trades", {"status": "pending"})).data
    verdict("list_pending_trades exposes no key-shaped field", not _key_fields(listed))

    # The agent guessing a key must fail, and fail identically every time.
    forged = (await client.call_tool(
        "execute_trade", {"proposal_id": trade_id, "confirmation_key": "guessed-key"}
    )).data
    verdict("forged key is refused", forged.get("status") == "error",
            f"-> {str(forged.get('message', ''))[:60]}")

    empty = (await client.call_tool(
        "execute_trade", {"proposal_id": trade_id, "confirmation_key": ""}
    )).data
    verdict("empty key is refused", empty.get("status") == "error")

    # The trade must still be pending - a failed execute must not consume it.
    still = schema.get_trade(trade_id)
    verdict("refused attempts left the trade pending", still["status"] == "pending")

    # Now mint one the way the app does, on a human click.
    issued = schema.issue_confirmation_key(trade_id)
    verdict("issue_confirmation_key returns a key", issued is not None)
    _, real_key = issued
    verdict("issued key is not stored in plaintext", _key_column(trade_id) != real_key)

    # A second issue on the same trade must fail: it is no longer pending.
    verdict("key cannot be minted twice", schema.issue_confirmation_key(trade_id) is None)

    # Wrong key against an approved trade: still refused.
    wrong = (await client.call_tool(
        "execute_trade", {"proposal_id": trade_id, "confirmation_key": real_key + "x"}
    )).data
    verdict("wrong key on an approved trade is refused", wrong.get("status") == "error")

    # The real key. Alpaca is almost certainly unconfigured locally, so the
    # order fails - but the key must have been REDEEMED either way, which is the
    # property under test.
    used = (await client.call_tool(
        "execute_trade", {"proposal_id": trade_id, "confirmation_key": real_key}
    )).data
    redeemed = schema.get_trade(trade_id)["status"] in ("executed", "failed")
    verdict("valid key is redeemed", redeemed,
            f"-> status={schema.get_trade(trade_id)['status']}")
    verdict("hash cleared on redemption", _key_column(trade_id) is None)

    # Replay: the same key, a second time. This is the property that makes a key
    # sitting in conversation history harmless.
    replay = (await client.call_tool(
        "execute_trade", {"proposal_id": trade_id, "confirmation_key": real_key}
    )).data
    verdict("replayed key is refused", replay.get("status") == "error")

    # Expiry: mint a key, age it past its TTL, then try it.
    expiring = schema.create_pending_trade(
        {"symbol": "MSFT", "side": "buy", "quantity": 1, "proposed_by": "agent"}
    )
    _, expiring_key = schema.issue_confirmation_key(expiring["id"])
    app_db.execute(
        "UPDATE pending_trades SET key_expires_at = now() - interval '1 minute' WHERE id = %s",
        (expiring["id"],),
    )
    stale = (await client.call_tool(
        "execute_trade", {"proposal_id": expiring["id"], "confirmation_key": expiring_key}
    )).data
    verdict("expired key is refused", stale.get("status") == "error")

    # And the trace row must not have persisted the key either.
    if tracing.tracing_available():
        traced = json.dumps(tracing.recent_calls(limit=30), default=str)
        verdict("no confirmation key appears in the trace rows", real_key not in traced)


def _key_fields(payload) -> list[str]:
    """
    Every field name in a tool payload that looks like key material.

    Checks names rather than values: the tools legitimately *mention* the word
    "confirmation" in their prose (telling the agent to ask the user for a key),
    so a substring search over the whole blob false-positives. What must never
    appear is a field carrying one.
    """
    found = []

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if "confirmation" in key.lower() or key.lower().endswith("_hash"):
                    found.append(key)
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    return found


def _key_column(trade_id: int):
    """Read confirmation_hash directly - the only place that bypasses the allowlist."""
    row = app_db.query_one(
        "SELECT confirmation_hash FROM pending_trades WHERE id = %s", (trade_id,)
    )
    return row["confirmation_hash"] if row else None


async def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke test the net-worth MCP server.")
    parser.add_argument("--seed", action="store_true", help="insert sample data first")
    parser.add_argument("--verbose", action="store_true", help="print full tool payloads")
    args = parser.parse_args()

    startup()  # the same schema init the deployed app runs
    print("Tracing: " + ("ON" if tracing.tracing_available() else "OFF (no tracing database)"))
    print(f"Alpaca:  {'configured' if alpaca_api.is_configured() else 'not configured'}"
          f" (paper={alpaca_api.is_paper()})\n")

    if args.seed:
        seed()

    results: list[bool] = []

    async with Client(mcp) as client:
        tools = await client.list_tools()
        print(f"{len(tools)} tools registered:")
        for tool in tools:
            properties = tool.inputSchema.get("properties", {})
            # `ctx` is injected by FastMCP and must never appear in the schema -
            # if it does, the agent will try to fill it in.
            leaked = " <-- LEAKED ctx INTO SCHEMA" if "ctx" in properties else ""
            print(f"  - {tool.name}({', '.join(sorted(properties))}){leaked}")
            results.append(not leaked)
        print()

        for name, arguments, expected in build_cases():
            result = await client.call_tool(name, arguments)
            payload = result.data if isinstance(result.data, dict) else {}
            status = payload.get("status", "?")

            ok = status == expected
            results.append(ok)
            label = f"{name}({json.dumps(arguments)})"
            print(f"[{PASS if ok else FAIL}] {label[:66]:<66} {status} (want {expected})")

            if not ok or args.verbose:
                print(json.dumps(payload, indent=2, default=str)[:700])
            elif status in ("error", "no_data"):
                print(f"       -> {str(payload.get('message', ''))[:110]}")

        await check_key_security(client, results)

    passed = sum(1 for r in results if r)
    print(f"\n{passed}/{len(results)} checks passed")
    if tracing.tracing_available():
        print(f"{len(tracing.recent_calls(limit=200))} trace rows in the tracing database")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
