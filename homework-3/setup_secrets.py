"""
One-time setup: store this homework's Lakebase connection URL in its own
Databricks secret scope.

Homework 3 is self-contained: it reads scope `homework-3`, key `lakebase-url`,
so it can point at a different Lakebase instance, database or role than the
other homeworks without either of them being affected.

Both apps in this homework read the same secret:
  * mcp_server/ writes a row per MCP tool call
  * dashboard/  reads those rows
so BOTH service principals need READ on the scope. The script asks for them one
at a time - deploy the apps first, then paste each app's service principal UUID.

The weather APIs themselves (Open-Meteo, api.weather.gov) need no credentials,
so the Lakebase URL is the only secret in this homework.

Run from a Databricks notebook terminal, or locally with the Databricks CLI
configured. The URL is read with getpass, so it is never echoed, written to
disk, or saved in shell history - and it never lands in this repo.

Usage:
    python setup_secrets.py
"""

import getpass

from databricks.sdk import WorkspaceClient
from databricks.sdk.service import workspace

# Must match LAKEBASE_SECRET_SCOPE / LAKEBASE_SECRET_KEY in both app.yaml files.
SCOPE = "homework-3"
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

# Both apps' service principals need READ to fetch the secret at startup.
print(
    "\nEnter the service principal UUID for each app (blank to finish).\n"
    "Find it on the app's page in the Databricks UI."
)
while True:
    principal = input("App service principal UUID: ").strip()
    if not principal:
        break
    try:
        w.secrets.put_acl(
            scope=SCOPE,
            principal=principal,
            permission=workspace.AclPermission.READ,
        )
        print(f"  Granted READ on '{SCOPE}' to '{principal}'")
    except Exception as err:
        print(f"  Could not grant READ to '{principal}': {err}")
        print("  Deploy the app first, then re-run this script or grant manually.")
