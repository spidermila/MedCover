"""Tests for admin blueprint: dashboard, audit log."""
from app.extensions import db
from app.models.audit import AuditLogEntry
from app.utils import diff_changes


class TestDiffChanges:
    def test_returns_only_changed_fields(self):
        before = {"name": "A", "desc": "same"}
        after = {"name": "B", "desc": "same"}
        result = diff_changes(before, after)
        assert result == {"name": ["A", "B"]}
        assert "desc" not in result

    def test_empty_when_nothing_changed(self):
        before = {"x": 1, "y": 2}
        after = {"x": 1, "y": 2}
        assert diff_changes(before, after) == {}

    def test_all_fields_changed(self):
        before = {"a": "old", "b": "old2"}
        after = {"a": "new", "b": "new2"}
        result = diff_changes(before, after)
        assert result == {"a": ["old", "new"], "b": ["old2", "new2"]}

    def test_new_keys_in_after_included(self):
        before = {"a": "x"}
        after = {"a": "x", "b": "y"}
        result = diff_changes(before, after)
        assert "b" in result
        assert result["b"] == [None, "y"]

    def test_missing_keys_in_after(self):
        before = {"a": "x", "b": "y"}
        after = {"a": "x"}
        result = diff_changes(before, after)
        assert "b" in result
        assert result["b"] == ["y", None]


class TestAdminDashboard:
    def test_admin_dashboard_requires_login(self, client):
        response = client.get("/admin/", follow_redirects=False)
        assert response.status_code == 302

    def test_member_cannot_access_admin_dashboard(self, member_client):
        response = member_client.get("/admin/")
        assert response.status_code == 403

    def test_admin_can_access_dashboard(self, admin_client):
        response = admin_client.get("/admin/")
        assert response.status_code == 200

    def test_admin_dashboard_shows_service_status(self, admin_client):
        response = admin_client.get("/admin/")
        assert response.status_code == 200
        # Dashboard should contain DB status info
        assert b"DB" in response.data or "Databáze".encode() in response.data

    def test_admin_dashboard_has_audit_log_link(self, admin_client):
        response = admin_client.get("/admin/")
        assert b"audit-log" in response.data


class TestAppSettings:
    def test_settings_page_requires_admin(self, member_client):
        response = member_client.get("/admin/settings/")
        assert response.status_code == 403

    def test_settings_page_loads_for_admin(self, admin_client):
        response = admin_client.get("/admin/settings/")
        assert response.status_code == 200

    def test_settings_page_does_not_expose_smtp_password(self, app, admin_client):
        """The plaintext SMTP password must never appear in the settings page HTML."""
        from app.extensions import db
        from app.models.settings import get_settings
        from cryptography.fernet import Fernet

        # Configure a real (encrypted) SMTP password in settings
        test_password = "super_secret_smtp_pass_99"
        with app.app_context():
            settings = get_settings()
            key = Fernet.generate_key()
            f = Fernet(key)
            settings.smtp_password_encrypted = f.encrypt(test_password.encode()).decode()
            db.session.commit()

        response = admin_client.get("/admin/settings/")
        assert response.status_code == 200
        # The plaintext password must never appear in the HTML
        assert test_password.encode() not in response.data


class TestAuditLogUI:
    def test_audit_log_list_requires_login(self, client):
        response = client.get("/admin/audit-log/", follow_redirects=False)
        assert response.status_code == 302

    def test_member_cannot_access_audit_log(self, member_client):
        response = member_client.get("/admin/audit-log/")
        assert response.status_code == 403

    def test_admin_can_access_audit_log(self, admin_client):
        response = admin_client.get("/admin/audit-log/")
        assert response.status_code == 200

    def test_audit_log_shows_entries(self, app, admin_client):
        with app.app_context():
            entry = AuditLogEntry(
                action_type="create",
                entity_type="Event",
                entity_id="42",
                summary="Vytvořena akce 'Test'",
            )
            db.session.add(entry)
            db.session.commit()

        response = admin_client.get("/admin/audit-log/")
        assert "Vytvořena".encode() in response.data

    def test_audit_log_filter_by_entity_type(self, app, admin_client):
        with app.app_context():
            db.session.add(AuditLogEntry(
                action_type="create", entity_type="Event",
                entity_id="1", summary="Akce vytvořena",
            ))
            db.session.add(AuditLogEntry(
                action_type="edit", entity_type="MasterEvent",
                entity_id="2", summary="ME upravena",
            ))
            db.session.commit()

        response = admin_client.get("/admin/audit-log/?entity_type=Event")
        assert response.status_code == 200
        assert b"ME upravena" not in response.data

    def test_audit_log_detail_requires_admin(self, member_client):
        response = member_client.get("/admin/audit-log/1")
        assert response.status_code == 403

    def test_audit_log_detail_404_for_missing(self, admin_client):
        response = admin_client.get("/admin/audit-log/999999")
        assert response.status_code == 404

    def test_audit_log_detail_shows_diff(self, app, admin_client):
        with app.app_context():
            entry = AuditLogEntry(
                action_type="edit",
                entity_type="Event",
                entity_id="10",
                summary="Upravena akce",
                changes_json={"name": ["Starý název", "Nový název"]},
            )
            db.session.add(entry)
            db.session.commit()
            entry_id = entry.id

        response = admin_client.get(f"/admin/audit-log/{entry_id}")
        assert response.status_code == 200
        assert "Starý název".encode() in response.data
        assert "Nový název".encode() in response.data
