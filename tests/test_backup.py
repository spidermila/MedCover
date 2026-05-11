"""Tests for backup/restore engine and backup management routes."""
from __future__ import annotations

import json
import zipfile
from datetime import datetime, timezone

import pytest

from app.extensions import db as _db
from app.models.role import Role
from app.models.settings import get_settings
from tests.conftest import _make_user, _login


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_csrf(client, url: str) -> str:
    """Fetch a page and extract the CSRF token from a hidden input."""
    resp = client.get(url)
    import re
    m = re.search(rb'name="csrf_token" value="([^"]+)"', resp.data)
    return m.group(1).decode() if m else ""


# ── Core engine tests ─────────────────────────────────────────────────────────

class TestExportToZip:
    def test_creates_zip_file(self, app, tmp_path):
        with app.app_context():
            from app.backup import export_to_zip
            path = export_to_zip(tmp_path)
        assert path.exists()
        assert path.suffix == ".zip"
        assert path.name.startswith("medcover_backup_")

    def test_zip_contains_backup_json(self, app, tmp_path):
        with app.app_context():
            from app.backup import export_to_zip
            path = export_to_zip(tmp_path)
        with zipfile.ZipFile(path) as zf:
            assert "backup.json" in zf.namelist()

    def test_backup_json_structure(self, app, tmp_path):
        with app.app_context():
            from app.backup import export_to_zip
            path = export_to_zip(tmp_path)
        with zipfile.ZipFile(path) as zf:
            payload = json.loads(zf.read("backup.json"))
        assert payload["version"] == "1.0"
        assert "schema_version" in payload
        assert "exported_at" in payload
        assert "tables" in payload

    def test_app_settings_excluded(self, app, tmp_path):
        with app.app_context():
            from app.backup import export_to_zip
            path = export_to_zip(tmp_path)
        with zipfile.ZipFile(path) as zf:
            payload = json.loads(zf.read("backup.json"))
        assert "app_settings" not in payload["tables"]
        assert "alembic_version" not in payload["tables"]

    def test_user_table_included(self, app, tmp_path):
        with app.app_context():
            _make_user("backup_test@example.com", "Backup User", Role.MEMBER)
            from app.backup import export_to_zip
            path = export_to_zip(tmp_path)
        with zipfile.ZipFile(path) as zf:
            payload = json.loads(zf.read("backup.json"))
        assert "user_account" in payload["tables"]
        emails = [row["email"] for row in payload["tables"]["user_account"]]
        assert "backup_test@example.com" in emails

    def test_creates_backup_dir_if_missing(self, app, tmp_path):
        new_dir = tmp_path / "nested" / "backups"
        with app.app_context():
            from app.backup import export_to_zip
            path = export_to_zip(new_dir)
        assert path.exists()


class TestRestoreFromZip:
    def test_restore_reloads_user(self, app, tmp_path):
        with app.app_context():
            _make_user("restore_target@example.com", "Restore Target", Role.MEMBER)
            from app.backup import export_to_zip
            zip_path = export_to_zip(tmp_path)

            # Delete the user and verify they're gone
            from app.models.user import UserAccount
            u = _db.session.scalars(
                _db.select(UserAccount).where(UserAccount.email == "restore_target@example.com")
            ).first()
            _db.session.delete(u)
            _db.session.commit()
            assert _db.session.scalars(
                _db.select(UserAccount).where(UserAccount.email == "restore_target@example.com")
            ).first() is None

            # Restore and verify user is back
            from app.backup import restore_from_zip
            restore_from_zip(zip_path)
            restored = _db.session.scalars(
                _db.select(UserAccount).where(UserAccount.email == "restore_target@example.com")
            ).first()
            assert restored is not None
            assert restored.name == "Restore Target"

    def test_restore_raises_on_missing_backup_json(self, app, tmp_path):
        zip_path = tmp_path / "bad.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("readme.txt", "not a backup")
        with app.app_context():
            from app.backup import restore_from_zip
            with pytest.raises(ValueError, match="backup.json"):
                restore_from_zip(zip_path)

    def test_restore_preserves_app_settings(self, app, tmp_path):
        """AppSettings must survive a restore (it is excluded from backup)."""
        with app.app_context():
            settings = get_settings()
            settings.org_name = "Pre-restore org"
            _db.session.commit()

            from app.backup import export_to_zip, restore_from_zip
            zip_path = export_to_zip(tmp_path)
            settings.org_name = "Changed after backup"
            _db.session.commit()

            restore_from_zip(zip_path)

            # AppSettings should retain "Changed after backup" (not wiped by restore)
            _db.session.expire_all()
            settings_after = get_settings()
            assert settings_after.org_name == "Changed after backup"

    def test_restore_handles_json_columns(self, app, tmp_path):
        """Rows with dict/list JSON columns (e.g. reminder_sent_json) must restore without error."""
        from datetime import timedelta

        from app.backup import export_to_zip, restore_from_zip
        from app.models.event import Event
        from app.models.master_event import MasterEvent

        with app.app_context():
            me = MasterEvent(name="JSON Test ME")
            _db.session.add(me)
            _db.session.flush()
            now = datetime.now(timezone.utc)
            event = Event(
                name="JSON Test Event",
                master_event_id=me.id,
                start_datetime=now,
                end_datetime=now + timedelta(hours=2),
                reminder_sent_json={"24": now.isoformat()},
            )
            _db.session.add(event)
            _db.session.commit()
            event_id = event.id

            zip_path = export_to_zip(tmp_path)
            restore_from_zip(zip_path)

            _db.session.expire_all()
            restored = _db.session.get(Event, event_id)
            assert restored is not None
            assert isinstance(restored.reminder_sent_json, dict)
            assert "24" in restored.reminder_sent_json


class TestPruneOldBackups:
    def test_prune_keeps_n_files(self, app, tmp_path):
        with app.app_context():
            from app.backup import export_to_zip, prune_old_backups
            # Create 5 backup files
            for i in range(5):
                export_to_zip(tmp_path)
            files_before = list(tmp_path.glob("medcover_backup_*.zip"))
            assert len(files_before) == 5

            deleted = prune_old_backups(tmp_path, keep_count=3)
            files_after = list(tmp_path.glob("medcover_backup_*.zip"))
            assert len(files_after) == 3
            assert len(deleted) == 2

    def test_prune_does_nothing_when_within_limit(self, app, tmp_path):
        with app.app_context():
            from app.backup import export_to_zip, prune_old_backups
            export_to_zip(tmp_path)
            deleted = prune_old_backups(tmp_path, keep_count=7)
            assert deleted == []

    def test_prune_nonexistent_dir_is_safe(self, app, tmp_path):
        with app.app_context():
            from app.backup import prune_old_backups
            deleted = prune_old_backups(tmp_path / "missing", keep_count=3)
            assert deleted == []


class TestListBackups:
    def test_list_returns_newest_first(self, app, tmp_path):
        import time
        with app.app_context():
            from app.backup import export_to_zip, list_backups
            p1 = export_to_zip(tmp_path)
            time.sleep(0.05)
            p2 = export_to_zip(tmp_path)
            listing = list_backups(tmp_path)
            assert listing[0]["name"] == p2.name
            assert listing[1]["name"] == p1.name

    def test_list_includes_size_and_date(self, app, tmp_path):
        with app.app_context():
            from app.backup import export_to_zip, list_backups
            export_to_zip(tmp_path)
            listing = list_backups(tmp_path)
            assert listing[0]["size_bytes"] > 0
            assert isinstance(listing[0]["created_at"], datetime)


# ── Scheduled backup task tests ───────────────────────────────────────────────

class TestRunScheduledBackup:
    def test_returns_false_when_disabled(self, app, tmp_path):
        with app.app_context():
            settings = get_settings()
            settings.backup_schedule_enabled = False
            _db.session.commit()
            from app.scheduler_tasks import run_scheduled_backup
            result = run_scheduled_backup(_db.session)
            assert result is False

    def test_returns_false_wrong_hour(self, app, tmp_path):
        with app.app_context():
            settings = get_settings()
            settings.backup_schedule_enabled = True
            settings.backup_schedule_hour = 3
            settings.backup_dir = str(tmp_path)
            _db.session.commit()
            from app.scheduler_tasks import run_scheduled_backup
            # Pass a time that is NOT hour 3
            fake_now = datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
            result = run_scheduled_backup(_db.session, now=fake_now)
            assert result is False

    def test_creates_backup_at_correct_hour(self, app, tmp_path):
        with app.app_context():
            settings = get_settings()
            settings.backup_schedule_enabled = True
            settings.backup_schedule_hour = 2
            settings.backup_dir = str(tmp_path)
            settings.backup_keep_count = 7
            _db.session.commit()
            from app.scheduler_tasks import run_scheduled_backup
            fake_now = datetime(2026, 1, 1, 2, 0, 0, tzinfo=timezone.utc)
            result = run_scheduled_backup(_db.session, now=fake_now)
            assert result is True
            assert len(list(tmp_path.glob("medcover_backup_*.zip"))) == 1

    def test_skips_if_already_backed_up_today(self, app, tmp_path):
        with app.app_context():
            settings = get_settings()
            settings.backup_schedule_enabled = True
            settings.backup_schedule_hour = 2
            settings.backup_dir = str(tmp_path)
            settings.backup_keep_count = 7
            _db.session.commit()
            from app.scheduler_tasks import run_scheduled_backup
            fake_now = datetime(2026, 1, 1, 2, 0, 0, tzinfo=timezone.utc)
            # First run should succeed
            assert run_scheduled_backup(_db.session, now=fake_now) is True
            # Second run same hour same day should be skipped
            assert run_scheduled_backup(_db.session, now=fake_now) is False
            assert len(list(tmp_path.glob("medcover_backup_*.zip"))) == 1


# ── Route tests ───────────────────────────────────────────────────────────────

class TestBackupRoutes:
    def test_index_requires_login(self, client):
        resp = client.get("/admin/backup/")
        assert resp.status_code in (302, 401)

    def test_index_accessible_to_admin(self, app, client):
        with app.app_context():
            _make_user("admin@test.com", "Admin", Role.ADMIN)
        _login(client, "admin@test.com")
        resp = client.get("/admin/backup/")
        assert resp.status_code == 200
        assert "Zálohy".encode() in resp.data or "záloh".encode() in resp.data

    def test_run_backup_creates_file(self, app, client, tmp_path):
        with app.app_context():
            _make_user("admin@test.com", "Admin", Role.ADMIN)
            settings = get_settings()
            settings.backup_dir = str(tmp_path)
            _db.session.commit()
        _login(client, "admin@test.com")
        csrf = _get_csrf(client, "/admin/backup/")
        resp = client.post("/admin/backup/run", data={"csrf_token": csrf},
                           follow_redirects=True)
        assert resp.status_code == 200
        assert len(list(tmp_path.glob("medcover_backup_*.zip"))) == 1

    def test_download_serves_zip(self, app, client, tmp_path):
        with app.app_context():
            _make_user("admin@test.com", "Admin", Role.ADMIN)
            settings = get_settings()
            settings.backup_dir = str(tmp_path)
            _db.session.commit()
        _login(client, "admin@test.com")
        csrf = _get_csrf(client, "/admin/backup/")
        client.post("/admin/backup/run", data={"csrf_token": csrf})
        files = list(tmp_path.glob("medcover_backup_*.zip"))
        assert files
        resp = client.get(f"/admin/backup/download/{files[0].name}")
        assert resp.status_code == 200
        assert resp.content_type == "application/zip"

    def test_download_rejects_path_traversal(self, app, client):
        with app.app_context():
            _make_user("admin@test.com", "Admin", Role.ADMIN)
        _login(client, "admin@test.com")
        resp = client.get("/admin/backup/download/../../etc/passwd")
        assert resp.status_code == 404

    def test_restore_requires_confirmation_word(self, app, client, tmp_path):
        with app.app_context():
            _make_user("admin@test.com", "Admin", Role.ADMIN)
            settings = get_settings()
            settings.backup_dir = str(tmp_path)
            _db.session.commit()
        _login(client, "admin@test.com")
        csrf = _get_csrf(client, "/admin/backup/")
        # Create a backup first
        client.post("/admin/backup/run", data={"csrf_token": csrf})
        files = list(tmp_path.glob("medcover_backup_*.zip"))
        # Wrong confirmation word
        resp = client.post(
            f"/admin/backup/restore/{files[0].name}",
            data={"csrf_token": csrf, "confirmation": "WRONG"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"RESTORE" in resp.data

    def test_restore_succeeds_with_correct_confirmation(self, app, client, tmp_path):
        with app.app_context():
            _make_user("admin@test.com", "Admin", Role.ADMIN)
            settings = get_settings()
            settings.backup_dir = str(tmp_path)
            settings.backup_keep_count = 7
            _db.session.commit()
        _login(client, "admin@test.com")
        csrf = _get_csrf(client, "/admin/backup/")
        client.post("/admin/backup/run", data={"csrf_token": csrf})
        files = list(tmp_path.glob("medcover_backup_*.zip"))
        resp = client.post(
            f"/admin/backup/restore/{files[0].name}",
            data={"csrf_token": csrf, "confirmation": "RESTORE"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        # Should show success flash, not error
        assert b"selhala" not in resp.data

    def test_member_cannot_access_backup(self, app, client):
        with app.app_context():
            _make_user("member@test.com", "Member", Role.MEMBER)
        _login(client, "member@test.com")
        resp = client.get("/admin/backup/")
        assert resp.status_code == 403

    def test_delete_requires_confirmation_word(self, app, client, tmp_path):
        with app.app_context():
            _make_user("admin@test.com", "Admin", Role.ADMIN)
            settings = get_settings()
            settings.backup_dir = str(tmp_path)
            _db.session.commit()
        _login(client, "admin@test.com")
        csrf = _get_csrf(client, "/admin/backup/")
        client.post("/admin/backup/run", data={"csrf_token": csrf})
        files = list(tmp_path.glob("medcover_backup_*.zip"))
        resp = client.post(
            f"/admin/backup/delete/{files[0].name}",
            data={"csrf_token": csrf, "confirmation": "wrong"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert files[0].exists(), "File should NOT be deleted on wrong confirmation"

    def test_delete_removes_file_with_correct_confirmation(self, app, client, tmp_path):
        with app.app_context():
            _make_user("admin@test.com", "Admin", Role.ADMIN)
            settings = get_settings()
            settings.backup_dir = str(tmp_path)
            _db.session.commit()
        _login(client, "admin@test.com")
        csrf = _get_csrf(client, "/admin/backup/")
        client.post("/admin/backup/run", data={"csrf_token": csrf})
        files = list(tmp_path.glob("medcover_backup_*.zip"))
        resp = client.post(
            f"/admin/backup/delete/{files[0].name}",
            data={"csrf_token": csrf, "confirmation": "SMAZAT"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert not files[0].exists(), "File should be deleted on correct confirmation"

    def test_delete_rejects_path_traversal(self, app, client):
        with app.app_context():
            _make_user("admin@test.com", "Admin", Role.ADMIN)
        _login(client, "admin@test.com")
        csrf = _get_csrf(client, "/admin/backup/")
        resp = client.post(
            "/admin/backup/delete/../etc/passwd",
            data={"csrf_token": csrf, "confirmation": "SMAZAT"},
        )
        assert resp.status_code == 404
