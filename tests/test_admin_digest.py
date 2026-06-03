"""Tests for the admin digest feature."""
from __future__ import annotations

from zoneinfo import ZoneInfo

import sqlalchemy as sa

from app.extensions import db
from app.models.digest import DigestBlock
from app.models.digest import DigestMetricSnapshot
from app.models.digest import DigestSchedule
from app.models.digest import get_digest_schedule
from app.models.settings import get_settings
from tests.conftest import _get_csrf


def _local_tz(app_ctx) -> ZoneInfo:
    """Return the ZoneInfo matching AppSettings.timezone."""
    return ZoneInfo(get_settings().timezone)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _seed_schedule(app) -> None:
    """Ensure the digest schedule + default blocks exist."""
    with app.app_context():
        get_digest_schedule()


def _first_block_id(app, block_type: str) -> int:
    """Return the id of the first DigestBlock of the given type."""
    with app.app_context():
        schedule = get_digest_schedule()
        block = db.session.scalar(
            sa.select(DigestBlock).where(
                DigestBlock.digest_schedule_id == schedule.id,
                DigestBlock.block_type == block_type,
            )
        )
        assert block is not None, f"No block of type {block_type!r} found"
        return block.id


# ── render_digest ─────────────────────────────────────────────────────────────

def test_render_digest_returns_html(app: object) -> None:
    """render_digest should return non-empty HTML with the outer frame."""
    from app.digest.renderer import render_digest

    with app.app_context():
        schedule = get_digest_schedule()
        html = render_digest(db.session)

    assert "<!DOCTYPE html>" in html
    assert schedule.email_subject in html


def test_render_digest_includes_enabled_blocks(app: object) -> None:
    """Enabled blocks should appear in the rendered digest."""
    from app.digest.renderer import render_digest

    with app.app_context():
        schedule = get_digest_schedule()
        block = db.session.scalar(
            sa.select(DigestBlock).where(
                DigestBlock.digest_schedule_id == schedule.id,
                DigestBlock.block_type == "server_stats",
            )
        )
        block.enabled = True
        db.session.commit()

        html = render_digest(db.session)
        title = block.config_json.get("title", "Servisní statistiky")

    assert title in html


def test_render_digest_skips_disabled_blocks(app: object) -> None:
    """Disabled blocks should not appear in the rendered digest."""
    from app.digest.renderer import render_digest

    with app.app_context():
        schedule = get_digest_schedule()
        block = db.session.scalar(
            sa.select(DigestBlock).where(
                DigestBlock.digest_schedule_id == schedule.id,
                DigestBlock.block_type == "free_text",
            )
        )
        block.enabled = False
        block.config_json = {"title": "UNIQUE_TITLE_XYZ_FREE_TEXT", "content": "hello"}
        db.session.commit()

        html = render_digest(db.session)

    assert "UNIQUE_TITLE_XYZ_FREE_TEXT" not in html


# ── run_record_metrics ────────────────────────────────────────────────────────

def test_run_record_metrics_inserts_snapshot(app: object) -> None:
    """run_record_metrics should insert a DigestMetricSnapshot row."""
    from app.scheduler_tasks import run_record_metrics
    from datetime import datetime, timezone

    with app.app_context():
        now = datetime.now(timezone.utc)
        run_record_metrics(db.session, now=now)
        snap = db.session.scalar(
            sa.select(DigestMetricSnapshot).where(
                DigestMetricSnapshot.metric_name == "outbox_pending_count",
                DigestMetricSnapshot.snapshot_at == now,
            )
        )
        assert snap is not None
        assert snap.metric_value >= 0


def test_run_record_metrics_prunes_old_rows(app: object) -> None:
    """run_record_metrics should delete snapshots older than 30 days."""
    from app.scheduler_tasks import run_record_metrics
    from datetime import datetime, timedelta, timezone

    with app.app_context():
        old_time = datetime.now(timezone.utc) - timedelta(days=31)
        db.session.add(DigestMetricSnapshot(
            snapshot_at=old_time,
            metric_name="outbox_pending_count",
            metric_value=5.0,
        ))
        db.session.commit()

        run_record_metrics(db.session, now=datetime.now(timezone.utc))

        old = db.session.scalar(
            sa.select(DigestMetricSnapshot).where(DigestMetricSnapshot.snapshot_at == old_time)
        )
        assert old is None


# ── run_admin_digest ──────────────────────────────────────────────────────────

def test_run_admin_digest_skips_when_disabled(app: object) -> None:
    """run_admin_digest should return False when schedule.enabled is False."""
    from app.scheduler_tasks import run_admin_digest
    from datetime import datetime, timezone

    with app.app_context():
        schedule = get_digest_schedule()
        schedule.enabled = False
        db.session.commit()
        now = datetime(2025, 1, 1, schedule.preferred_hour, 0, tzinfo=timezone.utc)
        result = run_admin_digest(db.session, now=now)

    assert result is False


def test_run_admin_digest_skips_wrong_hour(app: object) -> None:
    """run_admin_digest should skip if current local hour != preferred_hour."""
    from app.scheduler_tasks import run_admin_digest
    from datetime import datetime

    with app.app_context():
        tz = _local_tz(app)
        schedule = get_digest_schedule()
        schedule.enabled = True
        schedule.preferred_hour = 7
        schedule.last_sent_at = None
        db.session.commit()
        wrong_hour = 8  # 7 + 1 in local time
        now = datetime(2025, 1, 1, wrong_hour, 0, tzinfo=tz)
        result = run_admin_digest(db.session, now=now)

    assert result is False


def test_run_admin_digest_skips_already_sent_today(app: object) -> None:
    """run_admin_digest should skip if already sent today (same local calendar date)."""
    from app.scheduler_tasks import run_admin_digest
    from datetime import datetime

    with app.app_context():
        tz = _local_tz(app)
        schedule = get_digest_schedule()
        hour = schedule.preferred_hour
        now = datetime(2025, 6, 1, hour, 0, tzinfo=tz)
        schedule.enabled = True
        # Sent earlier the same day (e.g. at hour-1)
        schedule.last_sent_at = datetime(2025, 6, 1, max(hour - 1, 0), 0, tzinfo=tz)
        db.session.commit()
        result = run_admin_digest(db.session, now=now)

    assert result is False


def test_run_admin_digest_enqueues_on_new_day(app: object, admin_client: object) -> None:
    """run_admin_digest should fire if last_sent_at was a previous local day."""
    from app.scheduler_tasks import run_admin_digest
    from app.models.outbox import OutboxEmail
    from datetime import datetime

    with app.app_context():
        tz = _local_tz(app)
        schedule = get_digest_schedule()
        hour = schedule.preferred_hour
        now = datetime(2025, 6, 2, hour, 0, tzinfo=tz)
        schedule.enabled = True
        schedule.last_sent_at = datetime(2025, 6, 1, hour, 0, tzinfo=tz)  # yesterday
        db.session.commit()

        result = run_admin_digest(db.session, now=now)
        assert result is True

        outbox = db.session.scalars(
            sa.select(OutboxEmail).where(OutboxEmail.to_email == "admin@test.com")
        ).all()

    assert len(outbox) >= 1


def test_run_admin_digest_enqueues_when_due(app: object, admin_client: object) -> None:
    """run_admin_digest should enqueue emails when schedule is enabled and due."""
    from app.scheduler_tasks import run_admin_digest
    from app.models.outbox import OutboxEmail
    from datetime import datetime

    with app.app_context():
        tz = _local_tz(app)
        schedule = get_digest_schedule()
        hour = schedule.preferred_hour
        now = datetime(2025, 6, 1, hour, 0, tzinfo=tz)
        schedule.enabled = True
        schedule.last_sent_at = None
        db.session.commit()

        result = run_admin_digest(db.session, now=now)
        assert result is True

        outbox = db.session.scalars(
            sa.select(OutboxEmail).where(OutboxEmail.to_email == "admin@test.com")
        ).all()

    assert len(outbox) >= 1
    assert outbox[0].html_body is not None


# ── Routes ────────────────────────────────────────────────────────────────────

def test_digest_preview_returns_html(app: object, admin_client: object) -> None:
    """GET /admin/digest/preview should return 200 with HTML content."""
    with app.app_context():
        get_digest_schedule()  # seed

    resp = admin_client.get("/admin/digest/preview")
    assert resp.status_code == 200
    assert b"<!DOCTYPE html>" in resp.data


def test_digest_settings_page_ok(app: object, admin_client: object) -> None:
    """GET /admin/digest/ should return 200."""
    with app.app_context():
        get_digest_schedule()  # seed

    resp = admin_client.get("/admin/digest/")
    assert resp.status_code == 200


def test_digest_save_persists(app: object, admin_client: object) -> None:
    """POST /admin/digest/save should update DigestSchedule fields."""
    with app.app_context():
        schedule = get_digest_schedule()
        version = schedule.version

    csrf = _get_csrf(admin_client, "/admin/digest/")
    resp = admin_client.post("/admin/digest/save", data={
        "csrf_token": csrf,
        "enabled": "1",
        "frequency_hours": "12",
        "preferred_hour": "8",
        "email_subject": "Test Subject",
        "version": str(version),
    }, follow_redirects=True)
    assert resp.status_code == 200

    with app.app_context():
        updated = db.session.get(DigestSchedule, 1)
        assert updated.frequency_hours == 12
        assert updated.email_subject == "Test Subject"


def test_digest_requires_permission(app: object, member_client: object) -> None:
    """Non-admin user should get 403 on digest routes."""
    resp = member_client.get("/admin/digest/")
    assert resp.status_code == 403


# ── save (stale version) ──────────────────────────────────────────────────────


def test_save_stale_version_flashes_danger(app, admin_client):
    """POST /admin/digest/save with wrong version → flash + redirect."""
    _seed_schedule(app)
    csrf = _get_csrf(admin_client, "/admin/digest/")
    resp = admin_client.post("/admin/digest/save", data={
        "csrf_token": csrf,
        "enabled": "1",
        "frequency_hours": "24",
        "preferred_hour": "7",
        "email_subject": "X",
        "version": "9999",  # stale
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert "mezitím" in resp.data.decode() or "znovu" in resp.data.decode()


# ── add_block ─────────────────────────────────────────────────────────────────


class TestAddBlock:
    def test_add_valid_block_type(self, app, admin_client):
        """Adding a valid extra block creates a new DigestBlock row."""
        _seed_schedule(app)
        csrf = _get_csrf(admin_client, "/admin/digest/")
        with app.app_context():
            before = db.session.scalar(
                sa.select(sa.func.count()).select_from(DigestBlock)
            )
        resp = admin_client.post("/admin/digest/blocks/add", data={
            "csrf_token": csrf,
            "block_type": "free_text",
        }, follow_redirects=True)
        assert resp.status_code == 200
        with app.app_context():
            after = db.session.scalar(sa.select(sa.func.count()).select_from(DigestBlock))
        assert after == before + 1

    def test_add_invalid_block_type_flashes_danger(self, app, admin_client):
        """An unknown block_type should flash an error and not add a row."""
        _seed_schedule(app)
        csrf = _get_csrf(admin_client, "/admin/digest/")
        with app.app_context():
            before = db.session.scalar(sa.select(sa.func.count()).select_from(DigestBlock))
        resp = admin_client.post("/admin/digest/blocks/add", data={
            "csrf_token": csrf,
            "block_type": "does_not_exist",
        }, follow_redirects=True)
        assert resp.status_code == 200
        with app.app_context():
            after = db.session.scalar(sa.select(sa.func.count()).select_from(DigestBlock))
        assert after == before

    def test_add_block_at_max_flashes_danger(self, app, admin_client):
        """Adding a 6th block of the same type should flash an error."""
        _seed_schedule(app)
        csrf = _get_csrf(admin_client, "/admin/digest/")
        # Add 4 more free_text blocks (there is already 1 seeded → total 5 = max)
        for _ in range(4):
            admin_client.post("/admin/digest/blocks/add", data={
                "csrf_token": csrf,
                "block_type": "free_text",
            }, follow_redirects=True)
        # This 6th attempt should be rejected
        resp = admin_client.post("/admin/digest/blocks/add", data={
            "csrf_token": csrf,
            "block_type": "free_text",
        }, follow_redirects=True)
        assert resp.status_code == 200
        assert "nejvýše" in resp.data.decode() or b"5" in resp.data

    def test_add_block_member_forbidden(self, app, member_client):
        resp = member_client.post("/admin/digest/blocks/add", data={"block_type": "free_text"})
        assert resp.status_code == 403


# ── save_block ────────────────────────────────────────────────────────────────


class TestSaveBlock:
    def test_save_block_updates_config(self, app, admin_client):
        """POST /admin/digest/blocks/<id>/save should update config_json."""
        _seed_schedule(app)
        block_id = _first_block_id(app, "free_text")
        with app.app_context():
            block = db.session.get(DigestBlock, block_id)
            version = block.version

        csrf = _get_csrf(admin_client, "/admin/digest/")
        resp = admin_client.post(f"/admin/digest/blocks/{block_id}/save", data={
            "csrf_token": csrf,
            "title": "Moje Zpráva",
            "content": "Ahoj světe!",
            "version": str(version),
        }, follow_redirects=True)
        assert resp.status_code == 200

        with app.app_context():
            updated = db.session.get(DigestBlock, block_id)
            assert updated.config_json.get("title") == "Moje Zpráva"
            assert updated.config_json.get("content") == "Ahoj světe!"

    def test_save_block_stale_version_flashes(self, app, admin_client):
        _seed_schedule(app)
        block_id = _first_block_id(app, "free_text")
        csrf = _get_csrf(admin_client, "/admin/digest/")
        resp = admin_client.post(f"/admin/digest/blocks/{block_id}/save", data={
            "csrf_token": csrf,
            "title": "X",
            "version": "9999",
        }, follow_redirects=True)
        assert resp.status_code == 200
        assert "mezitím" in resp.data.decode() or "znovu" in resp.data.decode()

    def test_save_block_404_on_missing(self, app, admin_client):
        _seed_schedule(app)
        csrf = _get_csrf(admin_client, "/admin/digest/")
        resp = admin_client.post("/admin/digest/blocks/99999/save", data={
            "csrf_token": csrf,
            "title": "X",
            "version": "0",
        })
        assert resp.status_code == 404

    def test_save_block_member_forbidden(self, app, member_client):
        resp = member_client.post("/admin/digest/blocks/1/save", data={"version": "0"})
        assert resp.status_code == 403


# ── delete_block ──────────────────────────────────────────────────────────────


class TestDeleteBlock:
    def test_delete_block_removes_row(self, app, admin_client):
        """Deleting a block removes it from the database."""
        _seed_schedule(app)
        # Add a fresh free_text block to delete (avoid deleting the seeded one)
        csrf = _get_csrf(admin_client, "/admin/digest/")
        admin_client.post("/admin/digest/blocks/add", data={
            "csrf_token": csrf,
            "block_type": "free_text",
        }, follow_redirects=True)
        with app.app_context():
            block = db.session.scalar(
                sa.select(DigestBlock).where(DigestBlock.block_type == "free_text")
                .order_by(DigestBlock.id.desc())
            )
            block_id = block.id

        resp = admin_client.post(f"/admin/digest/blocks/{block_id}/delete", data={
            "csrf_token": csrf,
        }, follow_redirects=True)
        assert resp.status_code == 200
        with app.app_context():
            assert db.session.get(DigestBlock, block_id) is None

    def test_delete_block_404_on_missing(self, app, admin_client):
        _seed_schedule(app)
        csrf = _get_csrf(admin_client, "/admin/digest/")
        resp = admin_client.post("/admin/digest/blocks/99999/delete", data={
            "csrf_token": csrf,
        })
        assert resp.status_code == 404

    def test_delete_block_member_forbidden(self, app, member_client):
        resp = member_client.post("/admin/digest/blocks/1/delete", data={})
        assert resp.status_code == 403


# ── toggle_block ──────────────────────────────────────────────────────────────


class TestToggleBlock:
    def test_toggle_flips_enabled(self, app, admin_client):
        """POST toggle endpoint returns JSON and flips the enabled flag."""
        _seed_schedule(app)
        block_id = _first_block_id(app, "free_text")
        with app.app_context():
            original = db.session.get(DigestBlock, block_id).enabled

        csrf = _get_csrf(admin_client, "/admin/digest/")
        resp = admin_client.post(f"/admin/digest/blocks/{block_id}/toggle", data={
            "csrf_token": csrf,
        }, content_type="application/x-www-form-urlencoded")
        assert resp.status_code == 200
        payload = resp.get_json()
        assert payload["ok"] is True
        assert payload["enabled"] == (not original)

        with app.app_context():
            assert db.session.get(DigestBlock, block_id).enabled == (not original)

    def test_toggle_block_404_on_missing(self, app, admin_client):
        _seed_schedule(app)
        csrf = _get_csrf(admin_client, "/admin/digest/")
        resp = admin_client.post("/admin/digest/blocks/99999/toggle", data={
            "csrf_token": csrf,
        })
        assert resp.status_code == 404

    def test_toggle_block_member_forbidden(self, app, member_client):
        resp = member_client.post("/admin/digest/blocks/1/toggle", data={})
        assert resp.status_code == 403


# ── reorder_blocks ────────────────────────────────────────────────────────────


class TestReorderBlocks:
    def test_reorder_updates_sort_order(self, app, admin_client):
        """POST /admin/digest/blocks/reorder with reversed IDs should update sort_order."""
        _seed_schedule(app)
        with app.app_context():
            blocks = db.session.scalars(
                sa.select(DigestBlock).order_by(DigestBlock.sort_order)
            ).all()
            ids = [b.id for b in blocks]

        reversed_ids = list(reversed(ids))
        csrf = _get_csrf(admin_client, "/admin/digest/")
        resp = admin_client.post(
            "/admin/digest/blocks/reorder",
            json=reversed_ids,
            headers={"X-CSRFToken": csrf},
        )
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True

        with app.app_context():
            for expected_order, block_id in enumerate(reversed_ids):
                b = db.session.get(DigestBlock, block_id)
                assert b.sort_order == expected_order

    def test_reorder_member_forbidden(self, app, member_client):
        resp = member_client.post("/admin/digest/blocks/reorder", json=[1, 2, 3])
        assert resp.status_code == 403


# ── send_test ─────────────────────────────────────────────────────────────────


class TestSendTest:
    def test_no_email_flashes_danger(self, app, admin_client):
        """POSTing with empty test_email should flash a danger message."""
        _seed_schedule(app)
        csrf = _get_csrf(admin_client, "/admin/digest/")
        resp = admin_client.post("/admin/digest/send-test", data={
            "csrf_token": csrf,
            "test_email": "",
        }, follow_redirects=True)
        assert resp.status_code == 200
        assert b"adresu" in resp.data or b"Zadejte" in resp.data

    def test_valid_email_enqueues_to_outbox(self, app, admin_client):
        """A valid test_email address should create an OutboxEmail row."""
        from app.models.outbox import OutboxEmail

        _seed_schedule(app)
        csrf = _get_csrf(admin_client, "/admin/digest/")
        resp = admin_client.post("/admin/digest/send-test", data={
            "csrf_token": csrf,
            "test_email": "test@example.com",
        }, follow_redirects=True)
        assert resp.status_code == 200

        with app.app_context():
            row = db.session.scalar(
                sa.select(OutboxEmail).where(OutboxEmail.to_email == "test@example.com")
            )
        assert row is not None
        assert row.html_body is not None

    def test_send_test_member_forbidden(self, app, member_client):
        resp = member_client.post("/admin/digest/send-test", data={"test_email": "x@x.com"})
        assert resp.status_code == 403


# ── send_now ──────────────────────────────────────────────────────────────────


class TestSendNow:
    def test_send_now_enqueues_for_admin(self, app, admin_client):
        """POST /admin/digest/send-now should enqueue at least one OutboxEmail."""
        from app.models.outbox import OutboxEmail

        _seed_schedule(app)
        csrf = _get_csrf(admin_client, "/admin/digest/")
        resp = admin_client.post("/admin/digest/send-now", data={
            "csrf_token": csrf,
        }, follow_redirects=True)
        assert resp.status_code == 200

        with app.app_context():
            count = db.session.scalar(
                sa.select(sa.func.count()).select_from(OutboxEmail)
            )
        assert count >= 1

    def test_send_now_member_forbidden(self, app, member_client):
        resp = member_client.post("/admin/digest/send-now", data={})
        assert resp.status_code == 403


# ── _merge_block_config per-type branches ────────────────────────────────────


class TestMergeBlockConfig:
    """Verify that save_block correctly merges form fields for each block type."""

    def _save(self, client, app, block_type: str, extra: dict) -> None:
        """POST save_block for the first block of block_type."""
        _seed_schedule(app)
        block_id = _first_block_id(app, block_type)
        with app.app_context():
            version = db.session.get(DigestBlock, block_id).version
        csrf = _get_csrf(client, "/admin/digest/")
        data = {"csrf_token": csrf, "title": "T", "version": str(version)}
        data.update(extra)
        resp = client.post(
            f"/admin/digest/blocks/{block_id}/save", data=data, follow_redirects=True
        )
        assert resp.status_code == 200
        return block_id

    def test_server_stats_config_saved(self, app, admin_client):
        block_id = self._save(admin_client, app, "server_stats", {
            "show_user_count": "1",
            "show_event_count": "1",
            "peak_hours": "12",
        })
        with app.app_context():
            cfg = db.session.get(DigestBlock, block_id).config_json
        assert cfg["show_user_count"] is True
        assert cfg["show_event_count"] is True
        assert cfg["show_db_size"] is False  # unchecked → False
        assert cfg["peak_hours"] == 12

    def test_audit_log_config_saved(self, app, admin_client):
        block_id = self._save(admin_client, app, "audit_log", {
            "hours": "48",
            "max_rows": "100",
            "show_actor": "1",
            "entity_types": ["Event", "UserAccount"],
            "action_types": ["create", "delete"],
        })
        with app.app_context():
            cfg = db.session.get(DigestBlock, block_id).config_json
        assert cfg["hours"] == 48
        assert cfg["max_rows"] == 100
        assert cfg["show_actor"] is True
        assert "Event" in cfg["entity_types"]
        assert "create" in cfg["action_types"]

    def test_upcoming_events_config_saved(self, app, admin_client):
        block_id = self._save(admin_client, app, "upcoming_events", {
            "days_ahead": "14",
            "max_rows": "20",
            "show_unfilled_only": "1",
        })
        with app.app_context():
            cfg = db.session.get(DigestBlock, block_id).config_json
        assert cfg["days_ahead"] == 14
        assert cfg["max_rows"] == 20
        assert cfg["show_unfilled_only"] is True

    def test_new_users_config_saved(self, app, admin_client):
        block_id = self._save(admin_client, app, "new_users", {
            "hours": "72",
            "max_rows": "30",
            "show_pending_only": "1",
        })
        with app.app_context():
            cfg = db.session.get(DigestBlock, block_id).config_json
        assert cfg["hours"] == 72
        assert cfg["max_rows"] == 30
        assert cfg["show_pending_only"] is True

    def test_feedback_summary_config_saved(self, app, admin_client):
        block_id = self._save(admin_client, app, "feedback_summary", {
            "hours": "12",
            "max_rows": "50",
        })
        with app.app_context():
            cfg = db.session.get(DigestBlock, block_id).config_json
        assert cfg["hours"] == 12
        assert cfg["max_rows"] == 50
