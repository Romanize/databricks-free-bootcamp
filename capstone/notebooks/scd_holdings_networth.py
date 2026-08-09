# Databricks notebook source
# MAGIC %md
# MAGIC # Daily SCD: holdings history + net worth history (Lakebase -> Unity Catalog)
# MAGIC
# MAGIC The **daily** job. It copies two things out of the operational Lakebase
# MAGIC database into Delta tables in Unity Catalog, for analytics that has nothing
# MAGIC to do with serving the app:
# MAGIC
# MAGIC | Delta table | Pattern | Why that pattern |
# MAGIC |---|---|---|
# MAGIC | `holdings_scd` | **SCD Type 2** | `holdings` is mutable *reference* data - an alias or a ticker can be edited in place, and the change is otherwise unrecoverable. This is also what lets a report from January still be read with January's labels, since the report itself only stores a `holding_id`. |
# MAGIC | `networth_history` | **Type 1 upsert** (MERGE on `report_date`) | Net worth is *already* a dated fact, one row per day, aggregated by the `networth_daily` view. Versioning it again would be tracking the history of a history. A report can be corrected on the day it was written, so the merge updates in place. |
# MAGIC
# MAGIC That asymmetry is the point of this notebook. Applying SCD2 to both would
# MAGIC look more consistent and be wrong: you would get a second valid_from /
# MAGIC valid_to layer over rows that are already time-stamped facts.
# MAGIC
# MAGIC Note what `holdings_scd` no longer tracks: quantities and values. Those
# MAGIC moved to `networth_report`, where they are already dated, so a change in
# MAGIC what you own is history captured by the fact table rather than a new
# MAGIC dimension version. The dimension only versions what a holding *is*.
# MAGIC
# MAGIC ## None of this is read by the app
# MAGIC
# MAGIC The app's charts read `networth_report` directly from Lakebase. These Delta
# MAGIC tables exist for later analysis - "how did my allocation drift over two
# MAGIC years", "when did I actually sell that position" - and nothing in the three
# MAGIC Databricks Apps depends on them. The job can fail for a week without
# MAGIC affecting anything a user sees.
# MAGIC
# MAGIC ## How the data gets across
# MAGIC
# MAGIC Read with **psycopg2 into Python, then `spark.createDataFrame`** - not
# MAGIC `spark.read.jdbc`. Same constraint as homework 2: JDBC against this Lakebase
# MAGIC instance is not supported. The volumes here are tiny (tens of holdings,
# MAGIC hundreds of reports), so a driver-side read is the right size of tool.
# MAGIC
# MAGIC ```
# MAGIC python notebooks/scd_holdings_networth.py --catalog main --schema capstone_analytics
# MAGIC ```
# MAGIC (as a script it needs a Spark session, so in practice: run it as a notebook.)

# COMMAND ----------

# DBTITLE 1,Install dependencies (notebook only)
# MAGIC %pip install -q 'databricks-sdk>=0.30.0' psycopg2-binary

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
NETWORTH_TABLE = f"{CATALOG}.{TARGET_SCHEMA}.networth_history"

RUN_DATE = dt.date.today()
print(f"Run date: {RUN_DATE}\nTargets:  {HOLDINGS_TABLE}\n          {NETWORTH_TABLE}")

# COMMAND ----------

# DBTITLE 1,Spark session
try:
    spark  # noqa: F821  - provided by the notebook
except NameError:  # pragma: no cover - script fallback
    from pyspark.sql import SparkSession

    spark = SparkSession.builder.appName("capstone-scd").getOrCreate()

from pyspark.sql import functions as F  # noqa: E402
from pyspark.sql.types import (  # noqa: E402
    BooleanType,
    DateType,
    DecimalType,
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
report_rows = app_db.query(
    """
    SELECT report_date, total_value, invested_value, cash_value,
           holdings_count, updated_at
    FROM networth_daily
    ORDER BY report_date
    """
)
print(f"Lakebase: {len(holdings_rows)} holdings, {len(report_rows)} reports")

if CREATE_SCHEMA:
    spark.sql(f"CREATE SCHEMA IF NOT EXISTS {CATALOG}.{TARGET_SCHEMA}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. `holdings_scd` - Slowly Changing Dimension, Type 2
# MAGIC
# MAGIC One row per (holding, version). A row is closed - `valid_to` set,
# MAGIC `is_current` false - the moment any tracked attribute changes, and a new
# MAGIC open row is written in its place.
# MAGIC
# MAGIC **What counts as a change** is `change_hash`: alias, type, symbol,
# MAGIC institution and the active flag. `updated_at` deliberately is *not* in it -
# MAGIC a touch that changed nothing must not create a version, or every daily run
# MAGIC would add a row per holding forever. `notes` is also excluded: editing a
# MAGIC memo to yourself is not a change in what the holding is.
# MAGIC
# MAGIC The job is **idempotent**: running it twice in one day produces the same
# MAGIC table, because the second run computes an identical hash and matches the
# MAGIC open row.

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

incoming = spark.createDataFrame(
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
).withColumn(
    # The business key of a *version*. Excludes timestamps on purpose.
    "change_hash",
    F.sha2(
        F.concat_ws(
            "||",
            *[
                F.coalesce(F.col(column).cast("string"), F.lit("~"))
                for column in ["alias", "holding_type", "symbol",
                               "institution", "is_active"]
            ],
        ),
        256,
    ),
).withColumn("valid_from", F.lit(RUN_DATE).cast(DateType())) \
 .withColumn("valid_to", F.lit(None).cast(DateType())) \
 .withColumn("is_current", F.lit(True))

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

incoming.createOrReplaceTempView("incoming_holdings")

# Step 1: close any open row whose attributes have changed. Note this only
# sees holdings still present in the source - the app soft-deletes (is_active
# goes false, which IS in the hash and so opens a new version), so a genuine
# hard delete would leave its last version open forever. That is acceptable
# here because nothing in the app hard-deletes a holding.
spark.sql(f"""
    MERGE INTO {HOLDINGS_TABLE} AS target
    USING incoming_holdings AS source
      ON  target.holding_id = source.holding_id
      AND target.is_current
    WHEN MATCHED AND target.change_hash <> source.change_hash THEN
      UPDATE SET target.valid_to = date_sub(source.valid_from, 1),
                 target.is_current = false
""")

# Step 2: insert a new open version for anything that is new or just closed.
spark.sql(f"""
    MERGE INTO {HOLDINGS_TABLE} AS target
    USING incoming_holdings AS source
      ON  target.holding_id = source.holding_id
      AND target.is_current
    WHEN NOT MATCHED THEN INSERT *
""")

closed_count = spark.sql(f"""
    SELECT COUNT(*) AS n FROM {HOLDINGS_TABLE} WHERE valid_to = date_sub('{RUN_DATE}', 1)
""").collect()[0]["n"]
current_count = spark.sql(f"SELECT COUNT(*) AS n FROM {HOLDINGS_TABLE} WHERE is_current") \
    .collect()[0]["n"]
total_count = spark.sql(f"SELECT COUNT(*) AS n FROM {HOLDINGS_TABLE}").collect()[0]["n"]

print(f"holdings_scd: {total_count} versions, {current_count} current, "
      f"{closed_count} closed by this run")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. `networth_history` - idempotent upsert on `report_date`
# MAGIC
# MAGIC Not SCD2. `networth_report` already has exactly one row per day and the app
# MAGIC guarantees it (`report_date` is UNIQUE, and every writer upserts). A report
# MAGIC can be *corrected* on the day it was written - a trade approved in the
# MAGIC afternoon rewrites the morning's figure - so the merge updates in place and
# MAGIC `synced_at` records when this job last saw it.

# COMMAND ----------

networth_schema = StructType([
    StructField("report_date", DateType(), False),
    StructField("total_value", DecimalType(20, 2), True),
    StructField("invested_value", DecimalType(20, 2), True),
    StructField("cash_value", DecimalType(20, 2), True),
    StructField("holdings_count", LongType(), True),
    StructField("source_updated_at", TimestampType(), True),
])

networth = spark.createDataFrame(
    [
        (
            row["report_date"],
            row["total_value"],
            row["invested_value"],
            row["cash_value"],
            row["holdings_count"],
            row["updated_at"],
        )
        for row in report_rows
    ],
    schema=networth_schema,
).withColumn("synced_at", F.current_timestamp())

spark.sql(f"""
    CREATE TABLE IF NOT EXISTS {NETWORTH_TABLE} (
        report_date       DATE,
        total_value       DECIMAL(20, 2),
        invested_value    DECIMAL(20, 2),
        cash_value        DECIMAL(20, 2),
        holdings_count    BIGINT,
        source_updated_at TIMESTAMP,
        synced_at         TIMESTAMP
    ) USING DELTA
""")

networth.createOrReplaceTempView("incoming_networth")

spark.sql(f"""
    MERGE INTO {NETWORTH_TABLE} AS target
    USING incoming_networth AS source
      ON target.report_date = source.report_date
    WHEN MATCHED AND (
             target.total_value    IS DISTINCT FROM source.total_value
          OR target.invested_value IS DISTINCT FROM source.invested_value
          OR target.cash_value     IS DISTINCT FROM source.cash_value
         ) THEN UPDATE SET *
    WHEN NOT MATCHED THEN INSERT *
""")

report_count = spark.sql(f"SELECT COUNT(*) AS n FROM {NETWORTH_TABLE}").collect()[0]["n"]
print(f"networth_history: {report_count} report days")

# COMMAND ----------

# MAGIC %md
# MAGIC ## What you can ask these tables
# MAGIC
# MAGIC Queries the app deliberately cannot answer, because it only knows the
# MAGIC current state of `holdings`:

# COMMAND ----------

print("Holdings that changed in the last 30 days:")
spark.sql(f"""
    SELECT holding_id, alias, symbol, holding_type, valid_from, valid_to, is_current
    FROM {HOLDINGS_TABLE}
    WHERE valid_from > date_sub(current_date(), 30)
    ORDER BY holding_id, valid_from
""").show(20, truncate=False)

print("Month-end net worth:")
spark.sql(f"""
    SELECT date_trunc('month', report_date) AS month,
           max_by(total_value, report_date)  AS month_end_value
    FROM {NETWORTH_TABLE}
    GROUP BY 1
    ORDER BY 1
""").show(24, truncate=False)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Scheduling
# MAGIC
# MAGIC Workflows -> Create job -> Notebook task pointing at this file, schedule
# MAGIC `0 30 3 * * ?` (03:30 daily - after any same-day trade has settled into the
# MAGIC report, before anyone looks at a dashboard).
# MAGIC
# MAGIC The job's identity needs:
# MAGIC
# MAGIC * READ on the `capstone` secret scope for `lakebase-url`
# MAGIC * `USE CATALOG` / `USE SCHEMA` / `CREATE TABLE` / `MODIFY` on the target schema
# MAGIC
# MAGIC Missing a run costs nothing permanent: the next run still sees the current
# MAGIC state of `holdings` and opens a version from that day. What is lost is the
# MAGIC precision of *when* a change happened, which is the accepted trade-off for a
# MAGIC daily snapshot rather than change-data-capture on the Postgres side.
