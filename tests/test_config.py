"""Unit tests for Config classes."""

import os
from unittest.mock import patch

from app.config import DevelopmentConfig, ProductionConfig


class TestDevelopmentConfigInitApp:
    def test_raises_without_database_url(self):

        env = {k: v for k, v in os.environ.items() if k not in ("DATABASE_URL",)}
        env["SECRET_KEY"] = "test-key"
        with patch.dict(os.environ, env, clear=True):
            try:
                DevelopmentConfig.init_app(object())
                assert False, "Expected RuntimeError"
            except RuntimeError as exc:
                assert "DATABASE_URL" in str(exc)

    def test_raises_without_secret_key(self):

        env = {k: v for k, v in os.environ.items() if k not in ("SECRET_KEY",)}
        env["DATABASE_URL"] = "mssql+pyodbc://SA:pwd@localhost:1433/testdb?driver=ODBC+Driver+18+for+SQL+Server"
        with patch.dict(os.environ, env, clear=True):
            try:
                DevelopmentConfig.init_app(object())
                assert False, "Expected RuntimeError"
            except RuntimeError as exc:
                assert "SECRET_KEY" in str(exc)

    def test_no_raise_when_both_set(self):

        with patch.dict(
            os.environ,
            {
                "DATABASE_URL": "mssql+pyodbc://SA:pwd@localhost:1433/db?driver=ODBC+Driver+18+for+SQL+Server",
                "SECRET_KEY": "s",
            },
        ):
            DevelopmentConfig.init_app(object())  # must not raise


class TestProductionConfigInitApp:
    def test_raises_when_encrypt_missing(self):

        _url = "mssql+pyodbc://SA:pwd@server.database.windows.net:1433/db" "?driver=ODBC+Driver+18+for+SQL+Server"
        with patch.dict(os.environ, {"DATABASE_URL": _url}):
            try:
                ProductionConfig.init_app(object())
                assert False, "Expected RuntimeError"
            except RuntimeError as exc:
                assert "Encrypt=yes" in str(exc)

    def test_no_raise_when_encrypt_present(self):

        _url = (
            "mssql+pyodbc://SA:pwd@server.database.windows.net:1433/db"
            "?driver=ODBC+Driver+18+for+SQL+Server&Encrypt=yes"
        )
        with patch.dict(os.environ, {"DATABASE_URL": _url}):
            ProductionConfig.init_app(object())  # must not raise

    def test_no_raise_when_msi(self):

        _url = (
            "mssql+pyodbc://@server.database.windows.net/db"
            "?driver=ODBC+Driver+18+for+SQL+Server&Authentication=ActiveDirectoryMsi"
        )
        with patch.dict(os.environ, {"DATABASE_URL": _url}):
            ProductionConfig.init_app(object())  # must not raise

    def test_no_raise_when_database_url_empty(self):
        """Empty DATABASE_URL should not raise (setup wizard case)."""

        with patch.dict(os.environ, {"DATABASE_URL": ""}):
            ProductionConfig.init_app(object())  # must not raise
