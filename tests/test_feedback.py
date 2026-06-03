"""Tests for the user feedback feature."""

import uuid

from flask import current_app

from app.extensions import db
from app.models.feedback import UserFeedback
from app.models.settings import get_settings

# ── Helpers ───────────────────────────────────────────────────────────────────


def _post_feedback(client, message: str = "Test zpráva", **extra):
    return client.post(
        "/feedback/submit",
        data={"message": message, "page_url": "/events/", **extra},
        follow_redirects=True,
    )


# ── Submit ────────────────────────────────────────────────────────────────────


class TestFeedbackSubmit:
    def test_anonymous_redirected_to_login(self, client):
        rv = client.get("/feedback/", follow_redirects=False)
        assert rv.status_code in (301, 302)
        assert "/auth/login" in rv.headers["Location"]

    def test_anonymous_post_redirected(self, client):
        rv = client.post("/feedback/submit", data={"message": "test"}, follow_redirects=False)
        assert rv.status_code in (301, 302)

    def test_member_can_see_form(self, app, member_client):
        rv = member_client.get("/feedback/")
        assert rv.status_code == 200
        assert "Zpětná vazba".encode() in rv.data

    def test_member_can_submit(self, app, member_client):
        rv = _post_feedback(member_client, "Tohle nefunguje správně.")
        assert rv.status_code == 200
        assert "Děkujeme".encode() in rv.data
        with app.app_context():
            entry = db.session.scalar(db.select(UserFeedback))
            assert entry is not None
            assert entry.message == "Tohle nefunguje správně."
            assert entry.page_url == "/events/"

    def test_empty_message_rejected(self, app, member_client):
        rv = _post_feedback(member_client, "  ")
        assert rv.status_code == 200
        assert "nesmí být prázdná".encode() in rv.data
        with app.app_context():
            assert db.session.scalar(db.select(UserFeedback)) is None

    def test_browser_info_stored(self, app, member_client):
        rv = member_client.post(
            "/feedback/submit",
            data={
                "message": "Zpráva",
                "page_url": "/events/",
                "user_agent": "Mozilla/5.0 TestBrowser",
                "screen_info": "1920×1080 (24-bit)",
            },
            follow_redirects=True,
        )
        assert rv.status_code == 200
        with app.app_context():
            entry = db.session.scalar(db.select(UserFeedback))
            assert entry.user_agent == "Mozilla/5.0 TestBrowser"
            assert entry.screen_info == "1920×1080 (24-bit)"

    def test_from_query_param_prefills_page_url(self, app, member_client):
        rv = member_client.get("/feedback/?from=/master_events/")
        assert rv.status_code == 200
        assert b"/master_events/" in rv.data


# ── Admin list / delete ───────────────────────────────────────────────────────


class TestFeedbackAdmin:
    def _seed_entry(self, app, member_client) -> str:
        """Submit one feedback entry and return its UUID as string."""
        _post_feedback(member_client, "Seeded entry")
        with app.app_context():
            entry = db.session.scalar(db.select(UserFeedback))
            return str(entry.id)

    def test_member_cannot_see_admin_list(self, app, member_client):
        rv = member_client.get("/admin/feedback/")
        assert rv.status_code == 403

    def test_admin_can_see_list(self, app, admin_client):
        rv = admin_client.get("/admin/feedback/")
        assert rv.status_code == 200
        assert "Zpětná vazba".encode() in rv.data

    def test_admin_list_shows_entries(self, app, admin_client, member_client):
        _post_feedback(member_client, "Viditelná zpráva")
        rv = admin_client.get("/admin/feedback/")
        assert "Viditelná zpráva".encode() in rv.data

    def test_member_cannot_delete(self, app, member_client):
        entry_id = self._seed_entry(app, member_client)
        rv = member_client.post(f"/admin/feedback/{entry_id}/delete", follow_redirects=False)
        assert rv.status_code == 403
        with app.app_context():
            assert db.session.get(UserFeedback, entry_id) is not None

    def test_admin_can_delete(self, app, admin_client, member_client):
        entry_id = self._seed_entry(app, member_client)
        rv = admin_client.post(f"/admin/feedback/{entry_id}/delete", follow_redirects=True)
        assert rv.status_code == 200
        assert "smazána".encode() in rv.data
        with app.app_context():
            assert db.session.get(UserFeedback, entry_id) is None

    def test_delete_blocked_when_dev_login_enabled(self, app, admin_client, member_client):
        entry_id = self._seed_entry(app, member_client)
        app.config["DEV_LOGIN_ENABLED"] = True
        try:
            rv = admin_client.post(f"/admin/feedback/{entry_id}/delete", follow_redirects=True)
            assert rv.status_code == 200
            assert "testovacím prostředí".encode() in rv.data
            with app.app_context():
                assert db.session.get(UserFeedback, entry_id) is not None
        finally:
            app.config["DEV_LOGIN_ENABLED"] = False

    def test_delete_button_hidden_when_dev_login_enabled(self, app, admin_client, member_client):
        _post_feedback(member_client, "Chráněná zpráva")
        app.config["DEV_LOGIN_ENABLED"] = True
        try:
            rv = admin_client.get("/admin/feedback/")
            assert rv.status_code == 200
            assert b"Smazat" not in rv.data
            assert "testovacím prostředí".encode() in rv.data
        finally:
            app.config["DEV_LOGIN_ENABLED"] = False

    def test_delete_works_when_dev_login_disabled(self, app, admin_client, member_client):
        """Sanity check: deletion still works when DEV_LOGIN_ENABLED is False (the default)."""
        app.config["DEV_LOGIN_ENABLED"] = False
        entry_id = self._seed_entry(app, member_client)
        rv = admin_client.post(f"/admin/feedback/{entry_id}/delete", follow_redirects=True)
        assert rv.status_code == 200
        assert "smazána".encode() in rv.data
        with app.app_context():
            assert db.session.get(UserFeedback, entry_id) is None

        fake_id = str(uuid.uuid4())
        rv = admin_client.post(f"/admin/feedback/{fake_id}/delete")
        assert rv.status_code == 404


# ── Commit hash (still in config for cache-busting) ──────────────────────────


class TestCommitHash:
    def test_admin_dashboard_shows_version(self, app, admin_client):
        rv = admin_client.get("/admin/")
        assert rv.status_code == 200
        assert app.config["APP_VERSION"].encode() in rv.data
        # GIT_COMMIT still present in config for static cache-busting
        assert b"dev" in rv.data

    def test_git_commit_config_default(self, app):
        with app.app_context():

            assert current_app.config["GIT_COMMIT"] == "dev"

    def test_app_version_config(self, app):
        with app.app_context():

            version = current_app.config["APP_VERSION"]
            assert version and version != "unknown"
            parts = version.split(".")
            assert len(parts) == 3 and all(p.isdigit() for p in parts)


# ── app_version stored in feedback ────────────────────────────────────────────


class TestFeedbackAppVersion:
    def test_app_version_stored_on_submit(self, app, member_client):
        _post_feedback(member_client, "Version test")
        with app.app_context():
            entry = db.session.scalar(db.select(UserFeedback))
            # app_version stores APP_VERSION (semantic version from VERSION file)
            assert entry.app_version == app.config["APP_VERSION"]

    def test_app_version_shown_in_admin_list(self, app, admin_client, member_client):
        _post_feedback(member_client, "Version visible")
        rv = admin_client.get("/admin/feedback/")
        assert rv.status_code == 200
        assert app.config["APP_VERSION"].encode() in rv.data


# ── feedback_enabled toggle ───────────────────────────────────────────────────


class TestFeedbackEnabled:
    def _set_enabled(self, app, enabled: bool) -> None:

        with app.app_context():
            s = get_settings()
            s.feedback_enabled = enabled
            db.session.commit()

    def test_form_accessible_when_enabled(self, app, member_client):
        self._set_enabled(app, True)
        rv = member_client.get("/feedback/")
        assert rv.status_code == 200

    def test_form_returns_404_when_disabled(self, app, member_client):
        self._set_enabled(app, False)
        rv = member_client.get("/feedback/")
        assert rv.status_code == 404

    def test_submit_returns_404_when_disabled(self, app, member_client):
        self._set_enabled(app, False)
        rv = member_client.post(
            "/feedback/submit",
            data={"message": "test", "page_url": "/"},
        )
        assert rv.status_code == 404
