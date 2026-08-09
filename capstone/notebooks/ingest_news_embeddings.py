# Databricks notebook source
# MAGIC %md
# MAGIC # Ingest ticker news -> vector embeddings (Lakebase)
# MAGIC
# MAGIC The **every-2-hours** job. For each tracked ticker it:
# MAGIC
# MAGIC 1. Reads the high-water mark - the newest `published_utc` already stored
# MAGIC    for that ticker - and asks Massive only for articles published after it.
# MAGIC 2. Upserts the articles into `ticker_news` and their per-ticker sentiment
# MAGIC    into `ticker_sentiments`.
# MAGIC 3. Chunks and embeds whatever is pending with
# MAGIC    `sentence-transformers/all-MiniLM-L6-v2` (384-dim), upserting into
# MAGIC    `tickers_news_embeddings` via pg8000 + batched `execute_values`, casting each
# MAGIC    vector with `%s::vector`.
# MAGIC
# MAGIC "Tracked" means **the watchlist plus everything currently held** - you want
# MAGIC news about what you own whether or not you also remembered to watch it.
# MAGIC
# MAGIC ## Why this is a job and not a button
# MAGIC
# MAGIC Massive's free tier allows **5 requests per minute**, and there is no batch
# MAGIC news endpoint, so it is one request per ticker. `massive_api` sleeps 12.5s
# MAGIC between calls to stay under that ceiling, which means 20 tickers takes about
# MAGIC four minutes - fine for a background job, well past an HTTP request timeout.
# MAGIC The app can sync a *single* ticker on demand; bulk ingestion lives here.
# MAGIC
# MAGIC ## Why incremental
# MAGIC
# MAGIC Twelve runs a day x 20 tickers is 240 requests. Without the high-water mark
# MAGIC every run would re-fetch the same recent articles, and `synced_at` only moves
# MAGIC when `embed_text` actually changes, so re-embedding is already avoided - but
# MAGIC the *requests* would still be spent. The watermark is what keeps the job
# MAGIC inside a free API plan.
# MAGIC
# MAGIC Everything runs in plain Python - **no `spark.write.jdbc`**, which is not
# MAGIC supported against this Lakebase instance.
# MAGIC
# MAGIC Runs unchanged as a Databricks notebook or as a local script:
# MAGIC
# MAGIC ```
# MAGIC python notebooks/ingest_news_embeddings.py --symbols AAPL MSFT --limit 20
# MAGIC python notebooks/ingest_news_embeddings.py --skip-fetch   # embed only
# MAGIC ```

# COMMAND ----------

# DBTITLE 1,Install dependencies (notebook only)
# MAGIC %pip install -q 'databricks-sdk>=0.30.0' 'pg8000>=1.31.2' sentence-transformers requests

# COMMAND ----------

# MAGIC %md
# MAGIC ### Why pg8000 and not psycopg2
# MAGIC
# MAGIC `psycopg2-binary` is a C extension carrying its own libpq and OpenSSL. On a
# MAGIC Databricks Runtime those have to coexist with the image's copies, and when
# MAGIC they disagree the notebook does not raise - the kernel dies, which is what
# MAGIC these two jobs were hitting. `pg8000` implements the Postgres wire protocol
# MAGIC in pure Python, so there is nothing to link and nothing to clash: it installs
# MAGIC and imports on any runtime version.
# MAGIC
# MAGIC The driver differences (dict rows, typed errors, `execute_values`) are all
# MAGIC absorbed in `mcp_server/lakebase.py`, so nothing below this cell knows which
# MAGIC driver is in use.

# COMMAND ----------

# MAGIC %md
# MAGIC ## Config
# MAGIC
# MAGIC In a notebook these come from widgets; as a script they come from CLI flags.
# MAGIC Both land in the same variables.
# MAGIC
# MAGIC Leave `symbols` empty to use the tracked set (watchlist + holdings), which
# MAGIC is what the scheduled job should do. Naming symbols explicitly is for
# MAGIC backfilling one ticker by hand.

# COMMAND ----------

import argparse
import logging
import os
import sys

# The pipeline modules (lakebase.py, schema.py, embeddings.py, massive_api.py)
# live in mcp_server/. __file__ is undefined in a notebook, so fall back to the
# working directory and add every plausible candidate to the path.
try:
    HERE = os.path.dirname(os.path.abspath(__file__))
except NameError:  # Databricks notebook
    HERE = os.getcwd()

ROOT = os.path.dirname(HERE)
for candidate in (os.path.join(ROOT, "mcp_server"), os.path.join(HERE, "mcp_server"), ROOT, HERE):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

try:
    dbutils  # noqa: F821  - defined only inside a Databricks notebook
    dbutils.widgets.text("symbols", "", "Symbols (comma separated; blank = tracked set)")
    dbutils.widgets.text("limit", "20", "Max articles per symbol")
    dbutils.widgets.text("embed_limit", "1000", "Max articles to embed per run")
    dbutils.widgets.dropdown("skip_fetch", "false", ["true", "false"], "Skip fetch, embed only")
    dbutils.widgets.dropdown("full_refresh", "false", ["true", "false"], "Ignore the watermark")

    SYMBOLS = [s.strip().upper() for s in dbutils.widgets.get("symbols").split(",") if s.strip()]
    LIMIT = int(dbutils.widgets.get("limit"))
    EMBED_LIMIT = int(dbutils.widgets.get("embed_limit"))
    SKIP_FETCH = dbutils.widgets.get("skip_fetch") == "true"
    FULL_REFRESH = dbutils.widgets.get("full_refresh") == "true"
except NameError:  # plain script
    parser = argparse.ArgumentParser(description="Ingest ticker news into Lakebase.")
    parser.add_argument("--symbols", nargs="*", default=[], help="blank = watchlist + holdings")
    parser.add_argument("--limit", type=int, default=20, help="max articles per symbol")
    parser.add_argument("--embed-limit", type=int, default=1000)
    parser.add_argument("--skip-fetch", action="store_true", help="embed pending only")
    parser.add_argument("--full-refresh", action="store_true", help="ignore the watermark")
    args = parser.parse_args()

    SYMBOLS = [s.upper() for s in args.symbols]
    LIMIT = args.limit
    EMBED_LIMIT = args.embed_limit
    SKIP_FETCH = args.skip_fetch
    FULL_REFRESH = args.full_refresh

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("ingest-news")

# COMMAND ----------

# DBTITLE 1,Connect and make sure the schema exists
import embeddings
import massive_api
import schema

schema.init_db()

symbols = SYMBOLS or schema.tracked_symbols()
if not symbols:
    raise SystemExit(
        "Nothing to ingest: the watchlist is empty and no ticker or crypto "
        "holdings exist. Add something in the app first."
    )

print(f"Model:    {embeddings.MODEL_NAME} ({embeddings.EMBEDDING_DIM}-dim)")
print(f"Symbols:  {', '.join(symbols)}")
print(f"Massive:  {'configured' if massive_api.is_configured() else 'NOT CONFIGURED'}")
print(f"Pacing:   {massive_api.MIN_INTERVAL}s between requests "
      f"(~{len(symbols) * massive_api.MIN_INTERVAL / 60:.1f} min for this run)")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. Fetch
# MAGIC
# MAGIC One request per symbol, paced by the rate limiter inside `massive_api`.
# MAGIC A symbol that fails is reported and skipped - one bad ticker must not cost
# MAGIC the whole run, especially when the run takes four minutes to get here.

# COMMAND ----------

fetch_stats = {"symbols": 0, "articles": 0, "sentiments": 0, "errors": {}}

if SKIP_FETCH:
    print("Skipping fetch (embed-only run).")
elif not massive_api.is_configured():
    print("MASSIVE_API_KEY is not set - skipping fetch, will embed whatever is pending.")
else:
    for symbol in symbols:
        # The watermark: only ask for what was published after what we already
        # have. --full-refresh drops it, for a backfill.
        since = None
        if not FULL_REFRESH:
            newest = schema.latest_article_time(symbol)
            since = newest.isoformat() if newest else None

        try:
            articles, sentiments = massive_api.fetch_news(symbol, limit=LIMIT, published_after=since)
        except massive_api.MassiveAPIError as err:
            logger.warning("%s: %s", symbol, err)
            fetch_stats["errors"][symbol] = str(err)
            continue

        written = schema.upsert_articles(articles)
        scored = schema.upsert_sentiments(sentiments)

        fetch_stats["symbols"] += 1
        fetch_stats["articles"] += written
        fetch_stats["sentiments"] += scored
        print(f"  {symbol:<10} since={str(since)[:19] or 'beginning':<19} "
              f"articles={written:<4} sentiments={scored}")

    print(f"\nFetched {fetch_stats['articles']} articles across "
          f"{fetch_stats['symbols']} symbols; {len(fetch_stats['errors'])} failed.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. Embed
# MAGIC
# MAGIC `embed_pending()` selects exactly the stale work:
# MAGIC
# MAGIC ```sql
# MAGIC WHERE newest_embedding_created_at IS NULL
# MAGIC    OR newest_embedding_created_at < n.synced_at
# MAGIC ```
# MAGIC
# MAGIC and `synced_at` only moves when `embed_text` actually changed. So a re-run
# MAGIC that fetched the same articles embeds nothing, and an article whose text was
# MAGIC corrected gets re-embedded and has any leftover chunks from the longer
# MAGIC version deleted.

# COMMAND ----------

pending_before = embeddings.count_pending()
print(f"Pending articles: {pending_before}")

embed_stats = embeddings.embed_pending(limit=EMBED_LIMIT)
print(f"Embedded {embed_stats['chunks']} chunks across {embed_stats['articles']} articles.")
if embed_stats.get("symbols"):
    top = sorted(embed_stats["symbols"].items(), key=lambda kv: -kv[1])[:10]
    print("Chunks by symbol: " + ", ".join(f"{s}={n}" for s, n in top))

# COMMAND ----------

# DBTITLE 1,Run summary
counters = schema.stats()
summary = {
    "symbols_requested": len(symbols),
    "symbols_fetched": fetch_stats["symbols"],
    "articles_upserted": fetch_stats["articles"],
    "sentiments_upserted": fetch_stats["sentiments"],
    "fetch_errors": fetch_stats["errors"],
    "articles_embedded": embed_stats["articles"],
    "chunks_written": embed_stats["chunks"],
    "pending_after": embeddings.count_pending(),
    "total_articles": counters.get("articles"),
    "total_chunks": counters.get("chunks"),
    "total_sentiments": counters.get("sentiments"),
}
for key, value in summary.items():
    print(f"{key:<22} {value}")

# Fail the job run when every symbol failed - a silent green run that fetched
# nothing is worse than a red one, because nobody looks at a green run.
if fetch_stats["errors"] and fetch_stats["symbols"] == 0 and not SKIP_FETCH:
    raise RuntimeError(f"Every symbol failed: {fetch_stats['errors']}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Scheduling
# MAGIC
# MAGIC Workflows -> Create job -> Notebook task pointing at this file, on a
# MAGIC serverless or single-node cluster, schedule `0 0 */2 * * ?` (every 2 hours).
# MAGIC
# MAGIC The job's identity needs READ on the `capstone` secret scope for
# MAGIC `lakebase-url` and `massive-api-key` - see `setup_secrets.py`.
# MAGIC
# MAGIC Leave every widget at its default. Set `skip_fetch=true` to re-embed
# MAGIC without spending API quota, or `full_refresh=true` to backfill a ticker's
# MAGIC full history once.