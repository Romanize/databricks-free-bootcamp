"""
Lakebase (Databricks-managed Postgres) connection helper.

Resolution order for the connection URL:
  1. LAKEBASE_URL env var  -> local development (loaded from .env)
  2. Databricks secret scope/key configured in app.yaml -> deployed app

The URL is a standard Postgres URL for a native-password Lakebase role, e.g.
postgresql://role:password@host:5432/databricks_postgres?sslmode=require

The scope/key default to this homework's own secret (homework-2/lakebase-url,
created by setup_secrets.py), so it never shares state with another homework.
"""

import base64
import os
from contextlib import contextmanager
from functools import lru_cache

import psycopg2
from psycopg2.extras import RealDictCursor

SCOPE = os.environ.get("LAKEBASE_SECRET_SCOPE", "homework-2")
KEY = os.environ.get("LAKEBASE_SECRET_KEY", "lakebase-url")


@lru_cache(maxsize=1)
def _lakebase_url() -> str:
    """Return the Lakebase connection URL, cached for the life of the process."""
    url = os.environ.get("LAKEBASE_URL")
    if url:
        return url

    # Imported lazily so local runs with a .env file don't need Databricks auth.
    from databricks.sdk import WorkspaceClient

    secret = WorkspaceClient().secrets.get_secret(scope=SCOPE, key=KEY)
    return base64.b64decode(secret.value).decode("utf-8")


@contextmanager
def get_connection():
    """Yield a psycopg2 connection whose cursors return dicts."""
    conn = psycopg2.connect(_lakebase_url(), cursor_factory=RealDictCursor)
    try:
        yield conn
    finally:
        conn.close()


def query(sql: str, params: tuple | None = None) -> list[dict]:
    """Run a SELECT and return all rows."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()


def query_one(sql: str, params: tuple | None = None) -> dict | None:
    """Run a SELECT and return the first row, or None."""
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params: tuple | None = None) -> dict | None:
    """
    Run an INSERT/UPDATE/DELETE and commit.

    Returns the first row when the statement has a RETURNING clause, else None.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone() if cur.description else None
            conn.commit()
            return row


def execute_script(statements: list[tuple[str, tuple | None]]) -> None:
    """Run several statements inside a single transaction."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            for sql, params in statements:
                cur.execute(sql, params)
        conn.commit()
