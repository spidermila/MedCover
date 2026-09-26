"""Tests for backup/restore engine and backup management routes."""

import json
import zipfile
from datetime import date, datetime, timedelta, timezone
from io import BytesIO
from zoneinfo import ZoneInfo

import pytest
from azure.core.exceptions import HttpResponseError

from app.backup import _container, downloaded_backup, export_to_zip, list_backups, prune_old_backups, restore_from_zip
from app.extensions import db as _db
from app.models.audit import AuditLogEntry
from app.models.event import Event
from app.models.master_event import MasterEvent
from app.models.role import Role
from app.models.settings import get_settings
from app.models.user import UserAccount
from app.scheduler_tasks import run_scheduled_backup
from tests.conftest import _get_csrf, _login, _make_user


def _blob_names() -> list[str]:
    """Names of all backup blobs in this test's container, oldest first."""
    return sorted(b["name"] for b in list_backups())


def _blob_bytes(name: str) -> bytes:
    return _container().download_blob(name).readall()


def _restore(name: str) -> None:
    with downloaded_backup(name) as path:
        restore_from_zip(path)


# ── Core engine tests ─────────────────────────────────────────────────────────


class TestExportToZip:
    def test_creates_zip_file(self, app):
        with app.app_context():

            name = export_to_zip()
        assert _blob_names() == [name]
        assert name.startswith("medcover_backup_")
        assert name.endswith(".zip")

    def test_zip_contains_backup_json(self, app):
        with app.app_context():

            name = export_to_zip()
        with zipfile.ZipFile(BytesIO(_blob_bytes(name))) as zf:
            assert "backup.json" in zf.namelist()

    def test_backup_json_structure(self, app):
        with app.app_context():

            name = export_to_zip()
        with zipfile.ZipFile(BytesIO(_blob_bytes(name))) as zf:
            payload = json.loads(zf.read("backup.json"))
        assert payload["version"] == "1.0"
        assert "schema_version" in payload
        assert "exported_at" in payload
        assert "tables" in payload

    def test_app_settings_excluded(self, app):
        with app.app_context():

            name = export_to_zip()
        with zipfile.ZipFile(BytesIO(_blob_bytes(name))) as zf:
            payload = json.loads(zf.read("backup.json"))
        assert "app_settings" not in payload["tables"]
        assert "alembic_version" not in payload["tables"]

    def test_user_table_included(self, app):
        with app.app_context():
            _make_user("backup_test@example.com", "Backup User", Role.MEMBER)

            name = export_to_zip()
        with zipfile.ZipFile(BytesIO(_blob_bytes(name))) as zf:
            payload = json.loads(zf.read("backup.json"))
        assert "user_account" in payload["tables"]
        emails = [row["email"] for row in payload["tables"]["user_account"]]
        assert "backup_test@example.com" in emails


class TestRestoreFromZip:
    def test_restore_reloads_user(self, app):
        with app.app_context():
            _make_user("restore_target@example.com", "Restore Target", Role.MEMBER)

            name = export_to_zip()

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

            _restore(name)
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

    def test_restore_preserves_app_settings(self, app):
        """AppSettings must survive a restore (it is excluded from backup)."""
        with app.app_context():
            settings = get_settings()
            settings.org_name = "Pre-restore org"
            _db.session.commit()

            name = export_to_zip()
            settings.org_name = "Changed after backup"
            _db.session.commit()

            _restore(name)

            # AppSettings should retain "Changed after backup" (not wiped by restore)
            _db.session.expire_all()
            settings_after = get_settings()
            assert settings_after.org_name == "Changed after backup"

    def test_restore_handles_json_columns(self, app):
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

            name = export_to_zip()
            _restore(name)

            _db.session.expire_all()
            restored = _db.session.get(Event, event_id)
            assert restored is not None
            assert isinstance(restored.reminder_sent_json, dict)
            assert "24" in restored.reminder_sent_json

    def test_restore_roundtrips_binary_column(self, app):
        """LargeBinary columns (e.g. signature_image) are hex-encoded on export by
        _serialize_value; restore must decode them back to bytes, not leave them as
        hex strings (which pyodbc would reject as VARBINARY params)."""

        with app.app_context():
            user = _make_user("binary_roundtrip@example.com", "Binary Roundtrip", Role.MEMBER)
            user.signature_image = b"\x89PNG\r\n\x1a\n\x00\x01\xff\xfe"
            user.signature_mimetype = "image/png"
            _db.session.commit()
            user_id = user.id

            name = export_to_zip()

            user.signature_image = None
            user.signature_mimetype = None
            _db.session.commit()

            _restore(name)

            _db.session.expire_all()
            restored = _db.session.get(UserAccount, user_id)
            assert restored is not None
            assert restored.signature_image == b"\x89PNG\r\n\x1a\n\x00\x01\xff\xfe"
            assert restored.signature_mimetype == "image/png"


class TestPruneOldBackups:
    def test_prune_keeps_n_files(self, app):
        with app.app_context():

            # Create 5 backups, one per day, oldest first
            names = [export_to_zip(now=datetime(2026, 1, day, tzinfo=timezone.utc)) for day in range(1, 6)]
            assert len(_blob_names()) == 5

            deleted = prune_old_backups(keep_count=3)
            assert deleted == names[:2]
            assert _blob_names() == names[2:]

    def test_prune_does_nothing_when_within_limit(self, app):
        with app.app_context():

            export_to_zip()
            deleted = prune_old_backups(keep_count=7)
            assert deleted == []

    def test_prune_tolerates_concurrent_delete(self, app):
        """Another worker or the scheduler may prune the same blob first."""
        with app.app_context():
            names = [export_to_zip(now=datetime(2026, 1, day, tzinfo=timezone.utc)) for day in range(1, 4)]
            real_list = type(_container()).list_blobs

            def list_then_race(self, *args, **kwargs):
                blobs = list(real_list(self, *args, **kwargs))
                self.delete_blob(names[0])  # the concurrent prune wins
                return blobs

            with pytest.MonkeyPatch.context() as mp:
                mp.setattr(type(_container()), "list_blobs", list_then_race)
                deleted = prune_old_backups(keep_count=1)
            assert deleted == [names[1]]
            assert _blob_names() == [names[2]]

    def test_foreign_blob_neither_listed_nor_pruned(self, app):
        """A hand-uploaded blob sharing the prefix must not occupy a keep slot."""
        with app.app_context():
            _container().upload_blob("medcover_backup_manual.zip", b"x")
            names = [export_to_zip(now=datetime(2026, 1, day, tzinfo=timezone.utc)) for day in range(1, 4)]
            assert [b["name"] for b in list_backups()] == names[::-1]
            assert prune_old_backups(keep_count=2) == names[:1]
            remaining = sorted(b.name for b in _container().list_blobs())
            assert remaining == sorted(names[1:] + ["medcover_backup_manual.zip"])


class TestContainerClient:
    def test_container_url_uses_managed_identity_without_creating_container(self, monkeypatch):
        """Production path: Entra ID credential, no key, and no create_container
        (the identity has no rights to create containers). No network calls."""
        from azure.identity import DefaultAzureCredential  # pylint: disable=import-outside-toplevel
        from azure.storage.blob import ContainerClient  # pylint: disable=import-outside-toplevel

        def no_create(*args, **kwargs):
            raise AssertionError("must not create the container in managed-identity mode")

        monkeypatch.delenv("BACKUP_STORAGE_CONNECTION_STRING")
        monkeypatch.setenv("BACKUP_CONTAINER_URL", "https://acct.blob.core.windows.net/backups")
        monkeypatch.setenv("AZURE_CLIENT_ID", "00000000-0000-0000-0000-000000000000")
        monkeypatch.setattr(ContainerClient, "create_container", no_create)
        _container.cache_clear()
        try:
            client = _container()
            assert client.url == "https://acct.blob.core.windows.net/backups"
            assert client.container_name == "backups"
            assert isinstance(client.credential, DefaultAzureCredential)
        finally:
            # Don't let the autouse fixture's teardown talk to the fake account.
            _container.cache_clear()


class TestListBackups:
    def test_list_returns_newest_first(self, app):

        with app.app_context():

            n1 = export_to_zip(now=datetime(2026, 1, 1, tzinfo=timezone.utc))
            n2 = export_to_zip(now=datetime(2026, 1, 2, tzinfo=timezone.utc))
            listing = list_backups()
            assert [b["name"] for b in listing] == [n2, n1]

    def test_list_includes_size_and_date(self, app):
        with app.app_context():

            export_to_zip()
            listing = list_backups()
            assert listing[0]["size_bytes"] > 0
            assert listing[0]["created_at"].tzinfo is not None


# ── Scheduled backup task tests ───────────────────────────────────────────────


class TestRunScheduledBackup:
    def test_returns_false_when_disabled(self, app):
        with app.app_context():
            settings = get_settings()
            settings.backup_schedule_enabled = False
            _db.session.commit()

            result = run_scheduled_backup(_db.session)
            assert result is False

    def test_returns_false_before_scheduled_time(self, app):
        with app.app_context():
            settings = get_settings()
            settings.backup_schedule_enabled = True
            settings.backup_schedule_hour = 3
            settings.backup_schedule_minute = 30
            _db.session.commit()

            # January: Europe/Prague = UTC+1, so 02:00 UTC = 03:00 local (before 03:30).
            fake_now = datetime(2026, 1, 1, 2, 0, 0, tzinfo=timezone.utc)
            result = run_scheduled_backup(_db.session, now=fake_now)
            assert result is False

    def test_creates_backup_at_exact_scheduled_minute(self, app):
        with app.app_context():
            settings = get_settings()
            settings.backup_schedule_enabled = True
            settings.backup_schedule_hour = 2
            settings.backup_schedule_minute = 30
            settings.backup_keep_count = 7
            _db.session.commit()

            # January: Europe/Prague = UTC+1, so 01:30 UTC = 02:30 local.
            fake_now = datetime(2026, 1, 1, 1, 30, 0, tzinfo=timezone.utc)
            result = run_scheduled_backup(_db.session, now=fake_now)
            assert result is True
            assert len(_blob_names()) == 1

    def test_creates_backup_on_late_tick_after_missed_window(self, app):
        """If the scheduler is delayed past the scheduled minute, the next tick
        should still fire the backup (tolerant window), rather than skip the day."""
        with app.app_context():
            settings = get_settings()
            settings.backup_schedule_enabled = True
            settings.backup_schedule_hour = 2
            settings.backup_schedule_minute = 30
            settings.backup_keep_count = 7
            _db.session.commit()

            # Local 04:15 — well past scheduled 02:30, no backup yet today.
            fake_now = datetime(2026, 1, 1, 3, 15, 0, tzinfo=timezone.utc)
            assert run_scheduled_backup(_db.session, now=fake_now) is True
            assert len(_blob_names()) == 1

    def test_second_tick_same_day_is_deduped(self, app):
        with app.app_context():
            settings = get_settings()
            settings.backup_schedule_enabled = True
            settings.backup_schedule_hour = 2
            settings.backup_schedule_minute = 0
            settings.backup_keep_count = 7
            _db.session.commit()

            # January: Europe/Prague = UTC+1, so 01:00 UTC = 02:00 local
            fake_now = datetime(2026, 1, 1, 1, 0, 0, tzinfo=timezone.utc)
            assert run_scheduled_backup(_db.session, now=fake_now) is True
            assert run_scheduled_backup(_db.session, now=fake_now) is False
            assert len(_blob_names()) == 1

    def test_dedupe_uses_local_date_not_utc(self, app):
        """A scheduled run late on local day N (early UTC day N+1) must count
        as today's *scheduled* run for the next tick on local day N."""
        with app.app_context():
            settings = get_settings()
            settings.backup_schedule_enabled = True
            settings.backup_schedule_hour = 23
            settings.backup_schedule_minute = 45
            settings.backup_keep_count = 7
            _db.session.commit()

            # January: Europe/Prague = UTC+1. Local 2026-01-01 23:45 = UTC 22:45.
            first_tick = datetime(2026, 1, 1, 22, 45, 0, tzinfo=timezone.utc)
            assert run_scheduled_backup(_db.session, now=first_tick) is True
            # Ten minutes later: local 23:55 (same local day), UTC 22:55.
            second_tick = datetime(2026, 1, 1, 22, 55, 0, tzinfo=timezone.utc)
            assert run_scheduled_backup(_db.session, now=second_tick) is False
            assert len(_blob_names()) == 1

    def test_ad_hoc_backup_does_not_suppress_scheduled_run(self, app):
        """An ad-hoc backup in the same directory must not block the scheduled
        run. The dedupe key is the DB-stored last-scheduled-run date, not the
        presence of any file on disk."""
        with app.app_context():
            settings = get_settings()
            settings.backup_schedule_enabled = True
            settings.backup_schedule_hour = 13
            settings.backup_schedule_minute = 30
            settings.backup_keep_count = 7
            _db.session.commit()

            # Simulate an ad-hoc backup an admin ran earlier today, before the
            # scheduled time. Local 2026-01-01 10:00 = UTC 09:00.
            ad_hoc_time = datetime(2026, 1, 1, 9, 0, 0, tzinfo=timezone.utc)
            export_to_zip(now=ad_hoc_time)
            assert len(_blob_names()) == 1

            # Scheduled tick at local 13:30 = UTC 12:30. Must still fire.
            fake_now = datetime(2026, 1, 1, 12, 30, 0, tzinfo=timezone.utc)
            assert run_scheduled_backup(_db.session, now=fake_now) is True
            assert len(_blob_names()) == 2

            # And a second scheduled tick the same day is still deduped.
            fake_now_later = datetime(2026, 1, 1, 12, 31, 0, tzinfo=timezone.utc)
            assert run_scheduled_backup(_db.session, now=fake_now_later) is False
            assert len(_blob_names()) == 2

    def test_scheduled_run_stamps_last_run_date(self, app):
        with app.app_context():
            settings = get_settings()
            settings.backup_schedule_enabled = True
            settings.backup_schedule_hour = 2
            settings.backup_schedule_minute = 0
            settings.backup_last_scheduled_run_date = None
            _db.session.commit()

            fake_now = datetime(2026, 1, 1, 1, 0, 0, tzinfo=timezone.utc)
            assert run_scheduled_backup(_db.session, now=fake_now) is True
            _db.session.expire_all()
            settings = get_settings()
            # 2026-01-01 01:00 UTC = 2026-01-01 02:00 Europe/Prague
            assert settings.backup_last_scheduled_run_date == date(2026, 1, 1)


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

    def test_run_backup_creates_file(self, app, client):
        with app.app_context():
            _make_user("admin@test.com", "Admin", Role.ADMIN)
        _login(client, "admin@test.com")
        csrf = _get_csrf(client, "/admin/backup/")
        resp = client.post("/admin/backup/run", data={"csrf_token": csrf}, follow_redirects=True)
        assert resp.status_code == 200
        assert len(_blob_names()) == 1

    def test_download_serves_zip(self, app, client):
        with app.app_context():
            _make_user("admin@test.com", "Admin", Role.ADMIN)
        _login(client, "admin@test.com")
        csrf = _get_csrf(client, "/admin/backup/")
        client.post("/admin/backup/run", data={"csrf_token": csrf})
        files = _blob_names()
        assert files
        resp = client.get(f"/admin/backup/download/{files[0]}")
        assert resp.status_code == 200
        assert resp.content_type == "application/zip"

    def test_download_streams_blob_content(self, app, client):
        with app.app_context():
            _make_user("admin@test.com", "Admin", Role.ADMIN)
            name = export_to_zip()
        _login(client, "admin@test.com")
        resp = client.get(f"/admin/backup/download/{name}")
        assert resp.status_code == 200
        assert resp.data == _blob_bytes(name)
        assert name in resp.headers["Content-Disposition"]

    def test_download_missing_blob_returns_404(self, app, client):
        with app.app_context():
            _make_user("admin@test.com", "Admin", Role.ADMIN)
        _login(client, "admin@test.com")
        resp = client.get("/admin/backup/download/medcover_backup_20260101_000000_000000_UTC.zip")
        assert resp.status_code == 404

    def test_restore_missing_blob_returns_404(self, app, client):
        with app.app_context():
            _make_user("admin@test.com", "Admin", Role.ADMIN)
        _login(client, "admin@test.com")
        csrf = _get_csrf(client, "/admin/backup/")
        resp = client.post(
            "/admin/backup/restore/medcover_backup_20260101_000000_000000_UTC.zip",
            data={"csrf_token": csrf, "confirmation": "RESTORE"},
        )
        assert resp.status_code == 404

    @pytest.mark.parametrize(
        "method,url,confirmation",
        [
            ("get", "/admin/backup/download/other.zip", ""),
            ("get", "/admin/backup/download/medcover_backup_20260101_000000_1.zip%0A", ""),
            ("post", "/admin/backup/restore/other.zip", "RESTORE"),
            ("post", "/admin/backup/delete/other.zip", "SMAZAT"),
        ],
    )
    def test_invalid_name_rejected_before_storage_call(self, app, client, monkeypatch, method, url, confirmation):
        with app.app_context():
            _make_user("admin@test.com", "Admin", Role.ADMIN)
        _login(client, "admin@test.com")
        csrf = _get_csrf(client, "/admin/backup/")

        def no_storage():
            raise AssertionError("storage must not be touched for an invalid name")

        monkeypatch.setattr("app.backup._container", no_storage)
        resp = getattr(client, method)(url, data={"csrf_token": csrf, "confirmation": confirmation})
        assert resp.status_code == 404

    def test_delete_missing_blob_returns_404(self, app, client):
        with app.app_context():
            _make_user("admin@test.com", "Admin", Role.ADMIN)
        _login(client, "admin@test.com")
        csrf = _get_csrf(client, "/admin/backup/")
        resp = client.post(
            "/admin/backup/delete/medcover_backup_20260101_000000_000000_UTC.zip",
            data={"csrf_token": csrf, "confirmation": "SMAZAT"},
        )
        assert resp.status_code == 404

    def test_download_rejects_path_traversal(self, app, client):
        with app.app_context():
            _make_user("admin@test.com", "Admin", Role.ADMIN)
        _login(client, "admin@test.com")
        resp = client.get("/admin/backup/download/../../etc/passwd")
        assert resp.status_code == 404

    def test_restore_requires_confirmation_word(self, app, client):
        with app.app_context():
            _make_user("admin@test.com", "Admin", Role.ADMIN)
        _login(client, "admin@test.com")
        csrf = _get_csrf(client, "/admin/backup/")
        # Create a backup first
        client.post("/admin/backup/run", data={"csrf_token": csrf})
        files = _blob_names()
        # Wrong confirmation word
        resp = client.post(
            f"/admin/backup/restore/{files[0]}",
            data={"csrf_token": csrf, "confirmation": "WRONG"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert "Obnovení selhalo: pro potvrzení zadejte RESTORE.".encode() in resp.data

    def test_restore_succeeds_with_correct_confirmation(self, app, client):
        with app.app_context():
            _make_user("admin@test.com", "Admin", Role.ADMIN)
            settings = get_settings()
            settings.backup_keep_count = 7
            _db.session.commit()
        _login(client, "admin@test.com")
        csrf = _get_csrf(client, "/admin/backup/")
        client.post("/admin/backup/run", data={"csrf_token": csrf})
        files = _blob_names()
        resp = client.post(
            f"/admin/backup/restore/{files[0]}",
            data={"csrf_token": csrf, "confirmation": "RESTORE"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        # Should show success flash, not error
        assert b"selhala" not in resp.data

    def test_index_survives_storage_outage(self, app, client, monkeypatch):
        with app.app_context():
            _make_user("admin@test.com", "Admin", Role.ADMIN)
        _login(client, "admin@test.com")

        def outage():
            raise HttpResponseError("simulated storage outage")

        monkeypatch.setattr("app.routes.backup.list_backups", outage)
        resp = client.get("/admin/backup/")
        assert resp.status_code == 200
        assert "Nepodařilo se načíst seznam záloh".encode() in resp.data

    def test_run_backup_prune_failure_still_reports_success(self, app, client, monkeypatch):
        with app.app_context():
            _make_user("admin@test.com", "Admin", Role.ADMIN)
        _login(client, "admin@test.com")
        csrf = _get_csrf(client, "/admin/backup/")

        def fail_prune(*args, **kwargs):
            raise HttpResponseError("simulated storage outage")

        monkeypatch.setattr("app.routes.backup.prune_old_backups", fail_prune)
        resp = client.post("/admin/backup/run", data={"csrf_token": csrf}, follow_redirects=True)
        assert "Záloha byla vytvořena".encode() in resp.data
        assert len(_blob_names()) == 1
        with app.app_context():
            assert _db.session.query(AuditLogEntry).filter_by(entity_type="Backup", action_type="create").count() == 1

    def test_download_storage_outage_redirects_with_error(self, app, client, monkeypatch):
        with app.app_context():
            _make_user("admin@test.com", "Admin", Role.ADMIN)
        _login(client, "admin@test.com")

        def outage(name):
            raise HttpResponseError("simulated storage outage")

        monkeypatch.setattr("app.routes.backup.open_backup", outage)
        resp = client.get("/admin/backup/download/medcover_backup_20260101_000000_000000_UTC.zip")
        assert resp.status_code == 302

    def test_restore_storage_outage_redirects_with_error(self, app, client, monkeypatch):
        with app.app_context():
            _make_user("admin@test.com", "Admin", Role.ADMIN)
        _login(client, "admin@test.com")
        csrf = _get_csrf(client, "/admin/backup/")

        def outage(name):
            raise HttpResponseError("simulated storage outage")

        monkeypatch.setattr("app.routes.backup.downloaded_backup", outage)
        resp = client.post(
            "/admin/backup/restore/medcover_backup_20260101_000000_000000_UTC.zip",
            data={"csrf_token": csrf, "confirmation": "RESTORE"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert "Obnovení selhalo".encode() in resp.data

    def test_member_cannot_access_backup(self, app, client):
        with app.app_context():
            _make_user("member@test.com", "Member", Role.MEMBER)
        _login(client, "member@test.com")
        resp = client.get("/admin/backup/")
        assert resp.status_code == 403

    def test_delete_requires_confirmation_word(self, app, client):
        with app.app_context():
            _make_user("admin@test.com", "Admin", Role.ADMIN)
        _login(client, "admin@test.com")
        csrf = _get_csrf(client, "/admin/backup/")
        client.post("/admin/backup/run", data={"csrf_token": csrf})
        files = _blob_names()
        resp = client.post(
            f"/admin/backup/delete/{files[0]}",
            data={"csrf_token": csrf, "confirmation": "wrong"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert files[0] in _blob_names(), "File should NOT be deleted on wrong confirmation"

    def test_delete_removes_file_with_correct_confirmation(self, app, client):
        with app.app_context():
            _make_user("admin@test.com", "Admin", Role.ADMIN)
        _login(client, "admin@test.com")
        csrf = _get_csrf(client, "/admin/backup/")
        client.post("/admin/backup/run", data={"csrf_token": csrf})
        files = _blob_names()
        resp = client.post(
            f"/admin/backup/delete/{files[0]}",
            data={"csrf_token": csrf, "confirmation": "SMAZAT"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert files[0] not in _blob_names(), "File should be deleted on correct confirmation"

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

    def test_upload_restore_wrong_confirmation_rejected(self, app, client):
        with app.app_context():
            _make_user("admin@test.com", "Admin", Role.ADMIN)
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

    def test_upload_restore_no_file_rejected(self, app, client):
        with app.app_context():
            _make_user("admin@test.com", "Admin", Role.ADMIN)
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

    def test_upload_restore_non_zip_rejected(self, app, client):
        with app.app_context():
            _make_user("admin@test.com", "Admin", Role.ADMIN)
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

    def test_upload_restore_succeeds_and_restores_data(self, app, client):
        with app.app_context():
            _make_user("admin@test.com", "Admin", Role.ADMIN)
            _make_user("upload_target@example.com", "Upload Target", Role.MEMBER)
            settings = get_settings()
            settings.backup_keep_count = 7
            _db.session.commit()
        _login(client, "admin@test.com")
        csrf = _get_csrf(client, "/admin/backup/")

        # Create backup that includes upload_target
        client.post("/admin/backup/run", data={"csrf_token": csrf})
        files = _blob_names()
        assert files
        zip_bytes = _blob_bytes(files[0])

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

    def test_restore_route_actually_restores_deleted_data(self, app, client):
        """Restoring from a stored backup brings back data deleted after the backup."""
        with app.app_context():
            _make_user("admin@test.com", "Admin", Role.ADMIN)
            me = MasterEvent(name="Backup Round-trip ME")
            _db.session.add(me)
            _db.session.commit()
            me_id = me.id
            settings = get_settings()
            settings.backup_keep_count = 7
            _db.session.commit()
        _login(client, "admin@test.com")
        csrf = _get_csrf(client, "/admin/backup/")

        # Backup includes the ME
        client.post("/admin/backup/run", data={"csrf_token": csrf})
        files = _blob_names()
        assert files

        # Delete the ME after backup
        with app.app_context():
            me = _db.session.get(MasterEvent, me_id)
            _db.session.delete(me)
            _db.session.commit()
            assert _db.session.get(MasterEvent, me_id) is None

        # Restore — ME should come back
        resp = client.post(
            f"/admin/backup/restore/{files[0]}",
            data={"csrf_token": csrf, "confirmation": "RESTORE"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"selhala" not in resp.data
        with app.app_context():
            assert _db.session.get(MasterEvent, me_id) is not None


class TestExportToZipFilename:
    def test_filename_contains_utc_suffix(self, app):
        with app.app_context():
            name = export_to_zip()
        assert name.endswith("_UTC.zip")
        assert name.startswith("medcover_backup_")

    def test_filename_uses_utc_timestamp_regardless_of_input_tz(self, app):
        # 03:00 in a UTC+3 zone is 00:00 UTC — the filename must reflect UTC.
        local = datetime(2026, 6, 15, 3, 0, 0, tzinfo=ZoneInfo("Europe/Moscow"))
        with app.app_context():
            name = export_to_zip(now=local)
        assert "20260615_000000" in name


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
                "backup_keep_count": "5",
                "backup_schedule_time": "99:99",
            },
            follow_redirects=True,
        )
        with app.app_context():
            settings = get_settings()
            assert settings.backup_schedule_hour == 23
            assert settings.backup_schedule_minute == 59

    def test_seconds_in_time_field_ignored(self, app, client):
        """Some browsers submit HH:MM:SS; the seconds must not blank the minute."""
        with app.app_context():
            _make_user("admin@test.com", "Admin", Role.ADMIN)
        _login(client, "admin@test.com")
        csrf = _get_csrf(client, "/admin/backup/")
        client.post(
            "/admin/backup/settings",
            data={
                "csrf_token": csrf,
                "backup_keep_count": "5",
                "backup_schedule_time": "04:37:00",
            },
            follow_redirects=True,
        )
        with app.app_context():
            settings = get_settings()
            assert settings.backup_schedule_hour == 4
            assert settings.backup_schedule_minute == 37


class TestScheduledBackupFailureHandling:
    """A failing backup target must not turn the every-minute poll into a
    per-minute retry storm of audit rows and tracebacks."""

    def test_failed_attempt_is_not_retried_the_same_day(self, app, monkeypatch):
        with app.app_context():
            settings = get_settings()
            settings.backup_schedule_enabled = True
            settings.backup_schedule_hour = 2
            settings.backup_schedule_minute = 0
            settings.backup_last_scheduled_run_date = None
            _db.session.commit()

            calls = []

            def boom(*args, **kwargs):
                calls.append(1)
                raise OSError("no space left on device")

            monkeypatch.setattr("app.scheduler_tasks.export_to_zip", boom)

            # January: Europe/Prague = UTC+1, so 01:00 UTC = 02:00 local.
            assert run_scheduled_backup(_db.session, now=datetime(2026, 1, 1, 1, 0, tzinfo=timezone.utc)) is False
            # A minute later — must not attempt again.
            assert run_scheduled_backup(_db.session, now=datetime(2026, 1, 1, 1, 1, tzinfo=timezone.utc)) is False
            assert len(calls) == 1

            # Next local day it tries again.
            assert run_scheduled_backup(_db.session, now=datetime(2026, 1, 2, 1, 0, tzinfo=timezone.utc)) is False
            assert len(calls) == 2

            # Each failed attempt is recorded in the audit log.
            errors = _db.session.query(AuditLogEntry).filter_by(entity_type="Backup", action_type="error").all()
            assert len(errors) == 2
            assert all(e.summary for e in errors)

    def test_upload_failure_is_recorded_as_failed_backup(self, app, monkeypatch):
        """A storage error during upload goes through the normal failure path."""
        with app.app_context():
            settings = get_settings()
            settings.backup_schedule_enabled = True
            settings.backup_schedule_hour = 2
            settings.backup_schedule_minute = 0
            settings.backup_last_scheduled_run_date = None
            _db.session.commit()

            def fail_upload(*args, **kwargs):
                raise HttpResponseError("simulated storage outage")

            monkeypatch.setattr(type(_container()), "upload_blob", fail_upload)
            assert run_scheduled_backup(_db.session, now=datetime(2026, 1, 1, 1, 0, tzinfo=timezone.utc)) is False
            assert _blob_names() == []
            errors = _db.session.query(AuditLogEntry).filter_by(entity_type="Backup", action_type="error").all()
            assert len(errors) == 1

    def test_prune_failure_does_not_fail_backup(self, app, monkeypatch):
        with app.app_context():
            settings = get_settings()
            settings.backup_schedule_enabled = True
            settings.backup_schedule_hour = 2
            settings.backup_schedule_minute = 0
            settings.backup_last_scheduled_run_date = None
            _db.session.commit()

            def fail_prune(*args, **kwargs):
                raise HttpResponseError("simulated storage outage")

            monkeypatch.setattr("app.scheduler_tasks.prune_old_backups", fail_prune)
            assert run_scheduled_backup(_db.session, now=datetime(2026, 1, 1, 1, 0, tzinfo=timezone.utc)) is True
            assert len(_blob_names()) == 1
            _db.session.expire_all()
            assert get_settings().backup_last_scheduled_run_date == date(2026, 1, 1)
