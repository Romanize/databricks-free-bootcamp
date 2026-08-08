"""
Local smoke test for the weather MCP server - run this before deploying.

Starts no network server: it drives the FastMCP instance in-process with
fastmcp's in-memory client, so what it exercises is the real MCP surface (tool
discovery, argument schemas, JSON serialization) and not just the Python
functions underneath.

It does make real calls to Open-Meteo and api.weather.gov, so it needs internet
but no credentials.

Usage:
    pip install -r requirements.txt
    python smoke_test.py
"""

import asyncio
import json

from fastmcp import Client

import schema
from weather_mcp_server import mcp, startup

CASES = [
    ("get_current_weather", {"location": "Austin, TX"}),
    ("get_forecast", {"location": "Chicago, IL", "days": 3}),
    ("get_forecast", {"location": "London, UK", "days": 2, "units": "metric"}),
    ("predict_umbrella_needed", {"location": "Seattle, WA", "date": "tomorrow"}),
    ("get_severe_weather_alerts", {"location": "Miami, FL"}),
    (
        "compare_locations_weather",
        {"locations": ["Chicago, IL", "Austin, TX", "Denver, CO"], "date": "tomorrow"},
    ),
    # Error paths: each must come back as a clean status "error", never a raise.
    ("get_current_weather", {"location": "Nowhereville12345"}),
    ("predict_umbrella_needed", {"location": "Chicago, IL", "date": "2099-01-01"}),
    ("compare_locations_weather", {"locations": ["Chicago, IL"]}),
]


async def main() -> None:
    # Same startup the deployed app runs: creates the tool-call table when a
    # Lakebase URL is configured, no-ops when there is no database.
    startup()
    logging_on = schema.logging_available()
    print(
        "Tool-call logging: "
        + ("ON - calls will be written to Lakebase" if logging_on else "OFF")
    )

    async with Client(mcp) as client:
        tools = await client.list_tools()
        print(f"{len(tools)} tools registered:")
        for tool in tools:
            args = ", ".join(sorted(tool.inputSchema.get("properties", {})))
            print(f"  - {tool.name}({args})")

        for name, args in CASES:
            result = await client.call_tool(name, args)
            payload = result.data
            status = payload.get("status") if isinstance(payload, dict) else "?"
            print(f"\n### {name}({json.dumps(args)}) -> {status}")
            print(json.dumps(payload, indent=2, default=str)[:700])

        if logging_on:
            logged = schema.recent_calls(limit=len(CASES))
            print(f"\n{len(logged)} of {len(CASES)} calls logged to Lakebase.")


if __name__ == "__main__":
    asyncio.run(main())
