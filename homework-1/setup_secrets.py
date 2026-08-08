"""
One-time setup: store the Lakebase connection URL in a Databricks secret scope.

Run from a Databricks notebook terminal, or locally with the Databricks CLI
configured. The URL is read with getpass, so it is never echoed, written to
disk, or saved in shell history - and it never lands in this repo.

Usage:
    python setup_secrets.py
"""

import getpass

from databricks.sdk import WorkspaceClient
from databricks.sdk.service import workspace

SCOPE = "database"
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
w.secrets.put_acl(
    scope=SCOPE,
    principal="users",
    permission=workspace.AclPermission.READ,
)
print(f"Granted READ on '{SCOPE}' to 'users'")
