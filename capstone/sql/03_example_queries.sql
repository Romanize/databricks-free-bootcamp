-- Queries worth knowing, against the `capstone` app database.
-- These are the same shapes the app and the MCP server run; keeping them here
-- makes it possible to check a number by hand when a chart looks wrong.

-- ---------------------------------------------------------------- net worth

-- The current report and its lines, largest first.
SELECT alias, holding_type, symbol, quantity, price, value, price_source, price_as_of
FROM networth_report_lines
WHERE report_date = (SELECT MAX(report_date) FROM networth_report)
ORDER BY value DESC;

-- Growth between consecutive reports. Reports are irregular - you submit them
-- when you get round to it - so the day gap matters when reading the change.
SELECT report_date,
       total_value,
       total_value - LAG(total_value) OVER (ORDER BY report_date)  AS change,
       report_date - LAG(report_date) OVER (ORDER BY report_date)  AS days_since_previous
FROM networth_daily
ORDER BY report_date;

-- Month-end net worth: what the monthly chart plots. DISTINCT ON keeps the LAST
-- report of each month, so a mid-month reading never becomes the month's number.
SELECT month, report_date, total_value, invested_value, cash_value
FROM networth_monthly
ORDER BY month;

-- Allocation drift: each type's share of the total, per report.
SELECT l.report_date,
       l.holding_type,
       SUM(l.value)                                        AS value,
       ROUND(100 * SUM(l.value) / NULLIF(d.total_value, 0), 1) AS percent
FROM networth_report_lines l
JOIN networth_daily d ON d.report_date = l.report_date
GROUP BY l.report_date, d.total_value, l.holding_type
ORDER BY l.report_date DESC, value DESC;

-- Holdings that have never been reported on. They exist as reference data but
-- contribute nothing to any total, which is usually an oversight.
SELECT h.id, h.alias, h.holding_type, h.symbol
FROM holdings h
WHERE h.is_active
  AND NOT EXISTS (SELECT 1 FROM networth_report r WHERE r.holding_id = h.id);

-- Lines in the latest report that were carried forward rather than priced or
-- typed in fresh. A mixed report is fine, but you should know which is which.
SELECT alias, symbol, price, value, price_source, price_as_of
FROM networth_report_lines
WHERE report_date = (SELECT MAX(report_date) FROM networth_report)
  AND price_source LIKE 'carried%'
ORDER BY value DESC;

-- --------------------------------------------------------------------- news

-- What the 2-hourly job will fetch next: tracked symbols and their watermarks.
SELECT s.symbol,
       (SELECT MAX(published_utc) FROM ticker_news n WHERE s.symbol = ANY(n.tickers))
           AS newest_article,
       (SELECT COUNT(*) FROM ticker_news n WHERE s.symbol = ANY(n.tickers))
           AS articles
FROM (
    SELECT symbol FROM watchlist WHERE is_active
    UNION
    SELECT symbol FROM holdings
    WHERE is_active AND symbol IS NOT NULL AND holding_type IN ('ticker', 'crypto')
) s
ORDER BY newest_article NULLS FIRST;

-- Articles waiting to be embedded. Should be 0 shortly after each job run.
SELECT COUNT(*) AS pending
FROM ticker_news n
LEFT JOIN LATERAL (
    SELECT max(e.created_at) AS embedded_at
    FROM tickers_news_embeddings e WHERE e.article_id = n.id
) e ON TRUE
WHERE e.embedded_at IS NULL OR e.embedded_at < n.synced_at;

-- Sentiment over the last 30 days, per tracked symbol.
SELECT symbol,
       COUNT(*)                                       AS articles,
       COUNT(*) FILTER (WHERE sentiment = 'positive') AS positive,
       COUNT(*) FILTER (WHERE sentiment = 'negative') AS negative,
       ROUND((COUNT(*) FILTER (WHERE sentiment = 'positive')
              - COUNT(*) FILTER (WHERE sentiment = 'negative'))::numeric
             / NULLIF(COUNT(*), 0), 3)                AS score
FROM ticker_sentiments
WHERE published_utc > now() - interval '30 days'
GROUP BY symbol
ORDER BY articles DESC;

-- Vector search by hand. Paste a 384-float literal from embeddings.embed_query().
-- SELECT n.title, 1 - (e.embedding <=> '[0.1,...]'::vector) AS similarity
-- FROM tickers_news_embeddings e
-- JOIN ticker_news n ON n.id = e.article_id
-- WHERE e.tickers @> ARRAY['AAPL']::text[]
-- ORDER BY e.embedding <=> '[0.1,...]'::vector
-- LIMIT 5;

-- ------------------------------------------------------------------- trades

-- The approval queue, and what happened to everything else. Note that
-- confirmation_hash is never selected here: it is a credential, and the app and
-- the MCP tools both go through schema.SAFE_TRADE_COLUMNS for the same reason.
SELECT id, created_at, proposed_by, side, quantity, symbol, order_type,
       status, filled_price, error_message, decided_at, executed_at
FROM pending_trades
ORDER BY (status = 'pending') DESC, created_at DESC;

-- Trades stuck mid-flight: approved but never redeemed (the user accepted and
-- the agent never called execute_trade), or claimed but never finalized.
SELECT id, symbol, side, quantity, status, decided_at, key_expires_at,
       key_expires_at < now() AS key_expired
FROM pending_trades
WHERE status IN ('approved', 'executing')
ORDER BY decided_at;

-- Trades the agent proposed vs. what you actually approved. A high rejection
-- rate is a signal the system prompt needs tightening.
SELECT proposed_by, status, COUNT(*)
FROM pending_trades
GROUP BY proposed_by, status
ORDER BY proposed_by, status;
