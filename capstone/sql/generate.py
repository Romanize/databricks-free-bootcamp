"""
Regenerate the .sql files in this folder from the DDL the apps actually run.

The apps create their own schema at startup (`schema.init_db()` /
`tracing.init_db()`), so these files are documentation, not a deployment step.
Generating them rather than hand-maintaining a copy is the point: a hand-written
mirror drifts, and a schema file that disagrees with the code is worse than none.

    python sql/generate.py
"""

import os
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "mcp_server"))

import schema  # noqa: E402
import tracing  # noqa: E402

HEADER = """-- Generated from schema.py / tracing.py - the apps run this same DDL at startup
-- via init_db(), so you never need to execute this by hand. It is here so the
-- schema can be reviewed, diffed, or created up front in the Lakebase SQL editor.
--
-- Regenerate with: python sql/generate.py
"""


def write(filename: str, subtitle: str, statements: list[str]) -> None:
    path = HERE / filename
    path.write_text(
        HEADER + subtitle + "\n\n" + ";\n\n".join(s.strip() for s in statements) + ";\n"
    )
    print(f"wrote {path.relative_to(HERE.parent)} ({os.path.getsize(path)} bytes)")


write("01_app_schema.sql", "-- Database: capstone (the app database)", schema.DDL)
write(
    "02_tracing_schema.sql",
    "-- Database: capstone_tracing (agent tool-call traces)",
    tracing.DDL,
)
