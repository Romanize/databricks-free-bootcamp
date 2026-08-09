-- Generated from schema.py / tracing.py - the apps run this same DDL at startup
-- via init_db(), so you never need to execute this by hand. It is here so the
-- schema can be reviewed, diffed, or created up front in the Lakebase SQL editor.
--
-- Regenerate with: python sql/generate.py
-- Database: capstone_tracing (agent tool-call traces)

CREATE TABLE IF NOT EXISTS agent_tool_calls (
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
    );

CREATE INDEX IF NOT EXISTS ix_agent_tool_calls_called_at ON agent_tool_calls (called_at DESC);

CREATE INDEX IF NOT EXISTS ix_agent_tool_calls_tool_name ON agent_tool_calls (tool_name);

CREATE INDEX IF NOT EXISTS ix_agent_tool_calls_session ON agent_tool_calls (session_id, called_at);
