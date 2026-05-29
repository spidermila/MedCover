"""Basic smoke tests to verify the app factory and routes are wired up."""


def test_app_creates_successfully(app):
    assert app is not None


def test_login_page_returns_200(client):
    response = client.get("/auth/login")
    assert response.status_code == 200


def test_unknown_route_returns_404(client):
    response = client.get("/nonexistent")
    assert response.status_code == 404


def test_app_is_in_testing_mode(app):
    assert app.config["TESTING"] is True


# ── external_url_for ─────────────────────────────────────────────────────────


class TestExternalUrlFor:
    """Test the external_url_for utility honours app_base_url from AppSettings."""

    def test_falls_back_to_flask_external_when_no_base_url(self, app):
        """Without app_base_url configured, must return a valid absolute URL."""
        from app.utils import external_url_for
        from app.models.settings import get_settings

        with app.test_request_context("/"):
            settings = get_settings()
            settings.app_base_url = None

            url = external_url_for("auth.login")
            assert url.startswith("http")
            assert "/auth/login" in url

    def test_uses_configured_base_url(self, app):
        """When app_base_url is set, it must be used as the URL base."""
        from app.utils import external_url_for
        from app.models.settings import get_settings
        from app.extensions import db

        with app.test_request_context("/"):
            settings = get_settings()
            settings.app_base_url = "https://medcoverdev.example.com"
            db.session.flush()

            url = external_url_for("auth.login")
            assert url == "https://medcoverdev.example.com/auth/login"

            # Restore
            settings.app_base_url = None
            db.session.flush()

    def test_base_url_trailing_slash_stripped(self, app):
        """Trailing slash on base URL must not produce double slashes."""
        from app.utils import external_url_for
        from app.models.settings import get_settings
        from app.extensions import db

        with app.test_request_context("/"):
            settings = get_settings()
            settings.app_base_url = "https://medcoverdev.example.com/"
            db.session.flush()

            url = external_url_for("auth.login")
            assert "//auth" not in url
            assert url == "https://medcoverdev.example.com/auth/login"

            settings.app_base_url = None
            db.session.flush()


# ── Changelog route ───────────────────────────────────────────────────────────


class TestChangelog:
    def test_anonymous_redirected(self, client):
        rv = client.get("/changelog", follow_redirects=False)
        assert rv.status_code in (301, 302)

    def test_member_can_view(self, app, member_client):
        rv = member_client.get("/changelog")
        assert rv.status_code == 200
        assert "Změny ve verzích".encode() in rv.data
        assert app.config["APP_VERSION"].encode() in rv.data

    def test_admin_can_view(self, app, admin_client):
        rv = admin_client.get("/changelog")
        assert rv.status_code == 200
        assert app.config["APP_VERSION"].encode() in rv.data

    def test_target_blank_has_noopener(self, app, member_client):
        """Every target='_blank' link must have rel='noopener noreferrer' (tabnabbing protection)."""
        import re
        rv = member_client.get("/changelog")
        html = rv.data.decode()
        blanks = re.findall(r"<a [^>]*target=\"_blank\"[^>]*>", html)
        assert blanks, "Expected at least one target=_blank link"
        for tag in blanks:
            assert 'rel="noopener noreferrer"' in tag, f"Missing rel noopener: {tag}"


# ── APP_VERSION config ────────────────────────────────────────────────────────


def test_app_version_config(app):
    # Verify APP_VERSION is a non-empty semver-like string read from the VERSION file.
    version = app.config["APP_VERSION"]
    assert version and version != "unknown"
    parts = version.split(".")
    assert len(parts) == 3 and all(p.isdigit() for p in parts)
