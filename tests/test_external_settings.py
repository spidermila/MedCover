"""Persistent admin configuration for the external opportunity window."""

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text

from app.extensions import db
from app.models.audit import AuditLogEntry
from app.models.settings import get_settings
from tests.test_app_settings import _form


@pytest.mark.parametrize("days", [1, 21, 365])
def test_window_is_editable_and_audited(app, admin_client, days):
    response = admin_client.get("/admin/settings/")
    assert b'name="external_opportunity_days"' in response.data
    assert admin_client.post("/admin/settings/", data=_form("", external_opportunity_days=str(days))).status_code == 302
    with app.app_context():
        assert get_settings().external_opportunity_days == days
        entry = db.session.scalar(db.select(AuditLogEntry).where(AuditLogEntry.entity_type == "AppSettings"))
        assert entry.changes_json["external_opportunity_days"] == [10, days]


@pytest.mark.parametrize("value", [None, "", "invalid", "1.5", "0", "-1", "366", "999999999999999999999999999"])
def test_invalid_window_leaves_all_settings_and_audit_unchanged(app, admin_client, value):
    with app.app_context():
        before = get_settings().org_name
    data = _form("", external_opportunity_days=value, org_name="Must not persist", smtp_password="Must not persist")
    if value is None:
        data.pop("external_opportunity_days")
    response = admin_client.post("/admin/settings/", data=data)
    assert response.status_code == 200
    assert "od 1 do 365" in response.get_data(as_text=True)
    with app.app_context():
        settings = get_settings()
        assert settings.org_name == before
        assert settings.external_opportunity_days == 10
        assert settings.smtp_password_enc is None
        assert db.session.scalar(db.select(AuditLogEntry).where(AuditLogEntry.entity_type == "AppSettings")) is None


def test_external_cannot_change_window(app, external_client):
    assert external_client.post("/admin/settings/", data=_form("", external_opportunity_days="21")).status_code == 403
    with app.app_context():
        assert get_settings().external_opportunity_days == 10


def test_window_change_requires_csrf(app, admin_client):
    app.config["WTF_CSRF_ENABLED"] = True
    try:
        assert admin_client.post("/admin/settings/", data=_form("", external_opportunity_days="21")).status_code == 400
    finally:
        app.config["WTF_CSRF_ENABLED"] = False
    with app.app_context():
        assert get_settings().external_opportunity_days == 10


def test_window_migration_defaults_existing_settings(app):
    path = Path("migrations/versions/7d1e3f405162_external_opportunity_window.py")
    spec = importlib.util.spec_from_file_location("window_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with app.app_context():
        migration.op = Operations(MigrationContext.configure(db.session.connection()))
        migration.downgrade()
        migration.upgrade()
        db.session.commit()
        assert db.session.scalar(text("SELECT external_opportunity_days FROM app_settings WHERE id=1")) == 10


def test_test_email_action_audits_window_change(app, admin_client):
    response = admin_client.post("/admin/settings/", data=_form("", external_opportunity_days="21", action="test"))
    assert response.status_code == 200
    with app.app_context():
        assert get_settings().external_opportunity_days == 21
        entry = db.session.scalar(db.select(AuditLogEntry).where(AuditLogEntry.entity_type == "AppSettings"))
        assert entry.changes_json["external_opportunity_days"] == [10, 21]
