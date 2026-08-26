#!/usr/bin/env python3
"""Ping the MSSQL server used by the e2e stack.

Extracted from `scripts/e2e-entrypoint.sh` so the wait logic isn't
Python-inside-shell (hard to lint, test, and maintain). The retry/backoff loop
stays in the shell entrypoint so operators keep the familiar
"attempt N/MAX, retrying in 2s" output; this script performs a single
connection attempt and exits non-zero if the server isn't reachable yet.

Connection target is parsed from `DATABASE_URL` (mssql+pyodbc://...). The
admin password falls back to `MSSQL_SA_PASSWORD` when set (CI uses a distinct
SA password from the app user).
"""

import os
import re
import sys

import pyodbc

DATABASE_URL_RE = re.compile(r"mssql\+pyodbc://([^:]+):([^@]+)@([^:]+):(\d+)/([^?]+)")


def main() -> int:
    url = os.environ.get("DATABASE_URL", "")
    m = DATABASE_URL_RE.match(url)
    if not m:
        sys.exit(f"Cannot parse MSSQL DATABASE_URL: {url}")
    _user, pwd, host, port, _db = m.groups()
    sa_pwd = os.environ.get("MSSQL_SA_PASSWORD", pwd)
    conn_str = (
        f"DRIVER={{ODBC Driver 18 for SQL Server}};SERVER={host},{port};"
        f"DATABASE=master;UID=sa;PWD={sa_pwd};Encrypt=no;TrustServerCertificate=yes"
    )
    pyodbc.connect(conn_str, timeout=5).close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
