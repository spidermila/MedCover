"""Access-token authentication for Azure SQL via managed identity.

In Azure Container Apps the msodbcsql18 driver's ``Authentication=ActiveDirectoryMsi``
connection-string option cannot acquire a token: it targets the VM IMDS endpoint
(``169.254.169.254``), which Container Apps does not expose (identity is served via
``IDENTITY_ENDPOINT`` instead). The connect then hangs until "Login timeout expired".

To make managed-identity auth work on any Azure host, when ``DATABASE_URL`` uses
``Authentication=ActiveDirectoryMsi`` we:

1. rewrite the SQLAlchemy URL to a plain pyodbc ``odbc_connect`` string with no
   Authentication / UID / Trusted_Connection (so the driver does not run its own
   token flow, which also avoids the ``Cannot use Authentication option with
   Integrated Security`` conflict), and
2. attach a SQLAlchemy ``do_connect`` listener that fetches an Azure AD access
   token ourselves and hands it to pyodbc via the ``SQL_COPT_SS_ACCESS_TOKEN``
   connection attribute.

Local dev and tests use SQL authentication (user/password); ``is_msi_url`` returns
False for those, so none of this runs and their behaviour is unchanged.
"""

import json
import os
import struct
import threading
import time
import urllib.parse
import urllib.request
from typing import Any

from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import event
from sqlalchemy.engine import URL, make_url

# ODBC attribute id used to pass a pre-acquired access token to SQL Server.
SQL_COPT_SS_ACCESS_TOKEN = 1256
# Azure AD resource/audience for Azure SQL Database.
_TOKEN_RESOURCE = "https://database.windows.net/"
# Refresh the cached token this many seconds before it actually expires.
_EXPIRY_SKEW_SECONDS = 300

_token_lock = threading.Lock()
_token_cache: dict[str, tuple[str, int]] = {}  # client_id ("" if none) -> (token, expires_on)


def is_msi_url(uri: str) -> bool:
    """Return True if the connection string requests Azure AD managed-identity auth."""
    return "Authentication=ActiveDirectoryMsi" in (uri or "")


def _extract_client_id(url: URL) -> str | None:
    """The user-assigned identity's client id (URL username or ``UID`` query param)."""
    uid = url.query.get("UID")
    if isinstance(uid, tuple):
        uid = uid[0] if uid else None
    return url.username or uid


def rewrite_msi_url(uri: str) -> tuple[str, str | None]:
    """Return ``(clean_sqlalchemy_url, client_id)`` for token-based MSI auth.

    The clean URL is a pyodbc ``odbc_connect`` string carrying only the server,
    database and TLS options — no Authentication/UID — so the driver leaves
    authentication to the injected access token.
    """
    url = make_url(uri)
    client_id = _extract_client_id(url)
    driver = url.query.get("driver", "ODBC Driver 18 for SQL Server")
    if isinstance(driver, tuple):
        driver = driver[0]
    odbc = (
        f"Driver={{{driver}}};Server=tcp:{url.host},{url.port or 1433};"
        f"Database={url.database};Encrypt=yes;TrustServerCertificate=no"
    )
    clean = URL.create("mssql+pyodbc", query={"odbc_connect": odbc})
    return clean.render_as_string(hide_password=False), client_id


def _fetch_token(client_id: str | None) -> tuple[str, int]:
    """Fetch an Azure AD access token from the local managed-identity endpoint.

    Uses the App Service / Container Apps identity protocol when ``IDENTITY_ENDPOINT``
    is present, otherwise falls back to the VM IMDS endpoint.
    """
    identity_endpoint = os.environ.get("IDENTITY_ENDPOINT")
    if identity_endpoint:
        params = {"resource": _TOKEN_RESOURCE, "api-version": "2019-08-01"}
        if client_id:
            params["client_id"] = client_id
        request = urllib.request.Request(
            f"{identity_endpoint}?{urllib.parse.urlencode(params)}",
            headers={"X-IDENTITY-HEADER": os.environ.get("IDENTITY_HEADER", "")},
        )
    else:
        params = {"resource": _TOKEN_RESOURCE, "api-version": "2018-02-01"}
        if client_id:
            params["client_id"] = client_id
        request = urllib.request.Request(
            f"http://169.254.169.254/metadata/identity/oauth2/token?{urllib.parse.urlencode(params)}",
            headers={"Metadata": "true"},
        )
    with urllib.request.urlopen(request, timeout=15) as resp:  # noqa: S310  # trusted local endpoint
        data = json.load(resp)
    return data["access_token"], int(data["expires_on"])


def _get_token(client_id: str | None) -> str:
    """Return a cached access token, refreshing shortly before it expires."""
    key = client_id or ""
    now = int(time.time())
    with _token_lock:
        cached = _token_cache.get(key)
        if cached is not None and cached[1] - _EXPIRY_SKEW_SECONDS > now:
            return cached[0]
        token, expires_on = _fetch_token(client_id)
        _token_cache[key] = (token, expires_on)
        return token


def _token_struct(client_id: str | None) -> bytes:
    """Pack the access token into the byte structure pyodbc expects."""
    token = _get_token(client_id).encode("utf-16-le")
    return struct.pack(f"<I{len(token)}s", len(token), token)


def prepare_msi_auth(app: Flask) -> bool:
    """Rewrite an MSI ``DATABASE_URL`` to a token-friendly form before engine init.

    Returns True when managed-identity auth is in effect. No-op (returns False)
    for SQL-auth URLs used by local dev and tests.
    """
    uri = app.config.get("SQLALCHEMY_DATABASE_URI", "")
    if not is_msi_url(uri):
        return False
    clean_url, client_id = rewrite_msi_url(uri)
    app.config["SQLALCHEMY_DATABASE_URI"] = clean_url
    app.config["_MSI_AUTH"] = True
    app.config["_MSI_CLIENT_ID"] = client_id
    return True


def attach_msi_token_auth(app: Flask, db: SQLAlchemy) -> None:
    """Attach the access-token ``do_connect`` listener when MSI auth is in effect.

    Must be called after ``db.init_app(app)`` and before the first DB access.
    """
    if not app.config.get("_MSI_AUTH"):
        return
    client_id: str | None = app.config.get("_MSI_CLIENT_ID")
    with app.app_context():
        engine = db.engine

    @event.listens_for(engine, "do_connect")
    def _provide_token(dialect: Any, conn_rec: Any, cargs: Any, cparams: dict[str, Any]) -> None:
        cparams["attrs_before"] = {SQL_COPT_SS_ACCESS_TOKEN: _token_struct(client_id)}
