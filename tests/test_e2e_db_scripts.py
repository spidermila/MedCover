"""Tests for the extracted e2e MSSQL bootstrap scripts.

`scripts/e2e_wait_db.py` and `scripts/e2e_init_db.py` were previously inline
python heredocs in `scripts/e2e-entrypoint.sh`. These tests pin the two things
that are worth guarding without a live database:

  * the identifier allow-list used to interpolate db/user into DDL — this is
    the SQL-injection barrier for `CREATE DATABASE [{db}]` /
    `CREATE LOGIN [{user}]`;
  * the `DATABASE_URL` regex — a bad env var should fail fast with a clear
    message, not a cryptic pyodbc traceback.

We also run each script as a subprocess with a bad env to confirm the
"fail-fast on missing/malformed URL" behaviour survived the extraction. The
scripts' actual pyodbc calls are exercised by the e2e job on every CI run.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS_DIR / f"{name}.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_init = _load("e2e_init_db")
_wait = _load("e2e_wait_db")


# ── Identifier allow-list (SQL injection barrier) ─────────────────────────────


def test_ident_re_accepts_typical_identifiers():
    for good in ("medcover_e2e", "medcover_test_gw0", "_x", "App1", "MedCover"):
        assert _init.IDENT_RE.fullmatch(good), good


def test_ident_re_rejects_injection_and_bad_shapes():
    bad = [
        "foo;DROP DATABASE master--",
        "foo bar",
        "foo]",
        "foo-bar",
        "1abc",  # can't start with digit
        "",
        "foo.bar",
        "foo'; --",
    ]
    for value in bad:
        assert _init.IDENT_RE.fullmatch(value) is None, value


# ── DATABASE_URL parser ───────────────────────────────────────────────────────


def test_url_re_parses_full_mssql_url():
    m = _init.DATABASE_URL_RE.match(
        "mssql+pyodbc://medcover:E2e_Password1!@db-e2e:1433/medcover_e2e"
        "?driver=ODBC+Driver+18+for+SQL+Server&Encrypt=no"
    )
    assert m is not None
    user, pwd, host, port, db = m.groups()
    assert (user, host, port, db) == ("medcover", "db-e2e", "1433", "medcover_e2e")
    assert pwd == "E2e_Password1!"


def test_url_re_parses_url_without_query_string():
    m = _init.DATABASE_URL_RE.match("mssql+pyodbc://sa:p@h:1433/db")
    assert m is not None
    assert m.groups() == ("sa", "p", "h", "1433", "db")


def test_url_re_and_wait_share_the_same_regex():
    # The two scripts must agree on what a valid URL looks like — otherwise
    # wait_db could green-light a URL that init_db then rejects (or vice versa).
    assert _wait.DATABASE_URL_RE.pattern == _init.DATABASE_URL_RE.pattern


def test_url_re_rejects_junk():
    for bad in ("", "bogus", "postgresql://x:y@h:1/db", "mssql+pyodbc://no-at-sign/db"):
        assert _init.DATABASE_URL_RE.match(bad) is None, bad


# ── End-to-end: scripts still fail fast on bad env ────────────────────────────


def _run(script: str, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / script)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def test_wait_db_exits_nonzero_with_clear_message_on_missing_url():
    result = _run("e2e_wait_db.py", env={})
    assert result.returncode != 0
    assert "Cannot parse MSSQL DATABASE_URL" in (result.stderr + result.stdout)


def test_init_db_exits_nonzero_with_clear_message_on_missing_url():
    result = _run("e2e_init_db.py", env={})
    assert result.returncode != 0
    assert "Cannot parse MSSQL DATABASE_URL" in (result.stderr + result.stdout)


def test_init_db_rejects_invalid_database_identifier():
    # Password parses as ok (regex takes anything up to '@'); the db name
    # 'bad;name' must be caught by the IDENT_RE guard before any SQL runs.
    result = _run(
        "e2e_init_db.py",
        env={"DATABASE_URL": "mssql+pyodbc://sa:p@h:1433/bad;name"},
    )
    assert result.returncode != 0
    assert "Invalid database name" in (result.stderr + result.stdout)


def test_init_db_rejects_invalid_username():
    result = _run(
        "e2e_init_db.py",
        env={"DATABASE_URL": "mssql+pyodbc://bad user:p@h:1433/dbname"},
    )
    assert result.returncode != 0
    assert "Invalid username" in (result.stderr + result.stdout)
