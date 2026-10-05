"""Basic smoke tests to verify the app factory and routes are wired up."""

import re

from app.utils import external_url_for


def test_app_creates_successfully(app):
    assert app is not None


def test_login_page_returns_200(client):
    response = client.get("/auth/login")
    assert response.status_code == 200


def test_unknown_route_returns_404(client):
    response = client.get("/nonexistent")
    assert response.status_code == 404
    assert "Stránka nenalezena".encode() in response.data


def test_app_is_in_testing_mode(app):
    assert app.config["TESTING"] is True


def test_403_page_renders_czech_message(member_client, app):
    """Any 403 response should render the Czech error page instead of the default Werkzeug HTML."""
    # /events/create requires event.create permission, which a Member lacks.
    response = member_client.get("/events/create")
    assert response.status_code == 403
    assert "Nemáte oprávnění".encode() in response.data


# ── external_url_for / canonical host ────────────────────────────────────────


class TestAppBaseUrl:
    def test_external_url_falls_back_to_request_host(self, app):
        with app.test_request_context("/"):
            assert external_url_for("auth.login") == "http://localhost/auth/login"

    def test_external_url_uses_app_base_url(self, app, monkeypatch):
        monkeypatch.setitem(app.config, "APP_BASE_URL", "https://medcover.example.com")
        with app.test_request_context("/"):
            assert external_url_for("auth.login") == "https://medcover.example.com/auth/login"

    def test_other_host_redirected_with_path_and_query(self, app, client, monkeypatch):
        monkeypatch.setitem(app.config, "APP_BASE_URL", "https://medcover.example.com")
        rv = client.get("/auth/login?next=/events", base_url="http://dozory.example.com")
        assert rv.status_code == 302
        assert rv.headers["Location"] == "https://medcover.example.com/auth/login?next=/events"

    def test_redirect_keeps_encoded_path_characters(self, app, client, monkeypatch):
        monkeypatch.setitem(app.config, "APP_BASE_URL", "https://medcover.example.com")
        rv = client.get("/a%3Fb%23c", base_url="http://dozory.example.com")
        assert rv.headers["Location"] == "https://medcover.example.com/a%3Fb%23c"

    def test_canonical_host_and_health_not_redirected(self, app, client, monkeypatch):
        monkeypatch.setitem(app.config, "APP_BASE_URL", "https://medcover.example.com")
        assert client.get("/auth/login", base_url="https://medcover.example.com").status_code == 200
        assert client.get("/auth/login", base_url="https://MedCover.example.com").status_code == 200
        assert client.get("/auth/login", base_url="http://medcover.example.com:443").status_code == 200
        assert client.get("/health", base_url="http://10.0.0.5:5000").status_code == 200


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
