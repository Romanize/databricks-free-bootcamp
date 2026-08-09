"""
Lakebase (Databricks-managed Postgres) connection helpers.

The capstone uses **two databases on one Lakebase instance**:

  * `app`     - holdings, watchlist, news + embeddings, reports, trades.
                Read and written by the Flask app and the MCP server.
  * `tracing` - one row per MCP tool call. Written by the MCP server, read by
                the dashboard app. Kept separate so a burst of agent traffic
                cannot lock or bloat the tables the app serves pages from.

Each has its own connection URL, so each gets its own secret. Resolution order
for either one:

  1. the matching env var (LAKEBASE_URL / LAKEBASE_TRACING_URL) -> local dev
  2. the Databricks secret scope/key configured in app.yaml     -> deployed app

A URL is a standard Postgres URL for a native-password Lakebase role, e.g.
postgresql://role:password@host:5432/capstone?sslmode=require

This file is duplicated in app/ and dashboard/ - each Databricks App deploys
from its own folder, so the three apps cannot import a shared module. Keep the
copies in sync.
"""

import base64
import logging
import os
from contextlib import contextmanager

import psycopg2
from psycopg2.extras import RealDictCursor

logger = logging.getLogger(__name__)

SCOPE = os.environ.get("LAKEBASE_SECRET_SCOPE", "capstone")


class Database:
    """
    One Lakebase database, resolved lazily and cached for the life of the process.

    Lazily matters: the dashboard app never touches the app database and the
    notebooks never touch tracing, so neither should pay for - or fail on - a
    secret lookup it does not need.
    """

    def __init__(self, name: str, url_env: str, key_env: str, default_key: str):
        self.name = name
        self.url_env = url_env
        self.key = os.environ.get(key_env, default_key)
        self._url: str | None = None

    def is_configured(self) -> bool:
        """
        True when a connection URL could plausibly be resolved.

        Lets callers skip optional work (tool-call tracing, most obviously)
        during local runs with no database, instead of failing a secret lookup
        on every single call.
        """
        return bool(
            os.environ.get(self.url_env)
            or os.environ.get("DATABRICKS_HOST")
            or os.environ.get("DATABRICKS_APP_PORT")
        )

    def url(self) -> str:
        if self._url:
            return self._url

        url = os.environ.get(self.url_env)
        if not url:
            # Imported lazily so local runs with a .env file need no Databricks auth.
            from databricks.sdk import WorkspaceClient

            secret = WorkspaceClient().secrets.get_secret(scope=SCOPE, key=self.key)
            url = base64.b64decode(secret.value).decode("utf-8")
            logger.info("Resolved %s database URL from secret %s/%s", self.name, SCOPE, self.key)

        self._url = url
        return url

    @contextmanager
    def get_connection(self):
        """Yield a psycopg2 connection whose cursors return dicts."""
        conn = psycopg2.connect(self.url(), cursor_factory=RealDictCursor)
        try:
            yield conn
        finally:
            conn.close()

    def query(self, sql: str, params: tuple | None = None) -> list[dict]:
        """Run a SELECT and return all rows."""
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchall()

    def query_one(self, sql: str, params: tuple | None = None) -> dict | None:
        """Run a SELECT and return the first row, or None."""
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def execute(self, sql: str, params: tuple | None = None) -> dict | None:
        """
        Run an INSERT/UPDATE/DELETE and commit.

        Returns the first row when the statement has a RETURNING clause, else None.
        """
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                row = cur.fetchone() if cur.description else None
                conn.commit()
                return row

    def execute_script(self, statements: list[tuple[str, tuple | None]]) -> None:
        """Run several statements inside a single transaction."""
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                for sql, params in statements:
                    cur.execute(sql, params)
            conn.commit()


app_db = Database(
    name="app",
    url_env="LAKEBASE_URL",
    key_env="LAKEBASE_SECRET_KEY",
    default_key="lakebase-url",
)

tracing_db = Database(
    name="tracing",
    url_env="LAKEBASE_TRACING_URL",
    key_env="LAKEBASE_TRACING_SECRET_KEY",
    default_key="lakebase-tracing-url",
)
