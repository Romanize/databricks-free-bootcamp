"""
Lakebase schema and writes for the agent tool-call log.

`init_db()` runs at startup in both apps and is idempotent: it creates
`weather_tool_calls`, one row per MCP tool invocation, which is what the
dashboard app reads.

Logging is strictly best-effort. `record_call()` never raises and never lets a
database problem turn a working weather answer into a failed tool call - if
Lakebase is unreachable the write is dropped, a warning is logged, and after a
few consecutive failures logging switches itself off for the life of the
process so a dead database cannot add its connection timeout to every single
tool call.

This file is duplicated in dashboard/ - each Databricks App deploys from its own
folder, so the two apps cannot import a shared module. Keep the copies in sync.
"""

import logging
import os

from psycopg2.extras import Json

import lakebase

logger = logging.getLogger(__name__)

TABLE = "weather_tool_calls"

# Set WEATHER_LOG_TOOL_CALLS=0 to run the MCP server with no database at all.
LOGGING_ENABLED = os.environ.get("WEATHER_LOG_TOOL_CALLS", "1").lower() not in (
    "0",
    "false",
    "no",
)

# After this many consecutive write failures, stop trying until the next restart.
MAX_CONSECUTIVE_FAILURES = 3
_failures = 0
_disabled = False

DDL = [
    f"""
    CREATE TABLE IF NOT EXISTS {TABLE} (
        id            BIGSERIAL PRIMARY KEY,
        tool_name     TEXT NOT NULL,
        location      TEXT,
        arguments     JSONB NOT NULL,
        status        TEXT NOT NULL CHECK (status IN ('success', 'error')),
        verdict       TEXT,
        summary       TEXT,
        error_message TEXT,
        duration_ms   INT,
        called_at     TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    f"CREATE INDEX IF NOT EXISTS ix_{TABLE}_called_at ON {TABLE} (called_at DESC)",
    f"CREATE INDEX IF NOT EXISTS ix_{TABLE}_tool_name ON {TABLE} (tool_name)",
]

INSERT_SQL = f"""
    INSERT INTO {TABLE}
        (tool_name, location, arguments, status, verdict, summary,
         error_message, duration_ms)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
"""


def init_db() -> None:
    """Create the tool-call table and its indexes if they are missing."""
    lakebase.execute_script([(sql, None) for sql in DDL])
    logger.info("Lakebase schema ready (%s)", TABLE)


def logging_available() -> bool:
    """True when a tool-call write is worth attempting."""
    return LOGGING_ENABLED and not _disabled and lakebase.is_configured()


def record_call(
    tool_name: str,
    arguments: dict,
    status: str,
    location: str | None = None,
    verdict: str | None = None,
    summary: str | None = None,
    error_message: str | None = None,
    duration_ms: int | None = None,
) -> None:
    """
    Write one tool invocation to Lakebase. Swallows every failure by design.

    The caller is an MCP tool that has already produced its answer; losing the
    audit row is always preferable to failing the answer.
    """
    global _failures, _disabled

    if not logging_available():
        return

    try:
        lakebase.execute(
            INSERT_SQL,
            (
                tool_name,
                location,
                Json(arguments),
                status,
                verdict,
                summary,
                error_message,
                duration_ms,
            ),
        )
        _failures = 0
    except Exception as err:
        _failures += 1
        logger.warning(
            "Could not log tool call %s to Lakebase (%s/%s): %s",
            tool_name,
            _failures,
            MAX_CONSECUTIVE_FAILURES,
            err,
        )
        if _failures >= MAX_CONSECUTIVE_FAILURES:
            _disabled = True
            logger.error(
                "Disabling tool-call logging after %s consecutive failures; "
                "weather tools continue to work normally.",
                _failures,
            )


# ------------------------------------------------------- dashboard read side


def recent_calls(limit: int = 50, tool_name: str | None = None) -> list[dict]:
    """Most recent tool calls, newest first, optionally filtered by tool."""
    where = "WHERE tool_name = %s" if tool_name else ""
    params = ([tool_name] if tool_name else []) + [limit]
    return lakebase.query(
        f"""
        SELECT id, tool_name, location, arguments, status, verdict, summary,
               error_message, duration_ms, called_at
        FROM {TABLE}
        {where}
        ORDER BY called_at DESC, id DESC
        LIMIT %s
        """,
        tuple(params),
    )


def stats() -> dict:
    """Headline counters for the dashboard's status bar."""
    totals = lakebase.query_one(
        f"""
        SELECT COUNT(*)                                             AS total_calls,
               COUNT(*) FILTER (WHERE status = 'error')             AS errors,
               COUNT(DISTINCT location) FILTER (WHERE location IS NOT NULL)
                                                                    AS locations,
               COUNT(*) FILTER (WHERE called_at > now() - interval '24 hours')
                                                                    AS last_24h,
               ROUND(AVG(duration_ms))                              AS avg_duration_ms,
               MAX(called_at)                                       AS last_call_at
        FROM {TABLE}
        """
    ) or {}

    by_tool = lakebase.query(
        f"""
        SELECT tool_name,
               COUNT(*)                                   AS calls,
               COUNT(*) FILTER (WHERE status = 'error')   AS errors,
               ROUND(AVG(duration_ms))                    AS avg_duration_ms
        FROM {TABLE}
        GROUP BY tool_name
        ORDER BY calls DESC
        """
    )
    return {**totals, "by_tool": by_tool}
