-- Generated from schema.py / tracing.py - the apps run this same DDL at startup
-- via init_db(), so you never need to execute this by hand. It is here so the
-- schema can be reviewed, diffed, or created up front in the Lakebase SQL editor.
--
-- Regenerate with: python sql/generate.py
-- Database: capstone (the app database)

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS holdings (
        id           BIGSERIAL PRIMARY KEY,
        alias        TEXT NOT NULL,
        holding_type TEXT NOT NULL CHECK (holding_type IN
                       ('ticker', 'crypto', 'cash', 'bank', 'wallet')),
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
    );

DROP INDEX IF EXISTS ux_holdings_alias;

CREATE UNIQUE INDEX ux_holdings_alias
    ON holdings (lower(alias)) WHERE is_active;

CREATE INDEX IF NOT EXISTS ix_holdings_symbol ON holdings (symbol) WHERE symbol IS NOT NULL;

CREATE TABLE IF NOT EXISTS watchlist (
        id        BIGSERIAL PRIMARY KEY,
        symbol    TEXT NOT NULL UNIQUE,
        reason    TEXT,
        is_active BOOLEAN NOT NULL DEFAULT true,
        added_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    );

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
    );

CREATE INDEX IF NOT EXISTS ix_ticker_news_tickers ON ticker_news USING gin (tickers);

CREATE INDEX IF NOT EXISTS ix_ticker_news_published ON ticker_news (published_utc DESC);

CREATE TABLE IF NOT EXISTS tickers_news_embeddings (
        id          TEXT PRIMARY KEY,
        article_id  TEXT NOT NULL REFERENCES ticker_news(id) ON DELETE CASCADE,
        -- Copied from the article so a filtered vector search ("news about
        -- AAPL") stays a single indexed scan instead of a join back to
        -- ticker_news before the ORDER BY can use the HNSW index.
        tickers     TEXT[] NOT NULL DEFAULT '{}',
        chunk_index INT NOT NULL,
        chunk_text  TEXT NOT NULL,
        embedding   VECTOR(384) NOT NULL,
        model_name  TEXT NOT NULL,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (article_id, chunk_index)
    );

CREATE INDEX IF NOT EXISTS ix_news_embeddings_vector
    ON tickers_news_embeddings USING hnsw (embedding vector_cosine_ops);

CREATE INDEX IF NOT EXISTS ix_news_embeddings_tickers
    ON tickers_news_embeddings USING gin (tickers);

CREATE TABLE IF NOT EXISTS ticker_sentiments (
        id                  BIGSERIAL PRIMARY KEY,
        symbol              TEXT NOT NULL,
        article_id          TEXT NOT NULL REFERENCES ticker_news(id) ON DELETE CASCADE,
        sentiment           TEXT NOT NULL CHECK (sentiment IN
                              ('positive', 'neutral', 'negative')),
        sentiment_reasoning TEXT,
        published_utc       TIMESTAMPTZ,
        created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (symbol, article_id)
    );

CREATE INDEX IF NOT EXISTS ix_ticker_sentiments_symbol ON ticker_sentiments (symbol, published_utc DESC);

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
    );

CREATE UNIQUE INDEX IF NOT EXISTS ux_investment_plan_active
    ON investment_plan ((is_active)) WHERE is_active;

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
    );

CREATE INDEX IF NOT EXISTS ix_networth_report_date ON networth_report (report_date DESC);

CREATE INDEX IF NOT EXISTS ix_networth_report_holding ON networth_report (holding_id);

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
                            ('pending', 'approved', 'executing', 'executed', 'rejected', 'failed')),
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
    );

CREATE INDEX IF NOT EXISTS ix_pending_trades_status ON pending_trades (status, created_at DESC);

DROP VIEW IF EXISTS networth_monthly;

DROP VIEW IF EXISTS networth_daily;

DROP VIEW IF EXISTS networth_report_lines;

DROP VIEW IF EXISTS ticker_sentiment_daily;

CREATE VIEW networth_report_lines AS
    SELECT r.id, r.report_date, r.holding_id,
           h.alias, h.holding_type, h.symbol, h.institution,
           r.quantity, r.price, r.price_as_of, r.price_source, r.value, r.notes,
           r.created_at, r.updated_at
    FROM networth_report r
    JOIN holdings h ON h.id = r.holding_id;

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
    GROUP BY report_date;

CREATE VIEW networth_monthly AS
    SELECT DISTINCT ON (date_trunc('month', report_date))
           date_trunc('month', report_date)::date AS month,
           report_date,
           total_value, invested_value, cash_value, holdings_count
    FROM networth_daily
    ORDER BY date_trunc('month', report_date), report_date DESC;

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
    GROUP BY symbol, day;
