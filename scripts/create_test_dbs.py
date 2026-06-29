#!/usr/bin/env python3
"""Create the CI/test MSSQL databases (base + xdist worker DBs).

Extracted out of `.github/workflows/ci.yml` so the logic isn't Python-inside-
shell-inside-YAML (hard to lint, test, and maintain). Run it before pytest:

    MSSQL_SA_PASSWORD=... python scripts/create_test_dbs.py [db_name ...]

Connection target is taken from the environment (with CI-friendly defaults):
    MSSQL_HOST          server host        (default: localhost)
    MSSQL_PORT          server port        (default: 1433)
    MSSQL_SA_USER       admin login        (default: SA)
    MSSQL_SA_PASSWORD   admin password     (required)

If no database names are given on the command line, the standard test set is
created: the base DB plus one per pytest-xdist worker (gw0..gw3).

RCSI (READ_COMMITTED_SNAPSHOT) is intentionally NOT enabled — standard READ
COMMITTED makes committed rows immediately visible to all connections, which
avoids the snapshot-gap flakiness we hit in CI. (Production enables RCSI via
mssql-init/setup.sh.)
"""

import os
import sys
import time

import pyodbc

DEFAULT_DBS = [
    "medcover_test",
    "medcover_test_gw0",
    "medcover_test_gw1",
    "medcover_test_gw2",
    "medcover_test_gw3",
]

COLLATION = "Czech_100_CI_AS_SC_UTF8"


def _connect_with_retry(conn_str: str, attempts: int = 30, delay: int = 2) -> pyodbc.Connection:
    last_exc: Exception | None = None
    for _ in range(attempts):
        try:
            conn = pyodbc.connect(conn_str)
            conn.autocommit = True
            return conn
        except Exception as exc:  # noqa: BLE001 — any driver/connection error is retryable here
            last_exc = exc
            time.sleep(delay)
    raise RuntimeError(f"MSSQL not reachable after {attempts} attempts: {last_exc}") from last_exc


def main(argv: list[str]) -> int:
    host = os.environ.get("MSSQL_HOST", "localhost")
    port = os.environ.get("MSSQL_PORT", "1433")
    user = os.environ.get("MSSQL_SA_USER", "SA")
    password = os.environ.get("MSSQL_SA_PASSWORD")
    if not password:
        sys.exit("ERROR: MSSQL_SA_PASSWORD must be set.")

    db_names = argv[1:] or DEFAULT_DBS

    # Brace-wrap UID/PWD so values containing ';' or other ODBC delimiters parse correctly.
    conn_str = (
        f"DRIVER={{ODBC Driver 18 for SQL Server}};SERVER={host},{port};"
        f"DATABASE=master;UID={{{user}}};PWD={{{password}}};Encrypt=no;TrustServerCertificate=yes"
    )

    conn = _connect_with_retry(conn_str)
    try:
        cursor = conn.cursor()
        for db_name in db_names:
            cursor.execute(
                f"IF NOT EXISTS (SELECT 1 FROM sys.databases WHERE name='{db_name}') "
                f"CREATE DATABASE [{db_name}] COLLATE {COLLATION}"
            )
            print(f"{db_name} ready")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
