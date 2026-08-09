"""
Resolves API credentials the same way `lakebase.py` resolves connection URLs.

    1. the plain env var  -> local development, loaded from .env
    2. the Databricks secret scope configured in app.yaml -> deployed app

This is the pattern homework 3 used for its one secret, generalised to the four
this project needs (two Lakebase URLs, the Massive key, the two Alpaca keys).
It is used in preference to Databricks Apps' `valueFrom:` because `valueFrom`
requires declaring a secret resource on the app in the UI, and this repo's other
apps deploy with nothing but a folder upload.

Nothing here ever logs a secret value - only whether one was found.
"""

import base64
import logging
import os
from functools import lru_cache

logger = logging.getLogger(__name__)

SCOPE = os.environ.get("CAPSTONE_SECRET_SCOPE", os.environ.get("LAKEBASE_SECRET_SCOPE", "capstone"))


def _in_databricks() -> bool:
    """True when a secret lookup has any chance of working."""
    # Check environment variables (apps, jobs, some notebook contexts)
    if os.environ.get("DATABRICKS_HOST") or os.environ.get("DATABRICKS_APP_PORT"):
        return True
    # Check for dbutils (notebooks, including serverless)
    try:
        import pyspark.dbutils
        return True
    except (ImportError, ModuleNotFoundError):
        pass
    return False


@lru_cache(maxsize=16)
def resolve(env_name: str, secret_key: str) -> str | None:
    """
    Return a credential, or None when it is not configured anywhere.

    None rather than an exception: every caller degrades politely (the app shows
    "not configured", the MCP tools answer `no_data`), and a missing optional
    integration should not crash an app at import time.
    """
    value = os.environ.get(env_name)
    if value:
        return value

    if not _in_databricks():
        return None

    try:
        from databricks.sdk import WorkspaceClient

        secret = WorkspaceClient().secrets.get_secret(scope=SCOPE, key=secret_key)
        logger.info("Resolved %s from secret %s/%s", env_name, SCOPE, secret_key)
        return base64.b64decode(secret.value).decode("utf-8")
    except Exception as err:
        logger.warning("Could not read secret %s/%s: %s", SCOPE, secret_key, err)
        return None
