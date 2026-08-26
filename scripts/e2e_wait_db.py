#!/usr/bin/env python3
"""Ping the MSSQL server used by the e2e stack.

Extracted from `scripts/e2e-entrypoint.sh` so the wait logic isn't
Python-inside-shell (hard to lint, test, and maintain). The retry/backoff loop
stays in the shell entrypoint so operators keep the familiar
"attempt N/MAX, retrying in 2s" output; this script performs a single
connection attempt and exits non-zero if the server isn't reachable yet.
"""

import pyodbc
from e2e_db import parse_env, sa_conn_str


def main() -> int:
    target = parse_env()
    pyodbc.connect(sa_conn_str(target, "master"), timeout=5).close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
