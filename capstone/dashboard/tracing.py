"""
Agent tracing: one row per MCP tool call, in the `tracing` Lakebase database.

Written by the MCP server, read by the dashboard app. Carried over from
homework 3 with the improvement that homework's README listed as the obvious
next step: **the row now records a session id**, so a multi-turn agent
conversation can be reconstructed instead of appearing as unrelated calls.

Logging is strictly best-effort. `record_call()` never raises and never lets a
database problem turn a working answer into a failed tool call - if Lakebase is
unreachable the write is dropped, a warning is logged, and after a few
consecutive failures logging switches itself off for the life of the process so
a dead database cannot add its connection timeout to every single tool call.

This file is duplicated in dashboard/ - each Databricks App deploys from its own
folder, so the apps cannot import a shared module. Keep the copies in sync.
"""

import json
import logging
import os

from lakebase import tracing_db

logger = logging.getLogger(__name__)

TABLE = "agent_tool_calls"

# Set CAPSTONE_TRACING=0 to run the MCP server with no tracing database at all.
TRACING_ENABLED = os.environ.get("CAPSTONE_TRACING", "1").lower() not in ("0", "false", "no")

# After this many consecutive write failures, stop trying until the next restart.
MAX_CONSECUTIVE_FAILURES = 3
_failures = 0
_disabled = False

DDL = [
    f"""
    CREATE TABLE IF NOT EXISTS {TABLE} (
        id            BIGSERIAL PRIMARY KEY,
        session_id    TEXT,
        tool_name     TEXT NOT NULL,
        arguments     JSONB NOT NULL,
        status        TEXT NOT NULL CHECK (status IN ('success', 'error', 'no_data')),
        symbol        TEXT,
        summary       TEXT,
        error_message TEXT,
        duration_ms   INT,
        called_at     TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    f"CREATE INDEX IF NOT EXISTS ix_{TABLE}_called_at ON {TABLE} (called_at DESC)",
    f"CREATE INDEX IF NOT EXISTS ix_{TABLE}_tool_name ON {TABLE} (tool_name)",
    f"CREATE INDEX IF NOT EXISTS ix_{TABLE}_session ON {TABLE} (session_id, called_at)",
]

INSERT_SQL = f"""
    INSERT INTO {TABLE}
        (session_id, tool_name, arguments, status, symbol, summary,
         error_message, duration_ms)
    VALUES (%s, %s, %s::jsonb, %s, %s, %s, %s, %s)
"""


def init_db() -> None:
    """Create the tracing table and its indexes if they are missing."""
    tracing_db.execute_script([(sql, None) for sql in DDL])
    logger.info("Lakebase tracing schema ready (%s)", TABLE)


def tracing_available() -> bool:
    """True when a trace write is worth attempting."""
    return TRACING_ENABLED and not _disabled and tracing_db.is_configured()


def record_call(
    tool_name: str,
    arguments: dict,
    status: str,
    session_id: str | None = None,
    symbol: str | None = None,
    summary: str | None = None,
    error_message: str | None = None,
    duration_ms: int | None = None,
) -> None:
    """
    Write one tool invocation to the tracing database. Swallows every failure.

    The caller is an MCP tool that has already produced its answer; losing the
    trace row is always preferable to failing the answer.
    """
    global _failures, _disabled

    if not tracing_available():
        return

    try:
        tracing_db.execute(
            INSERT_SQL,
            (
                session_id,
                tool_name,
                # Serialised here and cast in the SQL rather than handed to a
                # driver adapter: the MCP server writes through pg8000, which
                # has no psycopg2.extras.Json and stringifies the wrapper into
                # something Postgres rejects as "invalid input syntax for type
                # json". Plain text plus ::jsonb works on both drivers, and this
                # file is shared with the dashboard, which runs psycopg2.
                json.dumps(arguments, default=str),
                status,
                symbol,
                summary,
                error_message,
                duration_ms,
            ),
        )
        _failures = 0
    except Exception as err:
        _failures += 1
        logger.warning(
            "Could not trace tool call %s (%s/%s): %s",
            tool_name,
            _failures,
            MAX_CONSECUTIVE_FAILURES,
            err,
        )
        if _failures >= MAX_CONSECUTIVE_FAILURES:
            _disabled = True
            logger.error(
                "Disabling tracing after %s consecutive failures; the MCP tools "
                "continue to work normally.",
                _failures,
            )


# ------------------------------------------------------- dashboard read side


def recent_calls(
    limit: int = 50, tool_name: str | None = None, status: str | None = None
) -> list[dict]:
    """Most recent tool calls, newest first, optionally filtered."""
    filters, params = [], []
    if tool_name:
        filters.append("tool_name = %s")
        params.append(tool_name)
    if status:
        filters.append("status = %s")
        params.append(status)
    where = f"WHERE {' AND '.join(filters)}" if filters else ""
    params.append(limit)

    return tracing_db.query(
        f"""
        SELECT id, session_id, tool_name, arguments, status, symbol, summary,
               error_message, duration_ms, called_at
        FROM {TABLE}
        {where}
        ORDER BY called_at DESC, id DESC
        LIMIT %s
        """,
        tuple(params),
    )


def stats() -> dict:
    """Headline counters plus per-tool and per-session breakdowns."""
    totals = tracing_db.query_one(
        f"""
        SELECT COUNT(*)                                                AS total_calls,
               COUNT(*) FILTER (WHERE status = 'error')                AS errors,
               COUNT(*) FILTER (WHERE status = 'no_data')              AS no_data,
               COUNT(DISTINCT session_id) FILTER (WHERE session_id IS NOT NULL)
                                                                       AS sessions,
               COUNT(*) FILTER (WHERE called_at > now() - interval '24 hours')
                                                                       AS last_24h,
               ROUND(AVG(duration_ms))                                 AS avg_duration_ms,
               MAX(called_at)                                          AS last_call_at
        FROM {TABLE}
        """
    ) or {}

    by_tool = tracing_db.query(
        f"""
        SELECT tool_name,
               COUNT(*)                                   AS calls,
               COUNT(*) FILTER (WHERE status = 'error')   AS errors,
               COUNT(*) FILTER (WHERE status = 'no_data') AS no_data,
               ROUND(AVG(duration_ms))                    AS avg_duration_ms
        FROM {TABLE}
        GROUP BY tool_name
        ORDER BY calls DESC
        """
    )

    # One row per conversation: how many tools it needed and how long it ran.
    # This is what the session_id column was added for.
    by_session = tracing_db.query(
        f"""
        SELECT session_id,
               COUNT(*)                                 AS calls,
               COUNT(DISTINCT tool_name)                AS distinct_tools,
               COUNT(*) FILTER (WHERE status = 'error') AS errors,
               MIN(called_at)                           AS started_at,
               MAX(called_at)                           AS ended_at
        FROM {TABLE}
        WHERE session_id IS NOT NULL
        GROUP BY session_id
        ORDER BY MAX(called_at) DESC
        LIMIT 20
        """
    )
    return {**totals, "by_tool": by_tool, "by_session": by_session}
