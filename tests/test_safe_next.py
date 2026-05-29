"""Tests for the safe_next URL validation helper."""
from __future__ import annotations

import pytest

from app.utils import safe_next


class TestSafeNext:
    """Verify safe_next rejects external/malicious URLs and accepts valid paths."""

    # ── Valid same-origin paths (should be returned as-is) ────────────────

    @pytest.mark.parametrize("url", [
        "/events/",
        "/events/?statuses=DRAFT&page=2",
        "/admin/users",
        "/",
    ])
    def test_valid_relative_paths_returned(self, app, url):
        with app.app_context():
            assert safe_next(url) == url

    # ── Malicious / external URLs (should fall back) ─────────────────────

    @pytest.mark.parametrize("url", [
        "https://evil.example.com",
        "http://evil.example.com/steal",
        "//evil.example.com",
        "//evil.example.com/path",
        "ftp://evil.example.com",
        "javascript:alert(1)",
    ])
    def test_external_urls_rejected(self, app, url):
        with app.app_context():
            result = safe_next(url)
            assert result != url
            assert "dashboard" in result

    # ── Empty / None values (should fall back) ───────────────────────────

    @pytest.mark.parametrize("url", [None, ""])
    def test_empty_values_fall_back(self, app, url):
        with app.app_context():
            result = safe_next(url)
            assert "dashboard" in result

    # ── Default fallback goes to dashboard ───────────────────────────────

    def test_default_fallback_is_dashboard(self, app):
        with app.app_context():
            result = safe_next(None)
            assert "/dashboard" in result

    # ── Custom fallback is used ──────────────────────────────────────────

    def test_custom_fallback_on_none(self, app):
        with app.app_context():
            result = safe_next(None, "/events/")
            assert result == "/events/"

    def test_custom_fallback_on_external_url(self, app):
        with app.app_context():
            result = safe_next("https://evil.example.com", "/events/")
            assert result == "/events/"

    def test_custom_fallback_on_scheme_relative(self, app):
        with app.app_context():
            result = safe_next("//evil.example.com", "/events/")
            assert result == "/events/"

    def test_valid_url_ignores_fallback(self, app):
        with app.app_context():
            result = safe_next("/admin/users", "/events/")
            assert result == "/admin/users"
