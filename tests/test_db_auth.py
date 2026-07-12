"""Tests for app.db_auth — Azure SQL managed-identity URL rewriting.

These cover the pure URL/parsing logic; the token fetch and do_connect listener
require a live Azure identity endpoint and are not exercised here.
"""

from sqlalchemy.dialects.mssql import pyodbc as mssql_pyodbc
from sqlalchemy.engine import make_url

from app.db_auth import _extract_client_id, is_msi_url, rewrite_msi_url

CLIENT_ID = "712b31b7-ae43-41c9-9c32-69aca6dda993"

# Client id carried as the URL username (the form used in production).
MSI_URL_USERNAME = (
    f"mssql+pyodbc://{CLIENT_ID}@medcover-sql.database.windows.net/MedCover"
    "?driver=ODBC+Driver+18+for+SQL+Server&Authentication=ActiveDirectoryMsi&Encrypt=yes"
)

# Client id carried as a UID query parameter (the form shown in the docs).
MSI_URL_UID_QUERY = (
    "mssql+pyodbc://@medcover-sql.database.windows.net/MedCover"
    "?driver=ODBC+Driver+18+for+SQL+Server&Authentication=ActiveDirectoryMsi"
    f"&UID={CLIENT_ID}&Encrypt=yes"
)

# SQL authentication (local dev / tests) — must be left untouched.
SQL_AUTH_URL = (
    "mssql+pyodbc://SA:DevPassword123!@localhost:1433/medcover_test"
    "?driver=ODBC+Driver+18+for+SQL+Server&Encrypt=no&TrustServerCertificate=yes"
)


def test_is_msi_url_detects_managed_identity() -> None:
    assert is_msi_url(MSI_URL_USERNAME) is True
    assert is_msi_url(MSI_URL_UID_QUERY) is True


def test_is_msi_url_false_for_sql_auth() -> None:
    assert is_msi_url(SQL_AUTH_URL) is False
    assert is_msi_url("") is False


def test_extract_client_id_from_username_and_query() -> None:
    assert _extract_client_id(make_url(MSI_URL_USERNAME)) == CLIENT_ID
    assert _extract_client_id(make_url(MSI_URL_UID_QUERY)) == CLIENT_ID


def test_rewrite_msi_url_returns_client_id() -> None:
    for uri in (MSI_URL_USERNAME, MSI_URL_UID_QUERY):
        _, client_id = rewrite_msi_url(uri)
        assert client_id == CLIENT_ID


def test_rewrite_msi_url_produces_clean_odbc_string() -> None:
    """The rewritten URL must not carry driver-side auth options.

    Checking the actual pyodbc connect string guards against the two failure
    modes we hit in production: a driver ``Trusted_Connection=Yes`` (from an empty
    username) conflicting with Authentication, and the driver attempting its own
    (hanging) MSI token flow.
    """
    clean_url, _ = rewrite_msi_url(MSI_URL_USERNAME)
    cargs, _ = mssql_pyodbc.dialect().create_connect_args(make_url(clean_url))
    odbc = cargs[0]

    assert "Authentication" not in odbc
    assert "Trusted_Connection" not in odbc
    assert "medcover-sql.database.windows.net" in odbc
    assert "Database=MedCover" in odbc
    assert "Encrypt=yes" in odbc
