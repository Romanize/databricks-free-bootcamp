"""
Lakebase schema and queries for the capstone `app` database.

`init_db()` runs at startup in both the Flask app and the MCP server and is
idempotent: it enables pgvector and creates eight tables plus four views.

Everything that reads or writes the app database lives here, so the Flask app,
the MCP server and the notebooks all go through the same statements.

## The shape: one dimension, one fact table

`holdings` says **what exists** - alias, type, ticker, institution. It carries
no values at all. `networth_report` says **what it was worth, and when**: one row
per (report_date, holding), with the quantity, the price, where that price came
from, and the resulting value.

Everything else is a view over those. There is deliberately no report *header*
table: totals are an aggregate, and storing an aggregate next to the rows it
sums is how the two drift apart. `networth_daily` computes it, and
`networth_monthly` picks the last report in each month for the monthly chart.

`UNIQUE (report_date, holding_id)` is what enforces "at most one report per day":
re-submitting today's report updates the same rows.

## Currency

Everything is USD. There is no currency column anywhere - multi-currency means
FX rates, a rate source, a rate date on every line, and a conversion policy for
the aggregates, and none of that earns its place here. USD is an assumption of
the whole system, stated once, rather than a column implying otherwise.

## Why holdings are never hard-deleted

Report lines join to `holdings` for the alias, type and symbol rather than
freezing copies. That keeps the fact table narrow, and the daily SCD2 job in
Unity Catalog preserves what a holding was called at any point in time. The
price of it is that a deleted holding would orphan history, so the foreign key
is `ON DELETE RESTRICT` and `delete_holding()` deactivates instead.

This file is duplicated in app/ - each Databricks App deploys from its own
folder, so the apps cannot import a shared module. Keep the copies in sync.
"""

import hashlib
import logging
import secrets

from psycopg2.extras import Json, execute_values

from embeddings import EMBEDDING_DIM
from lakebase import app_db

logger = logging.getLogger(__name__)

HOLDING_TYPES = ["ticker", "crypto", "cash", "bank", "wallet"]
# Holdings whose value is quantity x market price. The rest carry a value typed
# in when the report is submitted.
PRICED_TYPES = ("ticker", "crypto")
SENTIMENTS = ["positive", "neutral", "negative"]

# Trade lifecycle. `approved` means a human clicked Accept and a confirmation
# key was issued; `executing` means that key has been redeemed and the order is
# with the broker. Both intermediate states exist so every transition can be an
# atomic UPDATE ... WHERE status = <expected>, which is what makes a replayed or
# double-submitted request a no-op rather than a second real order.
TRADE_STATUSES = ["pending", "approved", "executing", "executed", "rejected", "failed"]

# How long an issued confirmation key stays valid.
KEY_TTL_MINUTES = 15

# The only columns that may ever leave this module for a tool payload.
# `confirmation_hash` is deliberately not among them - see issue_confirmation_key().
SAFE_TRADE_COLUMNS = """
    id, symbol, side, quantity, order_type, limit_price, rationale, proposed_by,
    status, alpaca_order_id, filled_price, error_message, created_at,
    decided_at, executed_at,
    (key_expires_at IS NOT NULL AND key_expires_at < now()) AS key_expired
"""

DDL = [
    "CREATE EXTENSION IF NOT EXISTS vector",
    f"""
    CREATE TABLE IF NOT EXISTS holdings (
        id           BIGSERIAL PRIMARY KEY,
        alias        TEXT NOT NULL,
        holding_type TEXT NOT NULL CHECK (holding_type IN
                       ({", ".join(repr(t) for t in HOLDING_TYPES)})),
        symbol       TEXT,
        institution  TEXT,
        notes        TEXT,
        is_active    BOOLEAN NOT NULL DEFAULT true,
        created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
        -- The only rule left on a holding: a priced type has to say what it is.
        -- Quantities and values belong to a report, not to this table.
        CONSTRAINT holdings_symbol_ck CHECK (
            holding_type NOT IN ('ticker', 'crypto') OR symbol IS NOT NULL
        )
    )
    """,
    # Partial: only *active* holdings must have distinct aliases. Holdings are
    # soft-deleted, so without the WHERE clause a deactivated "Apple" would burn
    # that alias permanently and you could never re-add one.
    #
    # Dropped and recreated rather than IF NOT EXISTS, for the same reason the
    # views are: IF NOT EXISTS keeps whatever definition is already there, so an
    # index whose WHERE clause changed would never actually converge on a
    # database created by an earlier version. The table is tiny and the whole
    # script is one transaction, so the rebuild costs nothing.
    "DROP INDEX IF EXISTS ux_holdings_alias",
    """
    CREATE UNIQUE INDEX ux_holdings_alias
    ON holdings (lower(alias)) WHERE is_active
    """,
    "CREATE INDEX IF NOT EXISTS ix_holdings_symbol ON holdings (symbol) WHERE symbol IS NOT NULL",
    """
    CREATE TABLE IF NOT EXISTS watchlist (
        id        BIGSERIAL PRIMARY KEY,
        symbol    TEXT NOT NULL UNIQUE,
        reason    TEXT,
        is_active BOOLEAN NOT NULL DEFAULT true,
        added_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ticker_news (
        id            TEXT PRIMARY KEY,
        title         TEXT NOT NULL,
        description   TEXT,
        -- title + description, i.e. exactly the text that gets chunked and
        -- embedded. Stored rather than recomputed so the embedding job and any
        -- later audit agree on what was vectorized.
        embed_text    TEXT NOT NULL,
        article_url   TEXT,
        publisher     TEXT,
        author        TEXT,
        tickers       TEXT[] NOT NULL DEFAULT '{}',
        keywords      TEXT[] NOT NULL DEFAULT '{}',
        published_utc TIMESTAMPTZ,
        payload       JSONB NOT NULL,
        synced_at     TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_ticker_news_tickers ON ticker_news USING gin (tickers)",
    "CREATE INDEX IF NOT EXISTS ix_ticker_news_published ON ticker_news (published_utc DESC)",
    f"""
    CREATE TABLE IF NOT EXISTS tickers_news_embeddings (
        id          TEXT PRIMARY KEY,
        article_id  TEXT NOT NULL REFERENCES ticker_news(id) ON DELETE CASCADE,
        -- Copied from the article so a filtered vector search ("news about
        -- AAPL") stays a single indexed scan instead of a join back to
        -- ticker_news before the ORDER BY can use the HNSW index.
        tickers     TEXT[] NOT NULL DEFAULT '{{}}',
        chunk_index INT NOT NULL,
        chunk_text  TEXT NOT NULL,
        embedding   VECTOR({EMBEDDING_DIM}) NOT NULL,
        model_name  TEXT NOT NULL,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (article_id, chunk_index)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_news_embeddings_vector
    ON tickers_news_embeddings USING hnsw (embedding vector_cosine_ops)
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_news_embeddings_tickers
    ON tickers_news_embeddings USING gin (tickers)
    """,
    f"""
    CREATE TABLE IF NOT EXISTS ticker_sentiments (
        id                  BIGSERIAL PRIMARY KEY,
        symbol              TEXT NOT NULL,
        article_id          TEXT NOT NULL REFERENCES ticker_news(id) ON DELETE CASCADE,
        sentiment           TEXT NOT NULL CHECK (sentiment IN
                              ({", ".join(repr(s) for s in SENTIMENTS)})),
        sentiment_reasoning TEXT,
        published_utc       TIMESTAMPTZ,
        created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (symbol, article_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_ticker_sentiments_symbol ON ticker_sentiments (symbol, published_utc DESC)",
    """
    CREATE TABLE IF NOT EXISTS investment_plan (
        id                     BIGSERIAL PRIMARY KEY,
        name                   TEXT NOT NULL,
        goal_amount            NUMERIC(20, 2) NOT NULL,
        expected_annual_rate   NUMERIC(6, 4) NOT NULL,
        years                  INT NOT NULL CHECK (years > 0 AND years <= 80),
        expected_inflation     NUMERIC(6, 4) NOT NULL DEFAULT 0.03,
        monthly_contribution   NUMERIC(20, 2) NOT NULL DEFAULT 0,
        annual_contribution    NUMERIC(20, 2) NOT NULL DEFAULT 0,
        start_date             DATE NOT NULL DEFAULT CURRENT_DATE,
        is_active              BOOLEAN NOT NULL DEFAULT false,
        -- Plans can now be written by the agent as well as the UI. Recording
        -- which makes "did I set this up or did the assistant?" answerable.
        created_by             TEXT NOT NULL DEFAULT 'user'
                               CHECK (created_by IN ('user', 'agent')),
        created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at             TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    # At most one active plan. The chart and the MCP projection tool both assume
    # "the plan" is unambiguous, so the database guarantees it.
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_investment_plan_active
    ON investment_plan ((is_active)) WHERE is_active
    """,
    # ---- the fact table. One row per holding per report date.
    """
    CREATE TABLE IF NOT EXISTS networth_report (
        id           BIGSERIAL PRIMARY KEY,
        report_date  DATE NOT NULL DEFAULT CURRENT_DATE,
        -- RESTRICT, not CASCADE: report lines join back to holdings for their
        -- label, so a hard delete would orphan history. delete_holding()
        -- deactivates instead, and this constraint is what enforces that.
        holding_id   BIGINT NOT NULL REFERENCES holdings(id) ON DELETE RESTRICT,
        quantity     NUMERIC(20, 8),
        price        NUMERIC(20, 8),
        price_as_of  TIMESTAMPTZ,
        price_source TEXT NOT NULL DEFAULT 'manual',
        value        NUMERIC(20, 2) NOT NULL,
        notes        TEXT,
        created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
        -- At most one report per day: re-submitting updates these same rows.
        UNIQUE (report_date, holding_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_networth_report_date ON networth_report (report_date DESC)",
    "CREATE INDEX IF NOT EXISTS ix_networth_report_holding ON networth_report (holding_id)",
    f"""
    CREATE TABLE IF NOT EXISTS pending_trades (
        id                BIGSERIAL PRIMARY KEY,
        symbol            TEXT NOT NULL,
        side              TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
        quantity          NUMERIC(20, 8) NOT NULL CHECK (quantity > 0),
        order_type        TEXT NOT NULL DEFAULT 'market'
                          CHECK (order_type IN ('market', 'limit')),
        limit_price       NUMERIC(20, 8),
        rationale         TEXT,
        proposed_by       TEXT NOT NULL DEFAULT 'agent'
                          CHECK (proposed_by IN ('agent', 'user')),
        status            TEXT NOT NULL DEFAULT 'pending' CHECK (status IN
                            ({", ".join(repr(s) for s in TRADE_STATUSES)})),
        -- SHA-256 of the confirmation key, never the key itself. Written when a
        -- human clicks Accept, cleared the moment it is redeemed. See
        -- issue_confirmation_key() for why it is hashed and single-use.
        confirmation_hash TEXT,
        key_expires_at    TIMESTAMPTZ,
        alpaca_order_id   TEXT,
        filled_price      NUMERIC(20, 8),
        error_message     TEXT,
        created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
        decided_at        TIMESTAMPTZ,
        executed_at       TIMESTAMPTZ,
        CONSTRAINT pending_trades_limit_ck CHECK (
            order_type <> 'limit' OR limit_price IS NOT NULL
        )
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_pending_trades_status ON pending_trades (status, created_at DESC)",
    # ---- views. Dropped and recreated rather than CREATE OR REPLACE, because
    # replacing a view whose column list changed is an error and this DDL runs on
    # every app start. execute_script wraps the lot in one transaction, so there
    # is no window in which a view is missing.
    "DROP VIEW IF EXISTS networth_monthly",
    "DROP VIEW IF EXISTS networth_daily",
    "DROP VIEW IF EXISTS networth_report_lines",
    "DROP VIEW IF EXISTS ticker_sentiment_daily",
    """
    CREATE VIEW networth_report_lines AS
    SELECT r.id, r.report_date, r.holding_id,
           h.alias, h.holding_type, h.symbol, h.institution,
           r.quantity, r.price, r.price_as_of, r.price_source, r.value, r.notes,
           r.created_at, r.updated_at
    FROM networth_report r
    JOIN holdings h ON h.id = r.holding_id
    """,
    """
    CREATE VIEW networth_daily AS
    SELECT report_date,
           SUM(value)                                                   AS total_value,
           COALESCE(SUM(value) FILTER
               (WHERE holding_type IN ('ticker', 'crypto')), 0)         AS invested_value,
           COALESCE(SUM(value) FILTER
               (WHERE holding_type NOT IN ('ticker', 'crypto')), 0)     AS cash_value,
           COUNT(*)                                                     AS holdings_count,
           MAX(updated_at)                                              AS updated_at
    FROM networth_report_lines
    GROUP BY report_date
    """,
    # One row per month, taking the LAST report of that month - that is the
    # month-end reading, and it is what the monthly chart plots. A mid-month
    # report should not become the month's number when a later one exists.
    """
    CREATE VIEW networth_monthly AS
    SELECT DISTINCT ON (date_trunc('month', report_date))
           date_trunc('month', report_date)::date AS month,
           report_date,
           total_value, invested_value, cash_value, holdings_count
    FROM networth_daily
    ORDER BY date_trunc('month', report_date), report_date DESC
    """,
    """
    CREATE VIEW ticker_sentiment_daily AS
    SELECT symbol,
           date_trunc('day', COALESCE(published_utc, created_at))::date AS day,
           COUNT(*)                                          AS articles,
           COUNT(*) FILTER (WHERE sentiment = 'positive')    AS positive,
           COUNT(*) FILTER (WHERE sentiment = 'neutral')     AS neutral,
           COUNT(*) FILTER (WHERE sentiment = 'negative')    AS negative,
           ROUND(
               (COUNT(*) FILTER (WHERE sentiment = 'positive')
                - COUNT(*) FILTER (WHERE sentiment = 'negative'))::numeric
               / NULLIF(COUNT(*), 0), 3
           )                                                 AS score
    FROM ticker_sentiments
    GROUP BY symbol, day
    """,
]


def init_db() -> None:
    """Create the extension, tables, indexes and views if they are missing."""
    app_db.execute_script([(sql, None) for sql in DDL])
    logger.info("Lakebase app schema ready (vector dim %s)", EMBEDDING_DIM)


# ------------------------------------------------------------------- holdings


def list_holdings(active_only: bool = True) -> list[dict]:
    """All holdings, priced ones first so the UI groups naturally."""
    where = "WHERE is_active" if active_only else ""
    return app_db.query(
        f"""
        SELECT id, alias, holding_type, symbol, institution, notes,
               is_active, created_at, updated_at
        FROM holdings
        {where}
        ORDER BY (holding_type IN ('ticker', 'crypto')) DESC, alias
        """
    )


def get_holding(holding_id: int) -> dict | None:
    return app_db.query_one("SELECT * FROM holdings WHERE id = %s", (holding_id,))


def find_holding_by_symbol(symbol: str) -> dict | None:
    """The active holding for a ticker, if the user tracks one."""
    return app_db.query_one(
        "SELECT * FROM holdings WHERE is_active AND upper(symbol) = upper(%s) "
        "AND holding_type IN ('ticker', 'crypto') LIMIT 1",
        (symbol,),
    )


def create_holding(data: dict) -> dict:
    return app_db.execute(
        """
        INSERT INTO holdings (alias, holding_type, symbol, institution, notes)
        VALUES (%s, %s, %s, %s, %s)
        RETURNING *
        """,
        (
            data["alias"],
            data["holding_type"],
            data.get("symbol") or None,
            data.get("institution"),
            data.get("notes"),
        ),
    )


def update_holding(holding_id: int, data: dict) -> dict | None:
    return app_db.execute(
        """
        UPDATE holdings SET
            alias       = COALESCE(%s, alias),
            symbol      = COALESCE(%s, symbol),
            institution = COALESCE(%s, institution),
            notes       = COALESCE(%s, notes),
            is_active   = COALESCE(%s, is_active),
            updated_at  = now()
        WHERE id = %s
        RETURNING *
        """,
        (
            data.get("alias"),
            data.get("symbol"),
            data.get("institution"),
            data.get("notes"),
            data.get("is_active"),
            holding_id,
        ),
    )


def delete_holding(holding_id: int) -> bool:
    """
    Soft delete.

    A hard delete is refused outright by the fact table's ON DELETE RESTRICT
    once the holding appears in any report, and would orphan the labels on those
    lines even if it were not. Deactivating keeps every past report readable.
    """
    row = app_db.execute(
        "UPDATE holdings SET is_active = false, updated_at = now() WHERE id = %s RETURNING id",
        (holding_id,),
    )
    return row is not None


# ------------------------------------------------------------------ watchlist


def list_watchlist(active_only: bool = True) -> list[dict]:
    where = "WHERE is_active" if active_only else ""
    return app_db.query(
        f"SELECT id, symbol, reason, is_active, added_at FROM watchlist {where} "
        "ORDER BY added_at DESC"
    )


def add_to_watchlist(symbol: str, reason: str | None = None) -> dict:
    """Idempotent: re-adding a symbol reactivates it and refreshes the reason."""
    return app_db.execute(
        """
        INSERT INTO watchlist (symbol, reason)
        VALUES (%s, %s)
        ON CONFLICT (symbol) DO UPDATE SET
            reason    = COALESCE(EXCLUDED.reason, watchlist.reason),
            is_active = true
        RETURNING *
        """,
        (symbol.upper(), reason),
    )


def remove_from_watchlist(symbol: str) -> bool:
    row = app_db.execute(
        "UPDATE watchlist SET is_active = false WHERE symbol = %s RETURNING id",
        (symbol.upper(),),
    )
    return row is not None


def tracked_symbols() -> list[str]:
    """
    Symbols the news job should fetch: the active watchlist plus everything
    currently held. You want news about what you own whether or not you
    remembered to also watch it.
    """
    rows = app_db.query(
        """
        SELECT symbol FROM watchlist WHERE is_active AND symbol IS NOT NULL
        UNION
        SELECT symbol FROM holdings
        WHERE is_active AND symbol IS NOT NULL AND holding_type IN ('ticker', 'crypto')
        ORDER BY symbol
        """
    )
    return [row["symbol"] for row in rows]


# ----------------------------------------------------------------------- news


def latest_article_time(symbol: str):
    """
    The newest `published_utc` already stored for one ticker.

    This is what makes the 2-hourly job incremental: it becomes the
    `published_utc.gt` filter on the next Massive request, so each run pulls
    only what appeared since the last one instead of re-fetching the same 20
    articles every two hours and burning the free tier's quota.
    """
    row = app_db.query_one(
        "SELECT MAX(published_utc) AS newest FROM ticker_news WHERE %s = ANY(tickers)",
        (symbol.upper(),),
    )
    return row["newest"] if row else None


UPSERT_NEWS_SQL = """
    INSERT INTO ticker_news (
        id, title, description, embed_text, article_url, publisher, author,
        tickers, keywords, published_utc, payload, synced_at
    ) VALUES %s
    ON CONFLICT (id) DO UPDATE SET
        title         = EXCLUDED.title,
        description   = EXCLUDED.description,
        embed_text    = EXCLUDED.embed_text,
        article_url   = EXCLUDED.article_url,
        publisher     = EXCLUDED.publisher,
        author        = EXCLUDED.author,
        tickers       = EXCLUDED.tickers,
        keywords      = EXCLUDED.keywords,
        published_utc = EXCLUDED.published_utc,
        payload       = EXCLUDED.payload,
        -- Only bump synced_at when the embedded text actually changed, so a
        -- re-run of the 2-hour job re-embeds nothing it already has.
        synced_at     = CASE
            WHEN ticker_news.embed_text IS DISTINCT FROM EXCLUDED.embed_text
            THEN now() ELSE ticker_news.synced_at
        END
"""

UPSERT_NEWS_TEMPLATE = "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::timestamptz, %s, now())"


def upsert_articles(articles: list[dict]) -> int:
    """Insert or refresh ticker_news rows; returns the number written."""
    if not articles:
        return 0

    values = [
        (
            article["id"],
            article["title"],
            article.get("description"),
            article["embed_text"],
            article.get("article_url"),
            article.get("publisher"),
            article.get("author"),
            article.get("tickers") or [],
            article.get("keywords") or [],
            article.get("published_utc"),
            Json(article.get("payload") or {}),
        )
        for article in articles
    ]
    with app_db.get_connection() as conn:
        with conn.cursor() as cur:
            execute_values(
                cur, UPSERT_NEWS_SQL, values, template=UPSERT_NEWS_TEMPLATE, page_size=100
            )
        conn.commit()
    return len(values)


UPSERT_SENTIMENT_SQL = """
    INSERT INTO ticker_sentiments
        (symbol, article_id, sentiment, sentiment_reasoning, published_utc)
    VALUES %s
    ON CONFLICT (symbol, article_id) DO UPDATE SET
        sentiment           = EXCLUDED.sentiment,
        sentiment_reasoning = EXCLUDED.sentiment_reasoning,
        published_utc       = EXCLUDED.published_utc
"""

UPSERT_SENTIMENT_TEMPLATE = "(%s, %s, %s, %s, %s::timestamptz)"


def upsert_sentiments(rows: list[dict]) -> int:
    """
    Store Massive's per-ticker sentiment verbatim.

    Sentiment is *not* recomputed locally. It arrives already attributed to a
    ticker in the article's `insights` array, which means the number in
    ticker_sentiments is always traceable to a specific article rather than to
    a model's opinion of one.
    """
    if not rows:
        return 0

    values = [
        (
            row["symbol"],
            row["article_id"],
            row["sentiment"],
            row.get("sentiment_reasoning"),
            row.get("published_utc"),
        )
        for row in rows
    ]
    with app_db.get_connection() as conn:
        with conn.cursor() as cur:
            execute_values(
                cur, UPSERT_SENTIMENT_SQL, values,
                template=UPSERT_SENTIMENT_TEMPLATE, page_size=200,
            )
        conn.commit()
    return len(values)


def sentiment_summary(symbols: list[str] | None = None, days: int = 30) -> list[dict]:
    """Per-symbol sentiment counts over a window, most-covered symbol first."""
    where = ["published_utc > now() - make_interval(days => %s)"]
    params: list = [days]
    if symbols:
        where.append("symbol = ANY(%s)")
        params.append([s.upper() for s in symbols])

    return app_db.query(
        f"""
        SELECT symbol,
               COUNT(*)                                       AS articles,
               COUNT(*) FILTER (WHERE sentiment = 'positive') AS positive,
               COUNT(*) FILTER (WHERE sentiment = 'neutral')  AS neutral,
               COUNT(*) FILTER (WHERE sentiment = 'negative') AS negative,
               ROUND(
                   (COUNT(*) FILTER (WHERE sentiment = 'positive')
                    - COUNT(*) FILTER (WHERE sentiment = 'negative'))::numeric
                   / NULLIF(COUNT(*), 0), 3
               )                                              AS score,
               MAX(published_utc)                             AS latest_article_at
        FROM ticker_sentiments
        WHERE {" AND ".join(where)}
        GROUP BY symbol
        ORDER BY articles DESC, symbol
        """,
        tuple(params),
    )


def sentiment_timeline(symbol: str, days: int = 90) -> list[dict]:
    """Daily sentiment score for one symbol, for the app's line chart."""
    return app_db.query(
        """
        SELECT day, articles, positive, neutral, negative, score
        FROM ticker_sentiment_daily
        WHERE symbol = %s AND day > CURRENT_DATE - %s
        ORDER BY day
        """,
        (symbol.upper(), days),
    )


ARTICLE_FILTER = "WHERE %s = ANY(tickers)"


def recent_articles(
    symbol: str | None = None, limit: int = 20, offset: int = 0
) -> list[dict]:
    """
    One page of stored articles, newest first, optionally filtered to a ticker.

    `id` breaks ties in the ORDER BY: `published_utc` is not unique - a job run
    can store a dozen articles sharing a timestamp - and without a tiebreaker
    Postgres is free to order those rows differently on each query, which shows
    up as a row appearing on both page 1 and page 2.
    """
    where = ARTICLE_FILTER if symbol else ""
    params = ([symbol.upper()] if symbol else []) + [limit, offset]
    return app_db.query(
        f"""
        SELECT n.id, n.title, n.description, n.article_url, n.publisher,
               n.published_utc, n.tickers,
               (SELECT COUNT(*) FROM tickers_news_embeddings e WHERE e.article_id = n.id)
                   AS chunk_count
        FROM ticker_news n
        {where}
        ORDER BY n.published_utc DESC NULLS LAST, n.id DESC
        LIMIT %s OFFSET %s
        """,
        tuple(params),
    )


def count_articles(symbol: str | None = None) -> int:
    """How many articles the same filter matches - the pager's denominator."""
    where = ARTICLE_FILTER if symbol else ""
    params = (symbol.upper(),) if symbol else ()
    return app_db.query_one(
        f"SELECT COUNT(*) AS total FROM ticker_news {where}", params
    )["total"]


# -------------------------------------------------------------- vector search

SEARCH_SQL = """
    SELECT n.id, n.title, n.article_url, n.publisher, n.published_utc, n.tickers,
           e.chunk_index, e.chunk_text,
           1 - (e.embedding <=> %s::vector) AS similarity,
           s.sentiment, s.sentiment_reasoning
    FROM tickers_news_embeddings e
    JOIN ticker_news n ON n.id = e.article_id
    LEFT JOIN LATERAL (
        SELECT sentiment, sentiment_reasoning
        FROM ticker_sentiments t
        WHERE t.article_id = n.id AND (%s::text IS NULL OR t.symbol = %s::text)
        LIMIT 1
    ) s ON TRUE
    {where}
    ORDER BY e.embedding <=> %s::vector
    LIMIT %s
"""


def search_news(vector: str, symbol: str | None, top_k: int, days: int | None = None) -> list[dict]:
    """
    Rank news chunks by cosine similarity, optionally filtered to one ticker
    and/or a recency window.

    `vector` is already a pgvector literal from embeddings.embed_query().
    """
    filters = []
    params: list = [vector, symbol.upper() if symbol else None, symbol.upper() if symbol else None]

    if symbol:
        filters.append("e.tickers @> ARRAY[%s]::text[]")
        params.append(symbol.upper())
    if days:
        filters.append("n.published_utc > now() - make_interval(days => %s)")
        params.append(days)

    where = f"WHERE {' AND '.join(filters)}" if filters else ""
    params.extend([vector, top_k])
    return app_db.query(SEARCH_SQL.format(where=where), tuple(params))


def has_embeddings() -> bool:
    """False when the news pipeline has never run - drives the `no_data` status."""
    return bool(app_db.query_one("SELECT 1 AS ok FROM tickers_news_embeddings LIMIT 1"))


# ----------------------------------------------------------- investment plans


def list_plans() -> list[dict]:
    return app_db.query("SELECT * FROM investment_plan ORDER BY is_active DESC, created_at DESC")


def active_plan() -> dict | None:
    return app_db.query_one("SELECT * FROM investment_plan WHERE is_active")


def get_plan(plan_id: int) -> dict | None:
    return app_db.query_one("SELECT * FROM investment_plan WHERE id = %s", (plan_id,))


def create_plan(data: dict) -> dict:
    """
    Create a plan, always inactive. Activation is a separate, explicit step so
    that writing a plan can never silently replace the one being charted.
    """
    return app_db.execute(
        """
        INSERT INTO investment_plan (name, goal_amount, expected_annual_rate, years,
                                     expected_inflation, monthly_contribution,
                                     annual_contribution, start_date, is_active,
                                     created_by)
        VALUES (%s, %s, %s, %s, %s, %s, %s, COALESCE(%s, CURRENT_DATE), false, %s)
        RETURNING *
        """,
        (
            data["name"],
            data["goal_amount"],
            data["expected_annual_rate"],
            data["years"],
            data.get("expected_inflation", 0.03),
            data.get("monthly_contribution", 0),
            data.get("annual_contribution", 0),
            data.get("start_date"),
            data.get("created_by", "user"),
        ),
    )


def update_plan(plan_id: int, data: dict) -> dict | None:
    """Patch a plan. Only the fields actually supplied are changed."""
    return app_db.execute(
        """
        UPDATE investment_plan SET
            name                 = COALESCE(%s, name),
            goal_amount          = COALESCE(%s, goal_amount),
            expected_annual_rate = COALESCE(%s, expected_annual_rate),
            years                = COALESCE(%s, years),
            expected_inflation   = COALESCE(%s, expected_inflation),
            monthly_contribution = COALESCE(%s, monthly_contribution),
            annual_contribution  = COALESCE(%s, annual_contribution),
            updated_at           = now()
        WHERE id = %s
        RETURNING *
        """,
        (
            data.get("name"),
            data.get("goal_amount"),
            data.get("expected_annual_rate"),
            data.get("years"),
            data.get("expected_inflation"),
            data.get("monthly_contribution"),
            data.get("annual_contribution"),
            plan_id,
        ),
    )


def activate_plan(plan_id: int) -> dict | None:
    """Make one plan active; the partial unique index needs the others cleared first."""
    with app_db.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE investment_plan SET is_active = false WHERE is_active")
            cur.execute(
                "UPDATE investment_plan SET is_active = true, updated_at = now() "
                "WHERE id = %s RETURNING *",
                (plan_id,),
            )
            row = cur.fetchone()
        conn.commit()
    return row


def delete_plan(plan_id: int) -> bool:
    row = app_db.execute("DELETE FROM investment_plan WHERE id = %s RETURNING id", (plan_id,))
    return row is not None


# ------------------------------------------------------------------ net worth


def latest_report_date():
    row = app_db.query_one("SELECT MAX(report_date) AS report_date FROM networth_report")
    return row["report_date"] if row else None


def latest_report() -> dict | None:
    """
    The most recent report's totals, from the `networth_daily` view.

    There is no stored header row - this is recomputed from the lines on every
    read, which is exactly why the total can never disagree with them.
    """
    return app_db.query_one("SELECT * FROM networth_daily ORDER BY report_date DESC LIMIT 1")


def report_on(report_date) -> dict | None:
    return app_db.query_one("SELECT * FROM networth_daily WHERE report_date = %s", (report_date,))


def report_lines(report_date) -> list[dict]:
    """Every holding's line for one report date, largest value first."""
    return app_db.query(
        """
        SELECT id, report_date, holding_id, alias, holding_type, symbol, institution,
               quantity, price, price_as_of, price_source, value, notes
        FROM networth_report_lines
        WHERE report_date = %s
        ORDER BY value DESC
        """,
        (report_date,),
    )


def latest_report_lines() -> list[dict]:
    report_date = latest_report_date()
    return report_lines(report_date) if report_date else []


def report_history(limit: int = 365) -> list[dict]:
    """Daily totals, oldest first so the growth chart plots without reversing."""
    return app_db.query(
        """
        SELECT * FROM (
            SELECT report_date, total_value, invested_value, cash_value, holdings_count
            FROM networth_daily
            ORDER BY report_date DESC
            LIMIT %s
        ) recent
        ORDER BY report_date
        """,
        (limit,),
    )


def monthly_history(limit: int = 60) -> list[dict]:
    """
    Month-end totals, oldest first.

    One point per month, taking the last report submitted in that month, so a
    mid-month reading never becomes the month's number when a later one exists.
    """
    return app_db.query(
        """
        SELECT * FROM (
            SELECT month, report_date, total_value, invested_value, cash_value, holdings_count
            FROM networth_monthly
            ORDER BY month DESC
            LIMIT %s
        ) recent
        ORDER BY month
        """,
        (limit,),
    )


def days_since_last_report() -> int | None:
    """None when no report exists at all - the UI treats that as 'overdue too'."""
    row = app_db.query_one(
        "SELECT (CURRENT_DATE - MAX(report_date)) AS days FROM networth_report"
    )
    return row["days"] if row and row["days"] is not None else None


def distribution(report_date) -> list[dict]:
    """Value by holding type for one report, for the distribution doughnut."""
    return app_db.query(
        """
        SELECT holding_type, SUM(value) AS value, COUNT(*) AS holdings
        FROM networth_report_lines
        WHERE report_date = %s
        GROUP BY holding_type
        ORDER BY value DESC
        """,
        (report_date,),
    )


def latest_line_by_holding() -> dict:
    """
    The most recent line for every holding, keyed by holding_id.

    This is what a new report is prefilled from: last month's quantity for a
    ticker you have not traded, last month's balance for a bank account whose
    statement has not arrived yet. It is also how the MCP tool re-values the
    portfolio at live prices, since quantities no longer live on `holdings`.
    """
    rows = app_db.query(
        """
        SELECT DISTINCT ON (holding_id)
               holding_id, report_date, quantity, price, value, price_source, notes
        FROM networth_report
        ORDER BY holding_id, report_date DESC
        """
    )
    return {row["holding_id"]: row for row in rows}


UPSERT_LINE_SQL = """
    INSERT INTO networth_report
        (report_date, holding_id, quantity, price, price_as_of, price_source, value, notes)
    VALUES %s
    ON CONFLICT (report_date, holding_id) DO UPDATE SET
        quantity     = EXCLUDED.quantity,
        price        = EXCLUDED.price,
        price_as_of  = EXCLUDED.price_as_of,
        price_source = EXCLUDED.price_source,
        value        = EXCLUDED.value,
        notes        = EXCLUDED.notes,
        updated_at   = now()
"""

UPSERT_LINE_TEMPLATE = "(%s, %s, %s, %s, %s::timestamptz, %s, %s, %s)"


def write_report(report_date, lines: list[dict], replace: bool = True) -> dict:
    """
    Write one report: upsert every line, in a single transaction.

    `replace=True` also deletes lines for that date that are not in `lines`, so
    a holding you dropped disappears from the report instead of lingering at its
    old value. Re-submitting the same date updates in place - the
    UNIQUE (report_date, holding_id) constraint is what makes that safe, and is
    the whole of the "at most one report per day" rule.
    """
    if not lines:
        raise ValueError("A report needs at least one line.")

    values = [
        (
            report_date,
            line["holding_id"],
            line.get("quantity"),
            line.get("price"),
            line.get("price_as_of"),
            line.get("price_source") or "manual",
            round(float(line["value"]), 2),
            line.get("notes"),
        )
        for line in lines
    ]

    with app_db.get_connection() as conn:
        with conn.cursor() as cur:
            execute_values(
                cur, UPSERT_LINE_SQL, values, template=UPSERT_LINE_TEMPLATE, page_size=200
            )
            if replace:
                cur.execute(
                    "DELETE FROM networth_report WHERE report_date = %s AND holding_id <> ALL(%s)",
                    (report_date, [line["holding_id"] for line in lines]),
                )
        conn.commit()

    report = report_on(report_date)
    logger.info(
        "Wrote report %s: %s lines, total %s",
        report_date, len(values), report["total_value"] if report else "?",
    )
    return report


# ------------------------------------------------------------- pending trades


def create_pending_trade(data: dict) -> dict:
    """
    Queue a proposal.

    No confirmation key exists at this point - one is only minted when a human
    clicks Accept, so there is nothing here for the agent to read back and
    confirm itself with.
    """
    return app_db.execute(
        f"""
        INSERT INTO pending_trades (symbol, side, quantity, order_type, limit_price,
                                    rationale, proposed_by)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        RETURNING {SAFE_TRADE_COLUMNS}
        """,
        (
            data["symbol"].upper(),
            data["side"],
            data["quantity"],
            data.get("order_type", "market"),
            data.get("limit_price"),
            data.get("rationale"),
            data.get("proposed_by", "agent"),
        ),
    )


def list_trades(status: str | None = None, limit: int = 50) -> list[dict]:
    """Trades, newest first. Never returns the confirmation hash."""
    where = "WHERE status = %s" if status else ""
    params = ([status] if status else []) + [limit]
    return app_db.query(
        f"SELECT {SAFE_TRADE_COLUMNS} FROM pending_trades {where} "
        "ORDER BY created_at DESC LIMIT %s",
        tuple(params),
    )


def get_trade(trade_id: int) -> dict | None:
    """One trade. Never returns the confirmation hash."""
    return app_db.query_one(
        f"SELECT {SAFE_TRADE_COLUMNS} FROM pending_trades WHERE id = %s", (trade_id,)
    )


def _hash_key(key: str) -> str:
    return hashlib.sha256((key or "").encode("utf-8")).hexdigest()


def issue_confirmation_key(trade_id: int) -> tuple[dict, str] | None:
    """
    Approve a proposal and mint a single-use confirmation key for it.

    **This is the human-in-the-loop boundary.** Only the Flask app calls it, and
    only from a route a person triggered by clicking Accept. The plaintext key
    is returned to that caller once and never stored: the row keeps only a
    SHA-256 of it, so even something that can read the whole database cannot
    replay it.

    Returns (trade, plaintext_key), or None when the trade was not pending -
    which is how a double-click loses the race rather than minting a second key.
    """
    key = secrets.token_urlsafe(24)
    trade = app_db.execute(
        f"""
        UPDATE pending_trades SET
            status            = 'approved',
            confirmation_hash = %s,
            key_expires_at    = now() + make_interval(mins => %s),
            decided_at        = now(),
            error_message     = NULL
        WHERE id = %s AND status = 'pending'
        RETURNING {SAFE_TRADE_COLUMNS}
        """,
        (_hash_key(key), KEY_TTL_MINUTES, trade_id),
    )
    if not trade:
        return None
    logger.info("Issued confirmation key for trade #%s (ttl %s min)", trade_id, KEY_TTL_MINUTES)
    return trade, key


def redeem_confirmation_key(trade_id: int, key: str) -> dict | None:
    """
    Redeem a key and claim the trade for execution, atomically.

    The single UPDATE is the entire guard: it moves `approved -> executing` only
    if the hash matches, the key has not expired and nobody has claimed it yet,
    and it clears the hash in the same statement. A replayed key therefore
    matches no row the second time, so a key sitting in a conversation history
    cannot produce a second order.

    Returns the claimed trade, or None if the key is wrong, expired or spent.
    """
    trade = app_db.execute(
        f"""
        UPDATE pending_trades SET
            status            = 'executing',
            confirmation_hash = NULL,
            key_expires_at    = NULL
        WHERE id = %s
          AND status = 'approved'
          AND confirmation_hash = %s
          AND key_expires_at > now()
        RETURNING {SAFE_TRADE_COLUMNS}
        """,
        (trade_id, _hash_key(key)),
    )
    if not trade:
        logger.warning("Rejected confirmation key for trade #%s", trade_id)
    return trade


def finalize_trade(
    trade_id: int,
    status: str,
    alpaca_order_id: str | None = None,
    filled_price=None,
    error_message: str | None = None,
) -> dict | None:
    """Close out a claimed trade: executing -> executed or failed."""
    return app_db.execute(
        f"""
        UPDATE pending_trades SET
            status          = %s,
            alpaca_order_id = COALESCE(%s, alpaca_order_id),
            filled_price    = COALESCE(%s, filled_price),
            error_message   = %s,
            executed_at     = now()
        WHERE id = %s AND status = 'executing'
        RETURNING {SAFE_TRADE_COLUMNS}
        """,
        (status, alpaca_order_id, filled_price, error_message, trade_id),
    )


def reject_trade(trade_id: int, reason: str | None = None) -> dict | None:
    """Decline a proposal. Only a pending one can be rejected."""
    return app_db.execute(
        f"""
        UPDATE pending_trades SET
            status            = 'rejected',
            confirmation_hash = NULL,
            key_expires_at    = NULL,
            error_message     = %s,
            decided_at        = now()
        WHERE id = %s AND status = 'pending'
        RETURNING {SAFE_TRADE_COLUMNS}
        """,
        (reason or "Rejected by the user.", trade_id),
    )


def stats() -> dict:
    """Counters for the app's status bar."""
    return app_db.query_one(
        """
        SELECT (SELECT COUNT(*) FROM holdings WHERE is_active)              AS holdings,
               (SELECT COUNT(*) FROM watchlist WHERE is_active)             AS watchlist,
               (SELECT COUNT(*) FROM ticker_news)                           AS articles,
               (SELECT COUNT(*) FROM tickers_news_embeddings)               AS chunks,
               (SELECT COUNT(*) FROM ticker_sentiments)                     AS sentiments,
               (SELECT COUNT(DISTINCT report_date) FROM networth_report)    AS reports,
               (SELECT COUNT(*) FROM pending_trades WHERE status = 'pending')
                                                                            AS pending_trades
        """
    ) or {}
