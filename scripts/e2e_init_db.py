#!/usr/bin/env python3
"""Create the e2e MSSQL database, login, and app user.

Extracted from `scripts/e2e-entrypoint.sh` so the init logic isn't
Python-inside-shell (hard to lint, test, and maintain).

Steps (all idempotent):
    1. Connect to `master` as SA and CREATE DATABASE (if missing) with the
       Czech collation, then flip on READ_COMMITTED_SNAPSHOT.
    2. Create the app LOGIN (if missing).
    3. Reconnect to the new database and create the matching USER, adding it
       to `db_owner`.

Step 3 needs its own retry loop because `SET READ_COMMITTED_SNAPSHOT ON`
briefly takes the database offline waiting for existing connections to drain;
a race with our immediate reconnect otherwise surfaces as
"Cannot open database ... The login failed. (4060)".

Connection target is parsed from `DATABASE_URL` (mssql+pyodbc://...); the
admin password falls back to `MSSQL_SA_PASSWORD` when set.
"""

import os
import re
import sys
import time

import pyodbc

DATABASE_URL_RE = re.compile(r"mssql\+pyodbc://([^:]+):([^@]+)@([^:]+):(\d+)/([^?]+)")
IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

COLLATION = "Czech_100_CI_AS_SC_UTF8"
RECONNECT_ATTEMPTS = 30
RECONNECT_DELAY_S = 1


def main() -> int:
    url = os.environ.get("DATABASE_URL", "")
    m = DATABASE_URL_RE.match(url)
    if not m:
        sys.exit(f"Cannot parse MSSQL DATABASE_URL: {url}")
    user, pwd, host, port, db = m.groups()
    if not IDENT_RE.fullmatch(db):
        sys.exit(f"Invalid database name: {db}")
    if not IDENT_RE.fullmatch(user):
        sys.exit(f"Invalid username: {user}")
    sa_pwd = os.environ.get("MSSQL_SA_PASSWORD", pwd)

    master_conn_str = (
        f"DRIVER={{ODBC Driver 18 for SQL Server}};SERVER={host},{port};"
        f"DATABASE=master;UID=sa;PWD={sa_pwd};Encrypt=no;TrustServerCertificate=yes"
    )
    db_conn_str = (
        f"DRIVER={{ODBC Driver 18 for SQL Server}};SERVER={host},{port};"
        f"DATABASE={db};UID=sa;PWD={sa_pwd};Encrypt=no;TrustServerCertificate=yes"
    )

    conn = pyodbc.connect(master_conn_str, autocommit=True)
    cursor = conn.cursor()
    cursor.execute(
        f"IF NOT EXISTS (SELECT 1 FROM sys.databases WHERE name='{db}') " f"CREATE DATABASE [{db}] COLLATE {COLLATION}"
    )
    cursor.execute(f"ALTER DATABASE [{db}] SET READ_COMMITTED_SNAPSHOT ON")
    safe_pwd = pwd.replace("'", "''")
    cursor.execute(
        f"IF NOT EXISTS (SELECT 1 FROM sys.server_principals WHERE name='{user}') "
        f"CREATE LOGIN [{user}] WITH PASSWORD='{safe_pwd}'"
    )
    conn.close()

    last_err: Exception | None = None
    db_conn: pyodbc.Connection | None = None
    for _ in range(RECONNECT_ATTEMPTS):
        try:
            db_conn = pyodbc.connect(db_conn_str, autocommit=True, timeout=5)
            break
        except pyodbc.Error as e:
            last_err = e
            time.sleep(RECONNECT_DELAY_S)
    if db_conn is None:
        sys.exit(f"Database '{db}' never became reachable after ALTER: {last_err}")

    db_cursor = db_conn.cursor()
    db_cursor.execute(
        f"IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name='{user}') "
        f"BEGIN CREATE USER [{user}] FOR LOGIN [{user}]; "
        f"ALTER ROLE db_owner ADD MEMBER [{user}]; END"
    )
    db_conn.close()
    print(f"  Database '{db}' ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
