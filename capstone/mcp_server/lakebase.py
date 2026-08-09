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

## Why pg8000 and not psycopg2

psycopg2 is a C extension linked against libpq and OpenSSL. On a Databricks
Runtime that means the wheel's copy of those libraries has to coexist with the
image's own copy, and when the two disagree the failure is an import-time
segfault or a dead kernel rather than a Python traceback - which is exactly what
the notebooks were hitting. pg8000 is **pure Python**: it speaks the Postgres
wire protocol itself, so there is nothing to link and nothing to conflict with.
It installs into any cluster, any runtime version, with no build step.

The cost is that pg8000 is a plainer DB-API driver than psycopg2, so three
conveniences this codebase relied on are re-implemented here, once:

  * `RealDictCursor` -> `_DictCursor` below wraps the driver cursor and turns
    each row into a dict using `cursor.description`.
  * `psycopg2.errors.*` -> pg8000 raises one `DatabaseError` carrying the server
    error fields, so `_translate()` re-raises it as `UniqueViolation` and
    friends, keeping the `err.diag.constraint_name` interface the app's error
    handler already uses.
  * `psycopg2.extras.execute_values` -> a small equivalent below, same
    signature, so the multi-row upserts in schema.py and embeddings.py are
    unchanged.

pg8000 also takes connection *keywords*, not a URL, and an `ssl.SSLContext`
instead of `sslmode`, so `_connect_kwargs()` translates the URL the secret
stores. Everything above that line - every caller - is driver-agnostic.

## This copy has deliberately diverged

`lakebase.py` normally exists three times over - app/, mcp_server/, dashboard/ -
because each Databricks App deploys from its own folder and cannot import a
shared module. The notebooks in ../notebooks/ import **this** copy, so this is
the one that moved to pg8000; app/ and dashboard/ still run psycopg2, which is
fine for them because a Databricks App runs its own container off its own
requirements.txt, not the notebook runtime.

The three copies are therefore no longer interchangeable. The public surface is
identical - `app_db` / `tracing_db` with the same five methods - so anything
written against one still works against another, but do not copy this file over
app/lakebase.py without also porting schema.py, embeddings.py and app.py, which
is the psycopg2-specific part.
"""

import base64
import logging
import os
import ssl
from contextlib import contextmanager
from urllib.parse import parse_qs, unquote, urlparse

import pg8000.dbapi

logger = logging.getLogger(__name__)

SCOPE = os.environ.get("LAKEBASE_SECRET_SCOPE", "capstone")


# ----------------------------------------------------------------- errors

class Diagnostics:
    """
    The psycopg2 `err.diag` interface, over pg8000's error-field dict.

    Only the fields anything here actually reads are exposed; the raw dict is
    on the exception as `.fields` if more is ever needed.
    """

    def __init__(self, fields: dict):
        self.sqlstate = fields.get("C")
        self.message_primary = fields.get("M")
        self.message_detail = fields.get("D")
        self.constraint_name = fields.get("n")
        self.table_name = fields.get("t")
        self.column_name = fields.get("c")


class PostgresError(pg8000.dbapi.DatabaseError):
    """A server-side error, with the SQLSTATE and constraint name broken out."""

    def __init__(self, fields: dict):
        self.fields = fields
        self.sqlstate = fields.get("C")
        self.diag = Diagnostics(fields)
        super().__init__(fields.get("M") or "database error")


class UniqueViolation(PostgresError):
    """SQLSTATE 23505 - the app turns this into a 409 with a friendly message."""


class ForeignKeyViolation(PostgresError):
    """SQLSTATE 23503 - e.g. deleting a holding a report still references."""


class CheckViolation(PostgresError):
    """SQLSTATE 23514 - a CHECK constraint, such as an unknown holding_type."""


class NotNullViolation(PostgresError):
    """SQLSTATE 23502."""


_SQLSTATE_ERRORS = {
    "23505": UniqueViolation,
    "23503": ForeignKeyViolation,
    "23514": CheckViolation,
    "23502": NotNullViolation,
}


def _translate(err: Exception) -> Exception:
    """
    Turn a pg8000 DatabaseError into the typed error the callers expect.

    pg8000 raises `DatabaseError(fields_dict)` for everything the server
    rejects, where the dict is the raw ErrorResponse: 'C' is the SQLSTATE, 'M'
    the message, 'n' the constraint name. Anything that does not look like that
    is handed back untouched.
    """
    fields = err.args[0] if err.args else None
    if not isinstance(fields, dict):
        return err
    return _SQLSTATE_ERRORS.get(fields.get("C"), PostgresError)(fields)


# ------------------------------------------------------------ dict cursor

class _DictCursor:
    """
    A pg8000 cursor that yields dicts and supports `with`.

    pg8000 returns each row as a list and its cursor is not a context manager,
    so both are added here rather than at ~40 call sites. Server errors are
    re-raised through `_translate()` on the way out.
    """

    def __init__(self, cursor):
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
        return False

    def __iter__(self):
        return iter(self.fetchall())

    def __getattr__(self, name):
        # rowcount, arraysize, connection, ... - anything not overridden.
        return getattr(self._cursor, name)

    @property
    def description(self):
        return self._cursor.description

    def execute(self, sql: str, params=None):
        # pg8000 defaults `args` to () and indexes it unconditionally, so an
        # explicit None - which psycopg2 accepts, and which half of the callers
        # pass - has to be normalised here.
        try:
            self._cursor.execute(sql, () if params is None else params)
        except pg8000.dbapi.DatabaseError as err:
            raise _translate(err) from None
        return self

    def executemany(self, sql: str, param_sets):
        try:
            self._cursor.executemany(sql, param_sets)
        except pg8000.dbapi.DatabaseError as err:
            raise _translate(err) from None
        return self

    def _columns(self) -> list[str]:
        description = self._cursor.description or []
        return [column[0] for column in description]

    def fetchone(self) -> dict | None:
        row = self._cursor.fetchone()
        return dict(zip(self._columns(), row)) if row is not None else None

    def fetchall(self) -> list[dict]:
        columns = self._columns()
        return [dict(zip(columns, row)) for row in self._cursor.fetchall()]

    def fetchmany(self, size: int | None = None) -> list[dict]:
        columns = self._columns()
        rows = self._cursor.fetchmany(size) if size is not None else self._cursor.fetchmany()
        return [dict(zip(columns, row)) for row in rows]

    def close(self) -> None:
        try:
            self._cursor.close()
        except pg8000.dbapi.Error:  # already closed with the connection
            pass


class _Connection:
    """Thin wrapper whose `.cursor()` hands back a `_DictCursor`."""

    def __init__(self, connection):
        self._connection = connection

    def __getattr__(self, name):
        # commit, rollback, close, autocommit, ...
        return getattr(self._connection, name)

    def cursor(self) -> _DictCursor:
        return _DictCursor(self._connection.cursor())


# ------------------------------------------------------------ multi-row insert

def execute_values(cursor, sql: str, argslist: list, template: str | None = None,
                   page_size: int = 100) -> None:
    """
    `psycopg2.extras.execute_values` for pg8000, same signature and semantics.

    `sql` carries a single `%s` where the rows go (`INSERT ... VALUES %s`), and
    `template` is one row's placeholder group - which is what lets the callers
    keep per-column casts like `%s::vector` and `%s::timestamptz`. The rows are
    sent `page_size` at a time as one statement each, so a thousand chunks cost
    ten round trips instead of a thousand.
    """
    if not argslist:
        return

    if template is None:
        template = "(" + ", ".join(["%s"] * len(argslist[0])) + ")"

    head, placeholder, tail = sql.partition("%s")
    if not placeholder:
        raise ValueError("execute_values() needs a %s placeholder for the VALUES list")

    for start in range(0, len(argslist), page_size):
        page = argslist[start : start + page_size]
        statement = head + ", ".join([template] * len(page)) + tail
        cursor.execute(statement, [value for row in page for value in row])


# ------------------------------------------------------------------ connect

def _ssl_context(sslmode: str) -> ssl.SSLContext | None:
    """
    Translate libpq's `sslmode` into what pg8000 wants: a context, or None.

    `require` encrypts without checking who is on the other end, which is what
    libpq does too and what Lakebase URLs ask for. `verify-full` is the strict
    one. `prefer`/`allow` have no pg8000 equivalent - the caller retries without
    TLS if the handshake fails.
    """
    if sslmode == "disable":
        return None

    context = ssl.create_default_context()
    if sslmode in ("require", "prefer", "allow"):
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    elif sslmode == "verify-ca":
        context.check_hostname = False
    return context


def _connect_kwargs(url: str) -> dict:
    """Split a Postgres URL into pg8000 connect keywords."""
    parts = urlparse(url)
    if parts.scheme not in ("postgres", "postgresql"):
        raise ValueError(f"Not a Postgres URL: scheme {parts.scheme!r}")

    query = parse_qs(parts.query)
    sslmode = (query.get("sslmode") or ["prefer"])[0]

    kwargs = {
        "user": unquote(parts.username or ""),
        "host": parts.hostname or "localhost",
        "port": parts.port or 5432,
        "database": unquote(parts.path.lstrip("/")) or None,
        "ssl_context": _ssl_context(sslmode),
        "application_name": query.get("application_name", ["capstone"])[0],
    }
    if parts.password:
        kwargs["password"] = unquote(parts.password)
    return kwargs


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

    def connect(self) -> _Connection:
        """Open one connection, honouring `sslmode` from the URL."""
        kwargs = _connect_kwargs(self.url())
        try:
            return _Connection(pg8000.dbapi.connect(**kwargs))
        except pg8000.dbapi.InterfaceError:
            # `prefer`/`allow` mean "TLS if the server does it" - a server built
            # without SSL closes the handshake, and libpq would fall back here.
            if kwargs.get("ssl_context") is None:
                raise
            parts = urlparse(self.url())
            if (parse_qs(parts.query).get("sslmode") or ["prefer"])[0] not in ("prefer", "allow"):
                raise
            logger.warning("%s: server refused TLS, retrying unencrypted (sslmode=prefer)", self.name)
            return _Connection(pg8000.dbapi.connect(**{**kwargs, "ssl_context": None}))

    @contextmanager
    def get_connection(self):
        """Yield a connection whose cursors return dicts."""
        conn = self.connect()
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
