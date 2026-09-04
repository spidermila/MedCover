"""Tests for backup/restore engine and backup management routes."""

import json
import time
import zipfile
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path as _Path
from zoneinfo import ZoneInfo

import pytest

from app.backup import export_to_zip, list_backups, prune_old_backups, restore_from_zip
from app.extensions import db as _db
from app.models.event import Event
from app.models.master_event import MasterEvent
from app.models.role import Role
from app.models.settings import get_settings
from app.models.user import UserAccount
from app.scheduler_tasks import run_scheduled_backup
from tests.conftest import _get_csrf, _login, _make_user

# ── Core engine tests ─────────────────────────────────────────────────────────


class TestExportToZip:
    def test_creates_zip_file(self, app, tmp_path):
        with app.app_context():

            path = export_to_zip(tmp_path)
        assert path.exists()
        assert path.suffix == ".zip"
        assert path.name.startswith("medcover_backup_")

    def test_zip_contains_backup_json(self, app, tmp_path):
        with app.app_context():

            path = export_to_zip(tmp_path)
        with zipfile.ZipFile(path) as zf:
            assert "backup.json" in zf.namelist()

    def test_backup_json_structure(self, app, tmp_path):
        with app.app_context():

            path = export_to_zip(tmp_path)
        with zipfile.ZipFile(path) as zf:
            payload = json.loads(zf.read("backup.json"))
        assert payload["version"] == "1.0"
        assert "schema_version" in payload
        assert "exported_at" in payload
        assert "tables" in payload

    def test_app_settings_excluded(self, app, tmp_path):
        with app.app_context():

            path = export_to_zip(tmp_path)
        with zipfile.ZipFile(path) as zf:
            payload = json.loads(zf.read("backup.json"))
        assert "app_settings" not in payload["tables"]
        assert "alembic_version" not in payload["tables"]

    def test_user_table_included(self, app, tmp_path):
        with app.app_context():
            _make_user("backup_test@example.com", "Backup User", Role.MEMBER)

            path = export_to_zip(tmp_path)
        with zipfile.ZipFile(path) as zf:
            payload = json.loads(zf.read("backup.json"))
        assert "user_account" in payload["tables"]
        emails = [row["email"] for row in payload["tables"]["user_account"]]
        assert "backup_test@example.com" in emails

    def test_creates_backup_dir_if_missing(self, app, tmp_path):
        new_dir = tmp_path / "nested" / "backups"
        with app.app_context():

            path = export_to_zip(new_dir)
        assert path.exists()


class TestRestoreFromZip:
    def test_restore_reloads_user(self, app, tmp_path):
        with app.app_context():
            _make_user("restore_target@example.com", "Restore Target", Role.MEMBER)

            zip_path = export_to_zip(tmp_path)

            # Delete the user and verify they're gone

            u = _db.session.scalars(
                _db.select(UserAccount).where(UserAccount.email == "restore_target@example.com")
            ).first()
            _db.session.delete(u)
            _db.session.commit()
            assert (
                _db.session.scalars(
                    _db.select(UserAccount).where(UserAccount.email == "restore_target@example.com")
                ).first()
                is None
            )

            # Restore and verify user is back

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

            with pytest.raises(ValueError, match="backup.json"):
                restore_from_zip(zip_path)

    def test_restore_preserves_app_settings(self, app, tmp_path):
        """AppSettings must survive a restore (it is excluded from backup)."""
        with app.app_context():
            settings = get_settings()
            settings.org_name = "Pre-restore org"
            _db.session.commit()

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

    def test_restore_roundtrips_binary_column(self, app, tmp_path):
        """LargeBinary columns (e.g. signature_image) are hex-encoded on export by
        _serialize_value; restore must decode them back to bytes, not leave them as
        hex strings (which pyodbc would reject as VARBINARY params)."""

        with app.app_context():
            user = _make_user("binary_roundtrip@example.com", "Binary Roundtrip", Role.MEMBER)
            user.signature_image = b"\x89PNG\r\n\x1a\n\x00\x01\xff\xfe"
            user.signature_mimetype = "image/png"
            _db.session.commit()
            user_id = user.id

            zip_path = export_to_zip(tmp_path)

            user.signature_image = None
            user.signature_mimetype = None
            _db.session.commit()

            restore_from_zip(zip_path)

            _db.session.expire_all()
            restored = _db.session.get(UserAccount, user_id)
            assert restored is not None
            assert restored.signature_image == b"\x89PNG\r\n\x1a\n\x00\x01\xff\xfe"
            assert restored.signature_mimetype == "image/png"


class TestPruneOldBackups:
    def test_prune_keeps_n_files(self, app, tmp_path):
        with app.app_context():

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

            export_to_zip(tmp_path)
            deleted = prune_old_backups(tmp_path, keep_count=7)
            assert deleted == []

    def test_prune_nonexistent_dir_is_safe(self, app, tmp_path):
        with app.app_context():

            deleted = prune_old_backups(tmp_path / "missing", keep_count=3)
            assert deleted == []


class TestListBackups:
    def test_list_returns_newest_first(self, app, tmp_path):

        with app.app_context():

            p1 = export_to_zip(tmp_path)
            time.sleep(0.05)
            p2 = export_to_zip(tmp_path)
            listing = list_backups(tmp_path)
            assert listing[0]["name"] == p2.name
            assert listing[1]["name"] == p1.name

    def test_list_includes_size_and_date(self, app, tmp_path):
        with app.app_context():

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

            result = run_scheduled_backup(_db.session)
            assert result is False

    def test_returns_false_before_scheduled_time(self, app, tmp_path):
        with app.app_context():
            settings = get_settings()
            settings.backup_schedule_enabled = True
            settings.backup_schedule_hour = 3
            settings.backup_schedule_minute = 30
            settings.backup_dir = str(tmp_path)
            _db.session.commit()

            # January: Europe/Prague = UTC+1, so 02:00 UTC = 03:00 local (before 03:30).
            fake_now = datetime(2026, 1, 1, 2, 0, 0, tzinfo=timezone.utc)
            result = run_scheduled_backup(_db.session, now=fake_now)
            assert result is False

    def test_creates_backup_at_exact_scheduled_minute(self, app, tmp_path):
        with app.app_context():
            settings = get_settings()
            settings.backup_schedule_enabled = True
            settings.backup_schedule_hour = 2
            settings.backup_schedule_minute = 30
            settings.backup_dir = str(tmp_path)
            settings.backup_keep_count = 7
            _db.session.commit()

            # January: Europe/Prague = UTC+1, so 01:30 UTC = 02:30 local.
            fake_now = datetime(2026, 1, 1, 1, 30, 0, tzinfo=timezone.utc)
            result = run_scheduled_backup(_db.session, now=fake_now)
            assert result is True
            assert len(list(tmp_path.glob("medcover_backup_*.zip"))) == 1

    def test_creates_backup_on_late_tick_after_missed_window(self, app, tmp_path):
        """If the scheduler is delayed past the scheduled minute, the next tick
        should still fire the backup (tolerant window), rather than skip the day."""
        with app.app_context():
            settings = get_settings()
            settings.backup_schedule_enabled = True
            settings.backup_schedule_hour = 2
            settings.backup_schedule_minute = 30
            settings.backup_dir = str(tmp_path)
            settings.backup_keep_count = 7
            _db.session.commit()

            # Local 04:15 — well past scheduled 02:30, no backup yet today.
            fake_now = datetime(2026, 1, 1, 3, 15, 0, tzinfo=timezone.utc)
            assert run_scheduled_backup(_db.session, now=fake_now) is True
            assert len(list(tmp_path.glob("medcover_backup_*.zip"))) == 1

    def test_skips_if_already_backed_up_today(self, app, tmp_path):
        with app.app_context():
            settings = get_settings()
            settings.backup_schedule_enabled = True
            settings.backup_schedule_hour = 2
            settings.backup_schedule_minute = 0
            settings.backup_dir = str(tmp_path)
            settings.backup_keep_count = 7
            _db.session.commit()

            # January: Europe/Prague = UTC+1, so 01:00 UTC = 02:00 local
            fake_now = datetime(2026, 1, 1, 1, 0, 0, tzinfo=timezone.utc)
            # First run should succeed
            assert run_scheduled_backup(_db.session, now=fake_now) is True
            # Second run same hour same day should be skipped
            assert run_scheduled_backup(_db.session, now=fake_now) is False
            assert len(list(tmp_path.glob("medcover_backup_*.zip"))) == 1

    def test_dedupe_uses_local_date_not_utc(self, app, tmp_path):
        """A backup taken late on local day N (early UTC day N+1) must still
        count as "today's" backup for the next tick on local day N."""
        with app.app_context():
            settings = get_settings()
            settings.backup_schedule_enabled = True
            settings.backup_schedule_hour = 23
            settings.backup_schedule_minute = 45
            settings.backup_dir = str(tmp_path)
            settings.backup_keep_count = 7
            _db.session.commit()

            # January: Europe/Prague = UTC+1. Local 2026-01-01 23:45 = UTC 22:45.
            first_tick = datetime(2026, 1, 1, 22, 45, 0, tzinfo=timezone.utc)
            assert run_scheduled_backup(_db.session, now=first_tick) is True
            # Ten minutes later: local 23:55 (same local day), UTC 22:55.
            second_tick = datetime(2026, 1, 1, 22, 55, 0, tzinfo=timezone.utc)
            assert run_scheduled_backup(_db.session, now=second_tick) is False
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
        resp = client.post("/admin/backup/run", data={"csrf_token": csrf}, follow_redirects=True)
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
        assert "Obnovení selhalo: pro potvrzení zadejte RESTORE.".encode() in resp.data

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

    # ── Upload-restore route ──────────────────────────────────────────────────

    def test_upload_restore_wrong_confirmation_rejected(self, app, client, tmp_path):
        with app.app_context():
            _make_user("admin@test.com", "Admin", Role.ADMIN)
            settings = get_settings()
            settings.backup_dir = str(tmp_path)
            _db.session.commit()
        _login(client, "admin@test.com")
        csrf = _get_csrf(client, "/admin/backup/")
        client.post("/admin/backup/run", data={"csrf_token": csrf})
        resp = client.post(
            "/admin/backup/upload-restore",
            data={"csrf_token": csrf, "confirmation": "WRONG"},
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert "Obnovení selhalo: pro potvrzení zadejte RESTORE.".encode() in resp.data

    def test_upload_restore_no_file_rejected(self, app, client, tmp_path):
        with app.app_context():
            _make_user("admin@test.com", "Admin", Role.ADMIN)
            settings = get_settings()
            settings.backup_dir = str(tmp_path)
            _db.session.commit()
        _login(client, "admin@test.com")
        csrf = _get_csrf(client, "/admin/backup/")
        resp = client.post(
            "/admin/backup/upload-restore",
            data={"csrf_token": csrf, "confirmation": "RESTORE"},
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert "Nebyl vybrán žádný soubor.".encode() in resp.data

    def test_upload_restore_non_zip_rejected(self, app, client, tmp_path):
        with app.app_context():
            _make_user("admin@test.com", "Admin", Role.ADMIN)
            settings = get_settings()
            settings.backup_dir = str(tmp_path)
            _db.session.commit()
        _login(client, "admin@test.com")
        csrf = _get_csrf(client, "/admin/backup/")
        resp = client.post(
            "/admin/backup/upload-restore",
            data={
                "csrf_token": csrf,
                "confirmation": "RESTORE",
                "backup_file": (BytesIO(b"not a zip"), "backup.txt"),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert "Soubor musí být ve formátu .zip.".encode() in resp.data

    def test_upload_restore_succeeds_and_restores_data(self, app, client, tmp_path):
        with app.app_context():
            _make_user("admin@test.com", "Admin", Role.ADMIN)
            _make_user("upload_target@example.com", "Upload Target", Role.MEMBER)
            settings = get_settings()
            settings.backup_dir = str(tmp_path)
            settings.backup_keep_count = 7
            _db.session.commit()
        _login(client, "admin@test.com")
        csrf = _get_csrf(client, "/admin/backup/")

        # Create backup that includes upload_target
        client.post("/admin/backup/run", data={"csrf_token": csrf})
        files = list(tmp_path.glob("medcover_backup_*.zip"))
        assert files
        zip_bytes = files[0].read_bytes()

        # Delete the user so we can verify restoration
        with app.app_context():
            u = _db.session.scalars(
                _db.select(UserAccount).where(UserAccount.email == "upload_target@example.com")
            ).first()
            _db.session.delete(u)
            _db.session.commit()

        # Upload-restore
        resp = client.post(
            "/admin/backup/upload-restore",
            data={
                "csrf_token": csrf,
                "confirmation": "RESTORE",
                "backup_file": (BytesIO(zip_bytes), "medcover_backup_upload.zip"),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"selhala" not in resp.data

        with app.app_context():
            restored = _db.session.scalars(
                _db.select(UserAccount).where(UserAccount.email == "upload_target@example.com")
            ).first()
            assert restored is not None
            assert restored.name == "Upload Target"

    def test_restore_route_actually_restores_deleted_data(self, app, client, tmp_path):
        """Restoring from a stored backup brings back data deleted after the backup."""
        with app.app_context():
            _make_user("admin@test.com", "Admin", Role.ADMIN)
            me = MasterEvent(name="Backup Round-trip ME")
            _db.session.add(me)
            _db.session.commit()
            me_id = me.id
            settings = get_settings()
            settings.backup_dir = str(tmp_path)
            settings.backup_keep_count = 7
            _db.session.commit()
        _login(client, "admin@test.com")
        csrf = _get_csrf(client, "/admin/backup/")

        # Backup includes the ME
        client.post("/admin/backup/run", data={"csrf_token": csrf})
        files = list(tmp_path.glob("medcover_backup_*.zip"))
        assert files

        # Delete the ME after backup
        with app.app_context():
            me = _db.session.get(MasterEvent, me_id)
            _db.session.delete(me)
            _db.session.commit()
            assert _db.session.get(MasterEvent, me_id) is None

        # Restore — ME should come back
        resp = client.post(
            f"/admin/backup/restore/{files[0].name}",
            data={"csrf_token": csrf, "confirmation": "RESTORE"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"selhala" not in resp.data
        with app.app_context():
            assert _db.session.get(MasterEvent, me_id) is not None


class TestExportToZipAtomicWrite:
    """The zip must appear in the target directory only after it is fully written.

    On a shared SMB mount (Azure Files in prod) or any other mount, a crash
    mid-write must never leave a truncated medcover_backup_*.zip that the
    web UI would then list and offer for download or restore.
    """

    def test_partial_file_not_visible_on_write_failure(self, app, tmp_path, monkeypatch):
        with app.app_context():
            real_write_bytes = _Path.write_bytes

            def failing_write_bytes(self, data):
                # Fail only for the .part sidecar; anything else falls through.
                if self.suffix == ".part":
                    real_write_bytes(self, data)
                    raise OSError("simulated mid-write failure")
                return real_write_bytes(self, data)

            monkeypatch.setattr(_Path, "write_bytes", failing_write_bytes)

            with pytest.raises(OSError, match="simulated"):
                export_to_zip(tmp_path)

        visible = list(tmp_path.glob("medcover_backup_*.zip"))
        assert visible == [], "no half-written zip should be listed"

    def test_no_part_file_left_after_successful_write(self, app, tmp_path):
        with app.app_context():
            export_to_zip(tmp_path)
        assert list(tmp_path.glob("*.part")) == []
        assert len(list(tmp_path.glob("medcover_backup_*.zip"))) == 1


class TestBackupSettingsRoute:
    """save_settings must enforce the absolute-path invariant on backup_dir."""

    def test_absolute_path_accepted(self, app, client):
        with app.app_context():
            _make_user("admin@test.com", "Admin", Role.ADMIN)
        _login(client, "admin@test.com")
        csrf = _get_csrf(client, "/admin/backup/")
        resp = client.post(
            "/admin/backup/settings",
            data={
                "csrf_token": csrf,
                "backup_dir": "/backups",
                "backup_keep_count": "5",
                "backup_schedule_hour": "2",
            },
            follow_redirects=True,
        )
        assert resp.status_code == 200
        with app.app_context():
            assert get_settings().backup_dir == "/backups"

    def test_relative_path_rejected_previous_value_kept(self, app, client):
        with app.app_context():
            _make_user("admin@test.com", "Admin", Role.ADMIN)
            settings = get_settings()
            settings.backup_dir = "/backups"
            _db.session.commit()
        _login(client, "admin@test.com")
        csrf = _get_csrf(client, "/admin/backup/")
        resp = client.post(
            "/admin/backup/settings",
            data={
                "csrf_token": csrf,
                "backup_dir": "relative/dir",
                "backup_keep_count": "5",
                "backup_schedule_hour": "2",
            },
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert "absolutn\u00ed cesta".encode() in resp.data
        with app.app_context():
            assert get_settings().backup_dir == "/backups"

    def test_empty_path_rejected_previous_value_kept(self, app, client):
        with app.app_context():
            _make_user("admin@test.com", "Admin", Role.ADMIN)
            settings = get_settings()
            settings.backup_dir = "/backups"
            _db.session.commit()
        _login(client, "admin@test.com")
        csrf = _get_csrf(client, "/admin/backup/")
        client.post(
            "/admin/backup/settings",
            data={
                "csrf_token": csrf,
                "backup_dir": "   ",
                "backup_keep_count": "5",
                "backup_schedule_hour": "2",
            },
            follow_redirects=True,
        )
        with app.app_context():
            assert get_settings().backup_dir == "/backups"


class TestExportToZipFilename:
    def test_filename_contains_utc_suffix(self, app, tmp_path):
        with app.app_context():
            path = export_to_zip(tmp_path)
        assert path.name.endswith("_UTC.zip")
        assert path.name.startswith("medcover_backup_")

    def test_filename_uses_utc_timestamp_regardless_of_input_tz(self, app, tmp_path):
        # 03:00 in a UTC+3 zone is 00:00 UTC — the filename must reflect UTC.
        local = datetime(2026, 6, 15, 3, 0, 0, tzinfo=ZoneInfo("Europe/Moscow"))
        with app.app_context():
            path = export_to_zip(tmp_path, now=local)
        assert "20260615_000000" in path.name


class TestBackupScheduleTimeFormField:
    def test_hhmm_field_parsed(self, app, client):
        with app.app_context():
            _make_user("admin@test.com", "Admin", Role.ADMIN)
        _login(client, "admin@test.com")
        csrf = _get_csrf(client, "/admin/backup/")
        client.post(
            "/admin/backup/settings",
            data={
                "csrf_token": csrf,
                "backup_dir": "/backups",
                "backup_keep_count": "5",
                "backup_schedule_time": "04:37",
            },
            follow_redirects=True,
        )
        with app.app_context():
            settings = get_settings()
            assert settings.backup_schedule_hour == 4
            assert settings.backup_schedule_minute == 37

    def test_hour_and_minute_fields_still_accepted(self, app, client):
        """API-style submission with separate hour/minute fields keeps working."""
        with app.app_context():
            _make_user("admin@test.com", "Admin", Role.ADMIN)
        _login(client, "admin@test.com")
        csrf = _get_csrf(client, "/admin/backup/")
        client.post(
            "/admin/backup/settings",
            data={
                "csrf_token": csrf,
                "backup_dir": "/backups",
                "backup_keep_count": "5",
                "backup_schedule_hour": "9",
                "backup_schedule_minute": "15",
            },
            follow_redirects=True,
        )
        with app.app_context():
            settings = get_settings()
            assert settings.backup_schedule_hour == 9
            assert settings.backup_schedule_minute == 15

    def test_invalid_hhmm_falls_back_to_defaults(self, app, client):
        with app.app_context():
            _make_user("admin@test.com", "Admin", Role.ADMIN)
        _login(client, "admin@test.com")
        csrf = _get_csrf(client, "/admin/backup/")
        client.post(
            "/admin/backup/settings",
            data={
                "csrf_token": csrf,
                "backup_dir": "/backups",
                "backup_keep_count": "5",
                "backup_schedule_time": "not:a:time",
            },
            follow_redirects=True,
        )
        with app.app_context():
            settings = get_settings()
            assert settings.backup_schedule_hour == 2
            assert settings.backup_schedule_minute == 0

    def test_out_of_range_values_clamped(self, app, client):
        with app.app_context():
            _make_user("admin@test.com", "Admin", Role.ADMIN)
        _login(client, "admin@test.com")
        csrf = _get_csrf(client, "/admin/backup/")
        client.post(
            "/admin/backup/settings",
            data={
                "csrf_token": csrf,
                "backup_dir": "/backups",
                "backup_keep_count": "5",
                "backup_schedule_time": "99:99",
            },
            follow_redirects=True,
        )
        with app.app_context():
            settings = get_settings()
            assert settings.backup_schedule_hour == 23
            assert settings.backup_schedule_minute == 59
