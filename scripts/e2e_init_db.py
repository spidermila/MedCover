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
"""

import sys
import time

import pyodbc
from e2e_db import IDENT_RE, parse_env, sa_conn_str

COLLATION = "Czech_100_CI_AS_SC_UTF8"
RECONNECT_ATTEMPTS = 30
RECONNECT_DELAY_S = 1


def main() -> int:
    target = parse_env()
    if not IDENT_RE.fullmatch(target.db):
        sys.exit(f"Invalid database name: {target.db}")
    if not IDENT_RE.fullmatch(target.user):
        sys.exit(f"Invalid username: {target.user}")

    conn = pyodbc.connect(sa_conn_str(target, "master"), autocommit=True)
    cursor = conn.cursor()
    cursor.execute(
        f"IF NOT EXISTS (SELECT 1 FROM sys.databases WHERE name='{target.db}') "
        f"CREATE DATABASE [{target.db}] COLLATE {COLLATION}"
    )
    cursor.execute(f"ALTER DATABASE [{target.db}] SET READ_COMMITTED_SNAPSHOT ON")
    safe_pwd = target.pwd.replace("'", "''")
    cursor.execute(
        f"IF NOT EXISTS (SELECT 1 FROM sys.server_principals WHERE name='{target.user}') "
        f"CREATE LOGIN [{target.user}] WITH PASSWORD='{safe_pwd}'"
    )
    conn.close()

    last_err: Exception | None = None
    for _ in range(RECONNECT_ATTEMPTS):
        try:
            db_conn = pyodbc.connect(sa_conn_str(target, target.db), autocommit=True, timeout=5)
            break
        except pyodbc.Error as e:
            last_err = e
            time.sleep(RECONNECT_DELAY_S)
    else:
        sys.exit(f"Database '{target.db}' never became reachable after ALTER: {last_err}")

    db_cursor = db_conn.cursor()
    db_cursor.execute(
        f"IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name='{target.user}') "
        f"BEGIN CREATE USER [{target.user}] FOR LOGIN [{target.user}]; "
        f"ALTER ROLE db_owner ADD MEMBER [{target.user}]; END"
    )
    db_conn.close()
    print(f"  Database '{target.db}' ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
