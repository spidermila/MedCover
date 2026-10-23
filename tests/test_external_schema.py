"""Exercise external-collaborator migrations against MSSQL, including old rows."""

import importlib.util
import json
import zipfile
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, text

from app.backup import downloaded_backup, export_to_zip, restore_from_zip
from app.extensions import db
from app.models.event import Event
from app.models.invite import RegistrationInvite
from app.models.role import Role
from app.models.settings import get_settings
from app.models.user import UserAccount
from tests.conftest import _make_event_with_spot, _make_user


def load_migration(filename):
    path = Path("migrations/versions") / filename
    spec = importlib.util.spec_from_file_location("external_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.op = Operations(MigrationContext.configure(db.session.connection()))
    return module


def test_invite_migration_preserves_rows_and_defaults(app):
    with app.app_context():
        admin = _make_user("admin@test.com", "Admin", Role.ADMIN)
        invite = RegistrationInvite(email="legacy@test.com", created_by_id=admin.id)
        db.session.add(invite)
        db.session.commit()
        invite_id = invite.id
        migration = load_migration("4a8b0c1d2e3f_external_collaborator_invites.py")
        migration.downgrade()
        try:
            assert "is_external" not in {
                c["name"] for c in inspect(db.session.connection()).get_columns("registration_invite")
            }
        finally:
            migration.upgrade()
            db.session.commit()
        assert (
            db.session.scalar(text("SELECT is_external FROM registration_invite WHERE id=:id"), {"id": invite_id}) == 0
        )
        assert db.session.scalar(text("SELECT COUNT(*) FROM role WHERE name='External'")) == 1


def test_role_downgrade_refuses_to_remove_assigned_external_role(app):
    with app.app_context():
        _make_user("external@test.com", "External", Role.EXTERNAL)
        migration = load_migration("4a8b0c1d2e3f_external_collaborator_invites.py")
        with pytest.raises(RuntimeError, match="External role"):
            migration.downgrade()


def test_event_migration_defaults_existing_events_to_automatic_window(app):
    event_id, _ = _make_event_with_spot(app)
    with app.app_context():
        migration = load_migration("5b9c1d2e3f40_external_event_opening.py")
        migration.downgrade()
        migration.upgrade()
        db.session.commit()
        assert db.session.scalar(text("SELECT open_to_externals FROM event WHERE id=:id"), {"id": event_id}) == 0


def test_notification_migration_defaults_existing_settings_to_enabled(app):
    with app.app_context():
        migration = load_migration("6c0d2e3f4051_external_opportunity_notifications.py")
        migration.downgrade()
        migration.upgrade()
        db.session.commit()
        assert db.session.scalar(text("SELECT notify_external_opportunities FROM app_settings WHERE id=1")) == 1


def test_external_scope_survives_backup_restore(app):
    event_id, _ = _make_event_with_spot(app)
    with app.app_context():
        user = _make_user("external@test.com", "External", Role.EXTERNAL)
        user_id = user.id
        invite = RegistrationInvite(email="friendly@test.com", created_by_id=user.id, is_external=True)
        db.session.add(invite)
        db.session.get(Event, event_id).open_to_externals = True
        db.session.commit()
        invite_id = invite.id
        get_settings().external_opportunity_days = 4
        db.session.commit()
        name = export_to_zip()
        get_settings().external_opportunity_days = 8
        db.session.commit()
        db.session.get(Event, event_id).open_to_externals = False
        invite.is_external = False
        user.roles = []
        db.session.commit()
        with downloaded_backup(name) as path:
            restore_from_zip(path)
        db.session.expire_all()
        assert db.session.get(UserAccount, user_id).is_external
        assert db.session.get(RegistrationInvite, invite_id).is_external
        assert db.session.get(Event, event_id).open_to_externals
        assert get_settings().external_opportunity_days == 8


def test_restoring_legacy_backup_keeps_external_role_assignable(app, tmp_path):
    event_id, _ = _make_event_with_spot(app)
    with app.app_context():
        admin = _make_user("admin@test.com", "Admin", Role.ADMIN)
        admin_id, admin_role_id = admin.id, admin.roles[0].id
        invite = RegistrationInvite(email="legacy@test.com", created_by_id=admin.id, is_external=True)
        db.session.add(invite)
        db.session.get(Event, event_id).open_to_externals = True
        db.session.commit()
        invite_id = invite.id
        get_settings().external_opportunity_days = 4
        db.session.commit()
        name = export_to_zip()
        get_settings().external_opportunity_days = 8
        db.session.commit()
        with downloaded_backup(name) as path:
            with zipfile.ZipFile(path) as archive:
                payload = json.loads(archive.read("backup.json"))
        tables = payload["tables"]
        tables["role"] = [role for role in tables["role"] if role["name"] != Role.EXTERNAL]
        for row in tables["event"]:
            row.pop("open_to_externals", None)
        for row in tables["registration_invite"]:
            row.pop("is_external", None)
        legacy_path = tmp_path / "legacy.zip"
        with zipfile.ZipFile(legacy_path, "w") as archive:
            archive.writestr("backup.json", json.dumps(payload))
        restore_from_zip(legacy_path)
        db.session.expire_all()
        assert not db.session.get(Event, event_id).open_to_externals
        assert get_settings().external_opportunity_days == 8
        assert not db.session.get(RegistrationInvite, invite_id).is_external
        restored_admin = db.session.get(UserAccount, admin_id)
        assert [role.id for role in restored_admin.roles] == [admin_role_id]
        external_role = db.session.scalar(db.select(Role).where(Role.name == Role.EXTERNAL))
        assert external_role is not None
        user = _make_user("friendly@test.com", "Friendly", Role.EXTERNAL)
        assert user.is_external
