"""
One-time setup: store the capstone's five secrets in its own Databricks scope.

The capstone is self-contained. It reads scope `capstone`, so it can point at
different Lakebase instances, a different Massive key and a different Alpaca
account than any of the three homeworks, without either side affecting the other.

    capstone/lakebase-url          app database  (holdings, news, reports, trades)
    capstone/lakebase-tracing-url  tracing database (agent_tool_calls)
    capstone/massive-api-key       Massive news + EOD prices
    capstone/alpaca-api-key-id     Alpaca PAPER key id
    capstone/alpaca-secret-key     Alpaca PAPER secret

## Who needs what

Three apps and two jobs read from this scope, and they do NOT all need
everything. The script grants per-app rather than blanket-granting the scope,
because the dashboard has no business being able to read an Alpaca key:

    app/         lakebase-url, massive-api-key, alpaca-*   (submits approved orders)
    mcp_server/  lakebase-url, lakebase-tracing-url, massive-api-key, alpaca-*
    dashboard/   lakebase-tracing-url                       (read-only tracing)
    jobs         lakebase-url, massive-api-key

Databricks secret ACLs are per *scope*, not per key, so that separation cannot
be enforced by ACLs alone. It is enforced by what each app.yaml asks for and
what each app's code imports - the dashboard never imports config.py at all. If
you want it enforced properly, split into two scopes; this script prints a note
about that at the end.

Every value is read with getpass, so nothing is echoed, written to disk, or left
in shell history - and nothing credential-shaped ever lands in this repo.

Run from a Databricks notebook terminal, or locally with the Databricks CLI
configured:

    python setup_secrets.py
    python setup_secrets.py --only massive-api-key    # rotate one value
"""

import argparse
import getpass

from databricks.sdk import WorkspaceClient
from databricks.sdk.service import workspace

SCOPE = "capstone"

# key -> (prompt, required)
SECRETS = {
    "lakebase-url": (
        "App Lakebase URL "
        "(postgresql://role:password@host:5432/capstone?sslmode=require)",
        True,
    ),
    "lakebase-tracing-url": (
        "Tracing Lakebase URL "
        "(postgresql://role:password@host:5432/capstone_tracing?sslmode=require)",
        True,
    ),
    "massive-api-key": (
        "Massive API key (https://massive.com/dashboard/keys) - blank to skip, "
        "the news pipeline is then disabled",
        False,
    ),
    "alpaca-api-key-id": (
        "Alpaca PAPER key id (https://app.alpaca.markets/paper/dashboard/overview) "
        "- blank to skip, trading is then disabled",
        False,
    ),
    "alpaca-secret-key": ("Alpaca PAPER secret key - blank to skip", False),
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Store the capstone secrets.")
    parser.add_argument(
        "--only", nargs="*", choices=sorted(SECRETS), help="store only these keys"
    )
    parser.add_argument(
        "--grant-only", action="store_true", help="skip the values, just grant access"
    )
    args = parser.parse_args()

    w = WorkspaceClient()

    try:
        w.secrets.create_scope(scope=SCOPE)
        print(f"Created secret scope '{SCOPE}'")
    except Exception as err:  # scope already exists on a re-run
        print(f"Scope '{SCOPE}' already exists ({err})")

    if not args.grant_only:
        wanted = args.only or list(SECRETS)
        for key in wanted:
            prompt, required = SECRETS[key]
            value = getpass.getpass(f"\n{prompt}\n  {SCOPE}/{key}: ").strip()

            if not value:
                if required:
                    raise SystemExit(f"{key} is required - nothing was stored for it.")
                print(f"  skipped {key}")
                continue

            w.secrets.put_secret(scope=SCOPE, key=key, string_value=value)
            print(f"  stored {SCOPE}/{key}")

    # ---- ACLs. Each app runs as its own service principal and needs READ.
    print(
        "\nNow grant READ to each app and job identity.\n"
        "Deploy the apps first, then paste each service principal UUID (found on\n"
        "the app's page in the Databricks UI). Job identities work here too.\n"
        "Press Enter on a blank line to finish."
    )
    granted = 0
    while True:
        principal = input("Service principal UUID (blank to finish): ").strip()
        if not principal:
            break
        try:
            w.secrets.put_acl(
                scope=SCOPE,
                principal=principal,
                permission=workspace.AclPermission.READ,
            )
            granted += 1
            print(f"  granted READ on '{SCOPE}' to '{principal}'")
        except Exception as err:
            print(f"  could not grant READ to '{principal}': {err}")
            print("  deploy the app first, then re-run with --grant-only.")

    print(f"\nDone. {granted} principal(s) granted READ on '{SCOPE}'.")
    print(
        "\nNote: Databricks secret ACLs are per scope, so every principal granted\n"
        "here can read every key in it - including the Alpaca keys, which only the\n"
        "app and the MCP server actually need. If that matters to you, split the\n"
        "tracing URL into its own scope (e.g. 'capstone-tracing') and grant the\n"
        "dashboard only that one; the dashboard reads\n"
        "LAKEBASE_SECRET_SCOPE from its app.yaml, so it is a one-line change."
    )


if __name__ == "__main__":
    main()
