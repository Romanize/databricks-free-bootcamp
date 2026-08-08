"""
One-time setup: store this homework's Lakebase connection URL in its own
Databricks secret scope.

Homework 2 is self-contained: it reads scope `homework-2`, key `lakebase-url`,
so it can point at a different Lakebase instance, database or role than the
other homeworks without either of them being affected.

Run from a Databricks notebook terminal, or locally with the Databricks CLI
configured. The URL is read with getpass, so it is never echoed, written to
disk, or saved in shell history - and it never lands in this repo.

Usage:
    python setup_secrets.py
"""

import getpass

from databricks.sdk import WorkspaceClient
from databricks.sdk.service import workspace

# Must match LAKEBASE_SECRET_SCOPE / LAKEBASE_SECRET_KEY in app.yaml.
SCOPE = "homework-2"
KEY = "lakebase-url"

w = WorkspaceClient()

try:
    w.secrets.create_scope(scope=SCOPE)
    print(f"Created secret scope '{SCOPE}'")
except Exception as err:  # scope already exists on a re-run
    print(f"Scope '{SCOPE}' already exists ({err})")

w.secrets.put_secret(
    scope=SCOPE,
    key=KEY,
    string_value=getpass.getpass("Paste your Lakebase connection URL: "),
)
print(f"Stored secret {SCOPE}/{KEY}")

# The app's service principal needs READ to fetch the secret at startup.
APP_SERVICE_PRINCIPAL = input("Enter the app service principal UUID: ").strip()
try:
    w.secrets.put_acl(
        scope=SCOPE,
        principal=APP_SERVICE_PRINCIPAL,
        permission=workspace.AclPermission.READ,
    )
    print(f"Granted READ on '{SCOPE}' to app '{APP_SERVICE_PRINCIPAL}'")
except Exception as err:
    print(f"Could not grant READ to '{APP_SERVICE_PRINCIPAL}': {err}")
    print(f"Deploy the app first, then re-run this script or grant manually.")
