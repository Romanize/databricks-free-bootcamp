# Databricks notebook source
# MAGIC %md
# MAGIC # Daily SCD2: holdings + watchlist (Lakebase -> Unity Catalog)
# MAGIC
# MAGIC The **daily** job. It versions the two pieces of *mutable reference data*
# MAGIC in the operational Lakebase database into Delta tables in Unity Catalog,
# MAGIC for analytics that has nothing to do with serving the app:
# MAGIC
# MAGIC | Delta table | Grain | Why it needs versioning |
# MAGIC |---|---|---|
# MAGIC | `holdings_scd` | one row per (holding, version) | `holdings` is mutable reference data - an alias or a ticker can be edited in place, and the change is otherwise unrecoverable. This is also what lets a report from January still be read with January's labels, since the report itself only stores a `holding_id`. |
# MAGIC | `watchlist_scd` | one row per (symbol, version) | `watchlist` is edited in place too, and re-adding a removed symbol reuses the same row (`ON CONFLICT (symbol) DO UPDATE`). Without a version history there is no way to answer *when* a symbol was being watched, which is exactly the window the news job was fetching articles for. |
# MAGIC
# MAGIC Both get the same treatment because both have the same problem: the
# MAGIC operational table only ever shows you *now*.
# MAGIC
# MAGIC ## What is deliberately not here
# MAGIC
# MAGIC Net worth. `networth_report` is already a dated fact - one row per
# MAGIC (report_date, holding), written once and only correctable on the day it was
# MAGIC written. Copying it into Delta was a no-op dressed up as a pipeline: the
# MAGIC same rows, the same grain, one day staler, and a second valid_from /
# MAGIC valid_to layer over rows that are already time-stamped facts. If an
# MAGIC analytics query wants net worth history it should read `networth_daily` /
# MAGIC `networth_monthly` in Lakebase, which is where the app reads it too.
# MAGIC
# MAGIC The same reasoning is why `holdings_scd` does not track quantities or
# MAGIC values: those live on `networth_report`, already dated. The dimensions
# MAGIC version only what a thing *is*.
# MAGIC
# MAGIC ## None of this is read by the app
# MAGIC
# MAGIC The app's charts read `networth_report` directly from Lakebase. These Delta
# MAGIC tables exist for later analysis - "how did my allocation drift over two
# MAGIC years", "when did I actually stop watching that ticker" - and nothing in the
# MAGIC three Databricks Apps depends on them. The job can fail for a week without
# MAGIC affecting anything a user sees.
# MAGIC
# MAGIC ## How the data gets across
# MAGIC
# MAGIC Read with **pg8000 into Python, then `spark.createDataFrame`** - not
# MAGIC `spark.read.jdbc`. Same constraint as homework 2: JDBC against this Lakebase
# MAGIC instance is not supported. The volumes here are tiny (tens of holdings, tens
# MAGIC of watched symbols), so a driver-side read is the right size of tool.
# MAGIC
# MAGIC ```
# MAGIC python notebooks/scd_holdings_watchlist.py --catalog main --schema capstone_analytics
# MAGIC ```
# MAGIC (as a script it needs a Spark session, so in practice: run it as a notebook.)

# COMMAND ----------

# DBTITLE 1,Install dependencies (notebook only)
# MAGIC %pip install -q 'databricks-sdk>=0.30.0' 'pg8000>=1.31.2'

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
# MAGIC `catalog` and `schema` are where the Delta tables land. The job's identity
# MAGIC needs CREATE TABLE on that schema, and READ on the `capstone` secret scope
# MAGIC for `lakebase-url`.

# COMMAND ----------

import argparse
import datetime as dt
import logging
import os
import sys

try:
    HERE = os.path.dirname(os.path.abspath(__file__))
except NameError:  # Databricks notebook
    HERE = os.getcwd()

ROOT = os.path.dirname(HERE)
for candidate in (os.path.join(ROOT, "mcp_server"), os.path.join(HERE, "mcp_server"), ROOT, HERE):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

try:
    dbutils  # noqa: F821
    dbutils.widgets.text("catalog", "main", "Unity Catalog catalog")
    dbutils.widgets.text("target_schema", "capstone_analytics", "Schema for the Delta tables")
    dbutils.widgets.dropdown("create_schema", "true", ["true", "false"], "Create the schema if missing")

    CATALOG = dbutils.widgets.get("catalog").strip()
    TARGET_SCHEMA = dbutils.widgets.get("target_schema").strip()
    CREATE_SCHEMA = dbutils.widgets.get("create_schema") == "true"
except NameError:
    parser = argparse.ArgumentParser(description="Build SCD tables from Lakebase.")
    parser.add_argument("--catalog", default="main")
    parser.add_argument("--schema", dest="target_schema", default="capstone_analytics")
    parser.add_argument("--no-create-schema", dest="create_schema", action="store_false")
    args = parser.parse_args()

    CATALOG = args.catalog
    TARGET_SCHEMA = args.target_schema
    CREATE_SCHEMA = args.create_schema

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("scd-job")

HOLDINGS_TABLE = f"{CATALOG}.{TARGET_SCHEMA}.holdings_scd"
WATCHLIST_TABLE = f"{CATALOG}.{TARGET_SCHEMA}.watchlist_scd"

RUN_DATE = dt.date.today()
print(f"Run date: {RUN_DATE}\nTargets:  {HOLDINGS_TABLE}\n          {WATCHLIST_TABLE}")

# COMMAND ----------

# DBTITLE 1,Spark session
try:
    spark  # noqa: F821  - provided by the notebook
except NameError:  # pragma: no cover - script fallback
    from pyspark.sql import SparkSession

    spark = SparkSession.builder.appName("capstone-scd").getOrCreate()

from pyspark.sql import DataFrame, functions as F  # noqa: E402
from pyspark.sql.types import (  # noqa: E402
    BooleanType,
    DateType,
    LongType,
    StringType,
    StructField,
    StructType,
    TimestampType,
)

# COMMAND ----------

# DBTITLE 1,Read the current state out of Lakebase
from lakebase import app_db  # noqa: E402

holdings_rows = app_db.query(
    """
    SELECT id, alias, holding_type, symbol, institution, notes,
           is_active, updated_at
    FROM holdings
    ORDER BY id
    """
)
watchlist_rows = app_db.query(
    """
    SELECT id, symbol, reason, is_active, added_at
    FROM watchlist
    ORDER BY id
    """
)
print(f"Lakebase: {len(holdings_rows)} holdings, {len(watchlist_rows)} watchlist symbols")

if CREATE_SCHEMA:
    spark.sql(f"CREATE SCHEMA IF NOT EXISTS {CATALOG}.{TARGET_SCHEMA}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## The SCD Type 2 merge, written once
# MAGIC
# MAGIC Both tables get identical treatment, so the merge lives in one function
# MAGIC rather than being pasted twice with two chances to drift.
# MAGIC
# MAGIC One row per (entity, version). A row is closed - `valid_to` set,
# MAGIC `is_current` false - the moment any tracked attribute changes, and a new
# MAGIC open row is written in its place.
# MAGIC
# MAGIC **What counts as a change** is `change_hash`, built from the caller's list
# MAGIC of tracked columns. Timestamps are deliberately never in it: a touch that
# MAGIC changed nothing must not create a version, or every daily run would add a
# MAGIC row per entity forever. That is also what makes the job **idempotent** -
# MAGIC running it twice in one day produces the same table, because the second run
# MAGIC computes an identical hash and matches the open row.
# MAGIC
# MAGIC Note what the merge only ever sees: entities still present in the source.
# MAGIC Both tables soft-delete (`is_active` goes false, which IS in the hash and so
# MAGIC opens a new version), so a genuine hard delete would leave its last version
# MAGIC open forever. Acceptable here because nothing in the app hard-deletes
# MAGIC either a holding or a watchlist row.

# COMMAND ----------


def with_scd_columns(df: DataFrame, hashed_columns: list[str]) -> DataFrame:
    """Add change_hash / valid_from / valid_to / is_current to a snapshot."""
    return (
        df.withColumn(
            # The business key of a *version*. Excludes timestamps on purpose.
            "change_hash",
            F.sha2(
                F.concat_ws(
                    "||",
                    *[
                        F.coalesce(F.col(column).cast("string"), F.lit("~"))
                        for column in hashed_columns
                    ],
                ),
                256,
            ),
        )
        .withColumn("valid_from", F.lit(RUN_DATE).cast(DateType()))
        .withColumn("valid_to", F.lit(None).cast(DateType()))
        .withColumn("is_current", F.lit(True))
    )


def apply_scd2(table: str, incoming: DataFrame, key_column: str) -> None:
    """Close changed versions, then open new ones. Two merges, in that order."""
    view = f"incoming_{table.split('.')[-1]}"
    incoming.createOrReplaceTempView(view)

    # Step 1: close any open row whose attributes have changed.
    spark.sql(f"""
        MERGE INTO {table} AS target
        USING {view} AS source
          ON  target.{key_column} = source.{key_column}
          AND target.is_current
        WHEN MATCHED AND target.change_hash <> source.change_hash THEN
          UPDATE SET target.valid_to = date_sub(source.valid_from, 1),
                     target.is_current = false
    """)

    # Step 2: insert a new open version for anything that is new or just closed.
    spark.sql(f"""
        MERGE INTO {table} AS target
        USING {view} AS source
          ON  target.{key_column} = source.{key_column}
          AND target.is_current
        WHEN NOT MATCHED THEN INSERT *
    """)

    closed = spark.sql(
        f"SELECT COUNT(*) AS n FROM {table} WHERE valid_to = date_sub('{RUN_DATE}', 1)"
    ).collect()[0]["n"]
    current = spark.sql(f"SELECT COUNT(*) AS n FROM {table} WHERE is_current").collect()[0]["n"]
    total = spark.sql(f"SELECT COUNT(*) AS n FROM {table}").collect()[0]["n"]

    print(f"{table.split('.')[-1]}: {total} versions, {current} current, "
          f"{closed} closed by this run")


# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. `holdings_scd`
# MAGIC
# MAGIC Tracked: alias, type, symbol, institution and the active flag. `notes` is
# MAGIC excluded - editing a memo to yourself is not a change in what the holding
# MAGIC is - and so is `updated_at`.

# COMMAND ----------

holdings_schema = StructType([
    StructField("holding_id", StringType(), False),
    StructField("alias", StringType(), True),
    StructField("holding_type", StringType(), True),
    StructField("symbol", StringType(), True),
    StructField("institution", StringType(), True),
    StructField("notes", StringType(), True),
    StructField("is_active", BooleanType(), True),
    StructField("source_updated_at", TimestampType(), True),
])

holdings_incoming = with_scd_columns(
    spark.createDataFrame(
        [
            (
                str(row["id"]),
                row["alias"],
                row["holding_type"],
                row["symbol"],
                row["institution"],
                row["notes"],
                row["is_active"],
                row["updated_at"],
            )
            for row in holdings_rows
        ],
        schema=holdings_schema,
    ),
    ["alias", "holding_type", "symbol", "institution", "is_active"],
)

spark.sql(f"""
    CREATE TABLE IF NOT EXISTS {HOLDINGS_TABLE} (
        holding_id        STRING,
        alias             STRING,
        holding_type      STRING,
        symbol            STRING,
        institution       STRING,
        notes             STRING,
        is_active         BOOLEAN,
        source_updated_at TIMESTAMP,
        change_hash       STRING,
        valid_from        DATE,
        valid_to          DATE,
        is_current        BOOLEAN
    ) USING DELTA
""")

apply_scd2(HOLDINGS_TABLE, holdings_incoming, "holding_id")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. `watchlist_scd`
# MAGIC
# MAGIC Keyed on **`symbol`**, not on `id`. Symbol is the natural key - it is
# MAGIC `UNIQUE` in Lakebase and `add_to_watchlist()` upserts on it, so re-adding a
# MAGIC removed symbol comes back as the same row. Keying on the surrogate id would
# MAGIC say the same thing with an extra hop.
# MAGIC
# MAGIC Tracked: `reason` and `is_active`. Unlike holdings, `reason` **is** in the
# MAGIC hash - it is the answer to "why was I watching this", which is the whole
# MAGIC point of keeping the history, not an incidental memo.
# MAGIC
# MAGIC `added_at` rides along as an attribute but is not hashed. It is the row's
# MAGIC own creation time and never changes, so including it would be noise.
# MAGIC
# MAGIC Removing a symbol from the watchlist sets `is_active = false`, which closes
# MAGIC the open version and opens an inactive one. That is what lets you line up
# MAGIC "watched from X to Y" against the articles `tickers_news_embeddings`
# MAGIC actually collected in that window - the news job fetches the union of the
# MAGIC active watchlist and current holdings, so a gap in coverage is explained by
# MAGIC exactly these two tables.

# COMMAND ----------

watchlist_schema = StructType([
    StructField("symbol", StringType(), False),
    StructField("watchlist_id", LongType(), True),
    StructField("reason", StringType(), True),
    StructField("is_active", BooleanType(), True),
    StructField("source_added_at", TimestampType(), True),
])

watchlist_incoming = with_scd_columns(
    spark.createDataFrame(
        [
            (
                row["symbol"],
                int(row["id"]),
                row["reason"],
                row["is_active"],
                row["added_at"],
            )
            for row in watchlist_rows
        ],
        schema=watchlist_schema,
    ),
    ["reason", "is_active"],
)

spark.sql(f"""
    CREATE TABLE IF NOT EXISTS {WATCHLIST_TABLE} (
        symbol          STRING,
        watchlist_id    BIGINT,
        reason          STRING,
        is_active       BOOLEAN,
        source_added_at TIMESTAMP,
        change_hash     STRING,
        valid_from      DATE,
        valid_to        DATE,
        is_current      BOOLEAN
    ) USING DELTA
""")

apply_scd2(WATCHLIST_TABLE, watchlist_incoming, "symbol")

# COMMAND ----------

# MAGIC %md
# MAGIC ## What you can ask these tables
# MAGIC
# MAGIC Queries the app deliberately cannot answer, because it only knows the
# MAGIC current state of `holdings` and `watchlist`:

# COMMAND ----------

print("Holdings that changed in the last 30 days:")
spark.sql(f"""
    SELECT holding_id, alias, symbol, holding_type, valid_from, valid_to, is_current
    FROM {HOLDINGS_TABLE}
    WHERE valid_from > date_sub(current_date(), 30)
    ORDER BY holding_id, valid_from
""").show(20, truncate=False)

print("How long each symbol has been watched, per stretch:")
spark.sql(f"""
    SELECT symbol,
           reason,
           valid_from,
           COALESCE(valid_to, current_date()) AS watched_through,
           datediff(COALESCE(valid_to, current_date()), valid_from) AS days
    FROM {WATCHLIST_TABLE}
    WHERE is_active
    ORDER BY symbol, valid_from
""").show(20, truncate=False)

print("Symbols dropped from the watchlist, and when:")
spark.sql(f"""
    SELECT symbol, valid_from AS dropped_on, reason
    FROM {WATCHLIST_TABLE}
    WHERE NOT is_active AND is_current
    ORDER BY valid_from DESC
""").show(20, truncate=False)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Scheduling
# MAGIC
# MAGIC Workflows -> Create job -> Notebook task pointing at this file, schedule
# MAGIC `0 30 3 * * ?` (03:30 daily - before anyone looks at a dashboard, and after
# MAGIC the day's edits have stopped).
# MAGIC
# MAGIC The job's identity needs:
# MAGIC
# MAGIC * READ on the `capstone` secret scope for `lakebase-url`
# MAGIC * `USE CATALOG` / `USE SCHEMA` / `CREATE TABLE` / `MODIFY` on the target schema
# MAGIC
# MAGIC Missing a run costs nothing permanent: the next run still sees the current
# MAGIC state of both tables and opens a version from that day. What is lost is the
# MAGIC precision of *when* a change happened, which is the accepted trade-off for a
# MAGIC daily snapshot rather than change-data-capture on the Postgres side. A
# MAGIC symbol added and removed between two runs is invisible, same as a holding
# MAGIC renamed twice in an afternoon.
