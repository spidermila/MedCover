"""Unit tests for Config classes."""

import os
import warnings
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
        env["DATABASE_URL"] = "postgresql://localhost/testdb"
        with patch.dict(os.environ, env, clear=True):
            try:
                DevelopmentConfig.init_app(object())
                assert False, "Expected RuntimeError"
            except RuntimeError as exc:
                assert "SECRET_KEY" in str(exc)

    def test_no_raise_when_both_set(self):

        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://x/y", "SECRET_KEY": "s"}):
            DevelopmentConfig.init_app(object())  # must not raise


class TestProductionConfigInitApp:
    def test_warns_when_sslmode_missing(self):

        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://localhost/prod"}):
            with warnings.catch_warnings(record=True) as w:
                warnings.simplefilter("always")
                ProductionConfig.init_app(object())
            assert any("sslmode" in str(warning.message) for warning in w)

    def test_no_warn_when_sslmode_present(self):

        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://localhost/prod?sslmode=require"}):
            with warnings.catch_warnings(record=True) as w:
                warnings.simplefilter("always")
                ProductionConfig.init_app(object())
            assert not any("sslmode" in str(warning.message) for warning in w)

    def test_no_warn_when_database_url_empty(self):
        """Empty DATABASE_URL should not trigger the warning (setup wizard case)."""

        with patch.dict(os.environ, {"DATABASE_URL": ""}):
            with warnings.catch_warnings(record=True) as w:
                warnings.simplefilter("always")
                ProductionConfig.init_app(object())
            assert not any("sslmode" in str(warning.message) for warning in w)
