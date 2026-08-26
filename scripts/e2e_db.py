"""Shared helpers for the e2e MSSQL bootstrap scripts.

`e2e_wait_db.py` and `e2e_init_db.py` both parse the same `DATABASE_URL`
and build the same pyodbc connection strings. Centralising here keeps them
from drifting.

`MSSQL_SA_PASSWORD`, when set, overrides the app-user password from the
URL when connecting as SA (CI uses a distinct SA password from the app
user); when unset, the URL password is reused.
"""

import os
import re
import sys
from typing import NamedTuple

DATABASE_URL_RE = re.compile(r"mssql\+pyodbc://([^:]+):([^@]+)@([^:]+):(\d+)/([^?]+)")
IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class DbTarget(NamedTuple):
    user: str
    pwd: str
    host: str
    port: str
    db: str
    sa_pwd: str


def parse_env() -> DbTarget:
    """Parse `DATABASE_URL` from the environment; exit non-zero on failure."""
    url = os.environ.get("DATABASE_URL", "")
    m = DATABASE_URL_RE.match(url)
    if not m:
        sys.exit(f"Cannot parse MSSQL DATABASE_URL: {url}")
    user, pwd, host, port, db = m.groups()
    sa_pwd = os.environ.get("MSSQL_SA_PASSWORD", pwd)
    return DbTarget(user=user, pwd=pwd, host=host, port=port, db=db, sa_pwd=sa_pwd)


def sa_conn_str(target: DbTarget, database: str) -> str:
    """Build a pyodbc connection string authenticated as SA against `database`."""
    return (
        f"DRIVER={{ODBC Driver 18 for SQL Server}};SERVER={target.host},{target.port};"
        f"DATABASE={database};UID=sa;PWD={target.sa_pwd};Encrypt=no;TrustServerCertificate=yes"
    )
