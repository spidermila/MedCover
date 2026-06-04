"""Tests for the application settings route (/admin/settings/)."""

from unittest.mock import patch

import sqlalchemy as sa

from app.extensions import db
from app.models.audit import AuditLogEntry
from app.models.settings import AppSettings
from tests.conftest import _get_csrf


def _form(csrf: str, **overrides) -> dict:
    """Build a minimal valid POST body for the settings form."""
    data: dict = {
        "csrf_token": csrf,
        "org_name": "Testovací Org",
        "timezone": "Europe/Prague",
        "app_base_url": "https://example.com",
        "smtp_port": "587",
    }
    data.update(overrides)
    return data


# ── GET ───────────────────────────────────────────────────────────────────────


class TestAppSettingsGet:
    def test_admin_can_view(self, admin_client):
        resp = admin_client.get("/admin/settings/")
        assert resp.status_code == 200
        assert b"timezone" in resp.data.lower() or b"Nastaven" in resp.data

    def test_member_is_forbidden(self, member_client):
        resp = member_client.get("/admin/settings/")
        assert resp.status_code == 403

    def test_unauthenticated_redirects(self, client):
        resp = client.get("/admin/settings/", follow_redirects=False)
        assert resp.status_code == 302
        assert "login" in resp.headers["Location"]


# ── POST — save ───────────────────────────────────────────────────────────────


class TestAppSettingsSave:
    def test_valid_save_redirects(self, admin_client):
        csrf = _get_csrf(admin_client, "/admin/settings/")
        resp = admin_client.post("/admin/settings/", data=_form(csrf), follow_redirects=False)
        assert resp.status_code == 302

    def test_valid_save_persists_org_name(self, app, admin_client):
        csrf = _get_csrf(admin_client, "/admin/settings/")
        admin_client.post("/admin/settings/", data=_form(csrf, org_name="Nová Org"), follow_redirects=True)
        with app.app_context():
            settings = db.session.get(AppSettings, 1)
            assert settings.org_name == "Nová Org"

    def test_valid_save_persists_timezone(self, app, admin_client):
        csrf = _get_csrf(admin_client, "/admin/settings/")
        admin_client.post("/admin/settings/", data=_form(csrf, timezone="UTC"), follow_redirects=True)
        with app.app_context():
            settings = db.session.get(AppSettings, 1)
            assert settings.timezone == "UTC"

    def test_valid_save_writes_audit_log(self, app, admin_client):
        csrf = _get_csrf(admin_client, "/admin/settings/")
        admin_client.post("/admin/settings/", data=_form(csrf), follow_redirects=True)
        with app.app_context():
            entry = db.session.scalar(
                sa.select(AuditLogEntry).where(
                    AuditLogEntry.entity_type == "AppSettings",
                    AuditLogEntry.action_type == "edit",
                )
            )
        assert entry is not None

    def test_invalid_timezone_stays_on_page(self, admin_client):
        csrf = _get_csrf(admin_client, "/admin/settings/")
        resp = admin_client.post(
            "/admin/settings/",
            data=_form(csrf, timezone="Not/ATimezone"),
            follow_redirects=False,
        )
        assert resp.status_code == 200

    def test_smtp_password_saved_encrypted(self, app, admin_client):
        csrf = _get_csrf(admin_client, "/admin/settings/")
        admin_client.post(
            "/admin/settings/",
            data=_form(csrf, smtp_password="supersecret"),
            follow_redirects=True,
        )
        with app.app_context():
            settings = db.session.get(AppSettings, 1)
            assert settings.smtp_password_enc is not None
            assert "supersecret" not in settings.smtp_password_enc
            assert settings.get_smtp_password() == "supersecret"

    def test_feedback_enabled_checkbox_persists(self, app, admin_client):
        csrf = _get_csrf(admin_client, "/admin/settings/")
        admin_client.post("/admin/settings/", data=_form(csrf, feedback_enabled="1"), follow_redirects=True)
        with app.app_context():
            settings = db.session.get(AppSettings, 1)
            assert settings.feedback_enabled is True

    def test_feedback_disabled_when_unchecked(self, app, admin_client):
        """Omitting the checkbox key means unchecked → False."""
        with app.app_context():
            db.session.get(AppSettings, 1).feedback_enabled = True
            db.session.commit()
        csrf = _get_csrf(admin_client, "/admin/settings/")
        # feedback_enabled key absent → checkbox unchecked
        admin_client.post("/admin/settings/", data=_form(csrf), follow_redirects=True)
        with app.app_context():
            settings = db.session.get(AppSettings, 1)
            assert settings.feedback_enabled is False

    def test_member_cannot_post(self, member_client):
        resp = member_client.post("/admin/settings/", data={"timezone": "UTC"})
        assert resp.status_code == 403


# ── POST — action=test ────────────────────────────────────────────────────────


class TestAppSettingsTestEmail:
    def test_no_smtp_configured_flashes_warning(self, admin_client):
        csrf = _get_csrf(admin_client, "/admin/settings/")
        resp = admin_client.post(
            "/admin/settings/",
            data=_form(csrf, action="test"),
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"SMTP" in resp.data or b"smtp" in resp.data.lower()

    def test_smtp_configured_sends_email(self, app, admin_client):

        with app.app_context():
            settings = db.session.get(AppSettings, 1)
            settings.smtp_server = "smtp.example.com"
            settings.smtp_username = "user@example.com"
            settings.set_smtp_password("somepass")
            db.session.commit()

        csrf = _get_csrf(admin_client, "/admin/settings/")
        with patch("app.routes.app_settings.mail.send") as mock_send:
            resp = admin_client.post(
                "/admin/settings/",
                data=_form(
                    csrf,
                    action="test",
                    smtp_server="smtp.example.com",
                    smtp_username="user@example.com",
                ),
                follow_redirects=True,
            )
        assert resp.status_code == 200
        assert mock_send.called

    def test_smtp_send_failure_flashes_danger(self, app, admin_client):

        with app.app_context():
            settings = db.session.get(AppSettings, 1)
            settings.smtp_server = "smtp.example.com"
            settings.smtp_username = "user@example.com"
            settings.set_smtp_password("somepass")
            db.session.commit()

        csrf = _get_csrf(admin_client, "/admin/settings/")
        with patch("app.routes.app_settings.mail.send", side_effect=Exception("conn refused")):
            resp = admin_client.post(
                "/admin/settings/",
                data=_form(
                    csrf,
                    action="test",
                    smtp_server="smtp.example.com",
                    smtp_username="user@example.com",
                ),
                follow_redirects=True,
            )
        assert resp.status_code == 200
        assert "nezdařilo" in resp.data.decode() or "conn refused" in resp.data.decode()
