"""
Tests for the email outbox pipeline and individual send_* helpers.

Strategy:
  - All tests use unittest.mock.patch to replace flask_mail.Mail.send so no
    real SMTP connection is made.
  - Tests verify that the correct OutboxEmail rows are created (subject,
    recipient, body keywords) and that the scheduler's process_email_queue
    function transitions rows through pending → sent / failed correctly.
"""

import importlib
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from click.testing import CliRunner
from flask import g as flask_g
from sqlalchemy.exc import IntegrityError, OperationalError, ProgrammingError

import scheduler.main as scheduler_main
from app.extensions import db
from app.mail import (
    _EVENT_CHANGED_CHANGE_TYPE,
    _merge_event_changed_payloads,
    drain_batched_outbox,
    drain_one_outbox_email,
    enqueue_deferred,
    send_admin_digest,
    send_assignment_confirmed,
    send_assignment_released,
    send_assignments_opened,
    send_debriefing_invitation,
    send_event_cancelled,
    send_event_changed,
    send_event_published,
    send_unfilled_spots_reminder,
)
from app.models.assignment import Assignment
from app.models.audit import AuditLogEntry
from app.models.event import Event, EventSpot, EventStatus
from app.models.master_event import MasterEvent
from app.models.outbox import OutboxEmail
from app.models.qualification import Qualification
from app.models.role import Role
from app.models.settings import get_settings
from app.models.user import UserAccount

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_event(name: str = "Testovací akce") -> Event:
    me = MasterEvent(name="Obecné", description="")
    db.session.add(me)
    db.session.flush()
    event = Event(
        name=name,
        master_event_id=me.id,
        start_datetime=datetime(2026, 8, 1, 9, 0, tzinfo=timezone.utc),
        end_datetime=datetime(2026, 8, 1, 18, 0, tzinfo=timezone.utc),
        address="Praha",
        status=EventStatus.ASSIGNMENTS_OPEN,
    )
    db.session.add(event)
    db.session.flush()
    return event


def _make_member_user(email: str = "member@test.cz", name: str = "Test Member") -> UserAccount:
    """Create an active Member user (minimum role for operational emails)."""

    role = db.session.scalar(db.select(Role).where(Role.name == "Member"))
    user = UserAccount(email=email, name=name, is_active=True)
    user.set_password("pass")
    user.roles = [role]
    db.session.add(user)
    db.session.flush()
    return user


# ── Outbox enqueue tests ───────────────────────────────────────────────────────


class TestOutboxEnqueue:
    """Verify that send_* helpers enqueue the correct OutboxEmail rows."""

    def test_send_assignment_confirmed_enqueues_row(self, app):

        with app.app_context():
            event = _make_event("Závody 2026")
            user = _make_member_user("jan@test.cz", "Jan Novák")
            send_assignment_confirmed(user, event)
            db.session.commit()

            rows = db.session.scalars(db.select(OutboxEmail)).all()
            assert len(rows) == 1
            row = rows[0]
            assert row.to_email == "jan@test.cz"
            assert "Závody 2026" in row.subject
            assert row.status == "pending"
            assert row.html_body is None

    def test_send_assignment_released_enqueues_row(self, app):

        with app.app_context():
            event = _make_event("Závody 2026")
            user = _make_member_user("jan@test.cz", "Jan Novák")
            send_assignment_released(user, event)
            db.session.commit()

            row = db.session.scalars(db.select(OutboxEmail)).first()
            assert row is not None
            assert "Odhlášení" in row.subject
            assert row.to_email == "jan@test.cz"

    def test_send_event_published_enqueues_row(self, app):

        with app.app_context():
            event = _make_event("Letní festival")
            user = _make_member_user("petra@test.cz", "Petra Svobodová")
            send_event_published(user, event)
            db.session.commit()

            row = db.session.scalars(db.select(OutboxEmail)).first()
            assert row is not None
            assert "Letní festival" in row.subject
            assert "petra@test.cz" == row.to_email

    def test_send_assignments_opened_enqueues_row(self, app):

        with app.app_context():
            event = _make_event("Maraton")
            user = _make_member_user()
            send_assignments_opened(user, event)
            db.session.commit()

            row = db.session.scalars(db.select(OutboxEmail)).first()
            assert row is not None
            assert "Otevřeny" in row.subject

    def test_send_event_cancelled_enqueues_row(self, app):

        with app.app_context():
            event = _make_event("Zrušená akce")
            user = _make_member_user()
            send_event_cancelled(user, event)
            db.session.commit()

            row = db.session.scalars(db.select(OutboxEmail)).first()
            assert row is not None
            assert "zrušena" in row.subject.lower()

    def test_send_unfilled_spots_reminder_enqueues_row(self, app):

        with app.app_context():
            event = _make_event("Akce s mezerami")
            user = _make_member_user("coord@test.cz", "Koordinátor")
            send_unfilled_spots_reminder(user, event, unfilled=[1, 2, 3])
            db.session.commit()

            row = db.session.scalars(db.select(OutboxEmail)).first()
            assert row is not None
            assert "coord@test.cz" == row.to_email
            assert row.change_type == "unfilled_reminder"
            payload = json.loads(row.change_value)
            assert payload == {"unfilled_count": 3}

    def test_multiple_enqueues_all_pending(self, app):
        """All enqueued rows start as 'pending'."""

        with app.app_context():
            event = _make_event()
            user_a = _make_member_user("a@test.cz", "A")
            user_b = _make_member_user("b@test.cz", "B")
            send_assignment_confirmed(user_a, event)
            send_event_cancelled(user_b, event)
            db.session.commit()

            rows = db.session.scalars(db.select(OutboxEmail)).all()
            assert len(rows) == 2
            assert all(r.status == "pending" for r in rows)

    def test_viewer_only_does_not_enqueue(self, app):
        """Viewer-only users must not receive operational emails (AD17)."""

        with app.app_context():
            event = _make_event("Test akce")
            viewer_role = db.session.scalar(db.select(Role).where(Role.name == "Viewer"))
            viewer = UserAccount(email="viewer@test.cz", name="Viewer User", is_active=True)
            viewer.set_password("pass")
            viewer.roles = [viewer_role]
            db.session.add(viewer)
            db.session.flush()

            send_assignment_confirmed(viewer, event)
            send_event_published(viewer, event)
            db.session.commit()

            rows = db.session.scalars(db.select(OutboxEmail)).all()
            assert len(rows) == 0, "Viewer-only user should not receive any operational emails"

    def test_viewer_plus_member_receives_emails(self, app):
        """User with Viewer + Member roles must still receive Member emails (AD17)."""

        with app.app_context():
            event = _make_event("Test akce")
            viewer_role = db.session.scalar(db.select(Role).where(Role.name == "Viewer"))
            member_role = db.session.scalar(db.select(Role).where(Role.name == "Member"))
            user = UserAccount(email="mixed@test.cz", name="Mixed User", is_active=True)
            user.set_password("pass")
            user.roles = [viewer_role, member_role]
            db.session.add(user)
            db.session.flush()

            send_assignment_confirmed(user, event)
            db.session.commit()

            row = db.session.scalars(db.select(OutboxEmail)).first()
            assert row is not None
            assert row.to_email == "mixed@test.cz"


# ── Dev email block tests ─────────────────────────────────────────────────────


class TestDevEmailBlock:
    """Verify the dev_email_block + allowlist logic in drain_one_outbox_email."""

    def _seed_pending(self, app, to: str = "user@example.com") -> int:
        with app.app_context():
            row = OutboxEmail(to_email=to, subject="Test", body="Tělo")
            db.session.add(row)
            db.session.commit()
            return row.id

    def _set_dev_block(self, app, block: bool, allowlist: str | None = None) -> None:
        with app.app_context():

            s = get_settings()
            s.dev_email_block = block
            s.dev_email_allowlist = allowlist
            db.session.commit()

    def test_block_off_sends_normally(self, app):
        """When dev_email_block is False, email sends normally."""
        self._set_dev_block(app, False)
        row_id = self._seed_pending(app)
        with app.app_context():
            with patch("flask_mail.Mail.send"):

                drain_one_outbox_email()
        with app.app_context():
            row = db.session.get(OutboxEmail, row_id)
            assert row.status == "sent"

    def test_block_on_no_allowlist_skips_email(self, app):
        """When block is on and allowlist is empty, email is skipped."""
        self._set_dev_block(app, True, None)
        row_id = self._seed_pending(app)
        with app.app_context():
            with patch("flask_mail.Mail.send") as mock_send:

                drain_one_outbox_email()
        mock_send.assert_not_called()
        with app.app_context():
            row = db.session.get(OutboxEmail, row_id)
            assert row.status == "skipped"
            assert "dev_email_block" in row.last_error

    def test_block_on_recipient_not_in_allowlist_skips(self, app):
        """Recipient not in allowlist is skipped even with other entries present."""
        self._set_dev_block(app, True, "admin@example.com, tester@example.com")
        row_id = self._seed_pending(app, to="outsider@example.com")
        with app.app_context():
            with patch("flask_mail.Mail.send") as mock_send:

                drain_one_outbox_email()
        mock_send.assert_not_called()
        with app.app_context():
            row = db.session.get(OutboxEmail, row_id)
            assert row.status == "skipped"

    def test_block_on_recipient_in_allowlist_sends(self, app):
        """Recipient in allowlist is sent even when block is on."""
        self._set_dev_block(app, True, "admin@example.com, tester@example.com")
        row_id = self._seed_pending(app, to="tester@example.com")
        with app.app_context():
            with patch("flask_mail.Mail.send"):

                drain_one_outbox_email()
        with app.app_context():
            row = db.session.get(OutboxEmail, row_id)
            assert row.status == "sent"

    def test_allowlist_matching_is_case_insensitive(self, app):
        """Allowlist matching ignores case differences."""
        self._set_dev_block(app, True, "Admin@Example.COM")
        row_id = self._seed_pending(app, to="admin@example.com")
        with app.app_context():
            with patch("flask_mail.Mail.send"):

                drain_one_outbox_email()
        with app.app_context():
            row = db.session.get(OutboxEmail, row_id)
            assert row.status == "sent"

    def test_is_email_allowed_helper(self, app):
        """Unit test AppSettings.is_email_allowed() directly."""
        with app.app_context():

            s = get_settings()
            s.dev_email_block = False
            assert s.is_email_allowed("anyone@example.com") is True

            s.dev_email_block = True
            s.dev_email_allowlist = None
            assert s.is_email_allowed("anyone@example.com") is False

            s.dev_email_allowlist = "a@b.com, c@d.com"
            assert s.is_email_allowed("a@b.com") is True
            assert s.is_email_allowed("A@B.COM") is True
            assert s.is_email_allowed("x@y.com") is False


# ── Scheduler queue processing tests ─────────────────────────────────────────


class TestProcessEmailQueue:
    """Verify the drain_one_outbox_email function transitions rows correctly."""

    def _seed_pending(self, app, to: str = "test@test.cz") -> int:
        with app.app_context():
            row = OutboxEmail(to_email=to, subject="Test", body="Tělo zprávy")
            db.session.add(row)
            db.session.commit()
            return row.id

    def test_successful_send_marks_row_sent(self, app):
        row_id = self._seed_pending(app)

        with app.app_context():
            with patch("flask_mail.Mail.send"):

                drain_one_outbox_email()

        with app.app_context():
            row = db.session.get(OutboxEmail, row_id)
            assert row.status == "sent"
            assert row.sent_at is not None
            assert row.retry_count == 0

    def test_smtp_failure_increments_retry_count(self, app):
        row_id = self._seed_pending(app)

        with app.app_context():
            with patch("flask_mail.Mail.send", side_effect=Exception("Connection refused")):

                drain_one_outbox_email()

        with app.app_context():
            row = db.session.get(OutboxEmail, row_id)
            assert row.status == "pending"
            assert row.retry_count == 1
            assert "Connection refused" in row.last_error

    def test_exhausted_retries_marks_row_failed(self, app):
        """After MAX_RETRIES failures the row must be permanently 'failed'."""
        with app.app_context():
            row = OutboxEmail(
                to_email="x@test.cz",
                subject="Test",
                body="...",
                retry_count=OutboxEmail.MAX_RETRIES - 1,
            )
            db.session.add(row)
            db.session.commit()
            row_id = row.id

        with app.app_context():
            with patch("flask_mail.Mail.send", side_effect=Exception("timeout")):

                drain_one_outbox_email()

        with app.app_context():
            row = db.session.get(OutboxEmail, row_id)
            assert row.status == "failed"
            assert row.retry_count == OutboxEmail.MAX_RETRIES
            # Permanent failure should produce an audit log entry

            entry = db.session.scalar(
                db.select(AuditLogEntry).where(
                    AuditLogEntry.entity_type == "OutboxEmail",
                    AuditLogEntry.action_type == "email_failed",
                    AuditLogEntry.entity_id == str(row_id),
                )
            )
            assert entry is not None
            assert "x@test.cz" in entry.summary

    def test_already_failed_rows_are_skipped(self, app):
        """Rows with status='failed' must never be retried."""
        with app.app_context():
            row = OutboxEmail(
                to_email="x@test.cz",
                subject="Test",
                body="...",
                status="failed",
                retry_count=OutboxEmail.MAX_RETRIES,
            )
            db.session.add(row)
            db.session.commit()

        with app.app_context():
            with patch("flask_mail.Mail.send") as mock_send:

                drain_one_outbox_email()

        mock_send.assert_not_called()

    def test_already_sent_rows_are_skipped(self, app):
        """Rows with status='sent' must never be re-delivered."""
        with app.app_context():
            row = OutboxEmail(
                to_email="x@test.cz",
                subject="Test",
                body="...",
                status="sent",
            )
            db.session.add(row)
            db.session.commit()

        with app.app_context():
            with patch("flask_mail.Mail.send") as mock_send:

                drain_one_outbox_email()

        mock_send.assert_not_called()

    def test_empty_queue_returns_false(self, app):
        """Drain_one_outbox_email on an empty outbox must return False."""
        with app.app_context():
            with patch("flask_mail.Mail.send") as mock_send:

                result = drain_one_outbox_email()

        assert result is False
        mock_send.assert_not_called()

    def test_processes_oldest_row_first(self, app):
        """Rows must be delivered in FIFO order (oldest created_at first)."""
        with app.app_context():
            older = OutboxEmail(
                to_email="older@test.cz",
                subject="Starší",
                body="...",
                created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            )
            newer = OutboxEmail(
                to_email="newer@test.cz",
                subject="Novější",
                body="...",
                created_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
            )
            db.session.add_all([newer, older])  # insert newer first intentionally
            db.session.commit()

        sent_recipients: list[str] = []

        def _capture_send(msg: object) -> None:
            sent_recipients.append(getattr(msg, "recipients", [None])[0])

        with app.app_context():
            with patch("flask_mail.Mail.send", side_effect=_capture_send):

                drain_one_outbox_email()  # processes one row per call

        assert sent_recipients == ["older@test.cz"]


# ── SMTP settings admin route tests ──────────────────────────────────────────


class TestSmtpAdminSettings:
    """Verify the admin settings page handles SMTP config correctly."""

    def test_settings_page_does_not_expose_smtp_password(self, admin_client):
        response = admin_client.get("/admin/settings/", follow_redirects=True)
        assert response.status_code == 200
        assert b"devpassword" not in response.data
        assert b"smtp_password_enc" not in response.data

    def test_smtp_not_configured_exits_nonzero(self, app):
        """CLI send-test-email must exit 1 when SMTP is not configured."""

        with app.app_context():

            settings = get_settings()
            settings.smtp_server = None
            db.session.commit()

        runner = CliRunner()
        with app.app_context():
            cmd = app.cli.commands["send-test-email"]
            result = runner.invoke(cmd, ["nobody@test.cz"], catch_exceptions=False)

        assert result.exit_code != 0
        assert "SMTP" in result.output


# ── OutboxEmail model unit tests ──────────────────────────────────────────────


class TestOutboxEmailModel:
    """Unit tests for the OutboxEmail model itself."""

    def test_default_status_is_pending(self, app):
        with app.app_context():
            row = OutboxEmail(to_email="a@b.com", subject="S", body="B")
            db.session.add(row)
            db.session.commit()
            assert row.status == "pending"

    def test_default_retry_count_is_zero(self, app):
        with app.app_context():
            row = OutboxEmail(to_email="a@b.com", subject="S", body="B")
            db.session.add(row)
            db.session.commit()
            assert row.retry_count == 0

    def test_max_retries_constant(self):
        assert OutboxEmail.MAX_RETRIES == 3

    def test_repr(self, app):
        with app.app_context():
            row = OutboxEmail(to_email="a@b.com", subject="S", body="B")
            db.session.add(row)
            db.session.commit()
            assert "a@b.com" in repr(row)
            assert "pending" in repr(row)


# ── OutboxEmail new columns ──────────────────────────────────────────────────


class TestOutboxEmailNewColumns:
    """New columns default to NULL; populated values round-trip."""

    def test_new_columns_default_null(self, app):
        """Creating an OutboxEmail with only legacy args leaves all new cols NULL."""
        with app.app_context():
            row = OutboxEmail(to_email="legacy@test.cz", subject="S", body="B")
            db.session.add(row)
            db.session.commit()
            db.session.expire(row)
            assert row.user_id is None
            assert row.event_id is None
            assert row.change_type is None
            assert row.change_value is None
            assert row.send_after is None

    def test_new_columns_round_trip(self, app):
        """When new columns are set, they survive a flush + re-fetch."""
        with app.app_context():
            me = MasterEvent(name="RT ME")
            db.session.add(me)
            db.session.flush()
            event = Event(
                name="RT Event",
                master_event_id=me.id,
                start_datetime=datetime(2030, 1, 1, 9, 0, tzinfo=timezone.utc),
                end_datetime=datetime(2030, 1, 1, 17, 0, tzinfo=timezone.utc),
            )
            db.session.add(event)
            db.session.flush()
            send_at = datetime(2030, 1, 1, 8, 0, tzinfo=timezone.utc)
            row = OutboxEmail(
                to_email="batch@test.cz",
                subject="Batched",
                body="body",
                event_id=event.id,
                change_type="field_edit",
                change_value='{"name":["A","B"]}',
                send_after=send_at,
            )
            db.session.add(row)
            db.session.commit()
            row_id = row.id
            event_id_saved = event.id
            db.session.expunge_all()

            fetched = db.session.get(OutboxEmail, row_id)
            assert fetched.event_id == event_id_saved
            assert fetched.change_type == "field_edit"
            assert fetched.change_value == '{"name":["A","B"]}'
            assert fetched.send_after is not None


# ── non-event helper columns null ────────────────────────────────────────────


class TestNonEventHelperNullColumns:
    """Non-event send_* helpers produce rows with send_after/user_id/event_id all NULL."""

    def test_non_event_helper_has_null_batching_columns(self, app):
        with app.app_context():
            send_admin_digest("admin@test.cz", "Digest", "<p>body</p>")
            db.session.commit()
            row = db.session.scalar(db.select(OutboxEmail).where(OutboxEmail.notification_type == "admin_digest"))
            assert row is not None
            assert row.send_after is None
            assert row.user_id is None
            assert row.event_id is None


# ── enqueue_deferred helper ──────────────────────────────────────────────────


def _make_ed_event(delta_hours: float | None = None) -> tuple[UserAccount, Event]:
    """Create a member user + event inside the current app context."""
    me = MasterEvent(name="ED-ME")
    db.session.add(me)
    db.session.flush()
    role = db.session.scalar(db.select(Role).where(Role.name == "Member"))
    user = UserAccount(email="ed_user@test.cz", name="ED User", is_active=True)
    user.set_password("x")
    user.roles = [role]
    db.session.add(user)
    db.session.flush()
    if delta_hours is None:
        start = datetime(2030, 1, 1, tzinfo=timezone.utc)
    else:
        start = datetime.now(timezone.utc) + timedelta(hours=delta_hours)
    event = Event(
        name="ED Event",
        master_event_id=me.id,
        start_datetime=start,
        end_datetime=start + timedelta(hours=2),
    )
    db.session.add(event)
    db.session.flush()
    return user, event


class TestEnqueueDeferred:
    """Enqueue_deferred tier logic, upsert, and immediate bypass."""

    def test_tier1_delta_12h_yields_5min(self, app):
        with app.app_context():
            user, event = _make_ed_event(delta_hours=12)
            enqueue_deferred(user, event, "assignment_confirmed", "Subj", "body")
            db.session.commit()
            row = db.session.scalar(
                db.select(OutboxEmail).where(OutboxEmail.notification_type == "assignment_confirmed")
            )
            assert row is not None
            assert row.send_after is not None
            expected = datetime.now(timezone.utc) + timedelta(minutes=5)
            assert abs((row.send_after - expected).total_seconds()) < 10

    def test_tier2_delta_3d_yields_60min(self, app):
        with app.app_context():
            user, event = _make_ed_event(delta_hours=72)
            enqueue_deferred(user, event, "event_published", "Subj", "body")
            db.session.commit()
            row = db.session.scalar(db.select(OutboxEmail).where(OutboxEmail.notification_type == "event_published"))
            assert row is not None
            assert row.send_after is not None
            expected = datetime.now(timezone.utc) + timedelta(minutes=60)
            assert abs((row.send_after - expected).total_seconds()) < 10

    def test_tier3_delta_14d_yields_360min(self, app):
        with app.app_context():
            user, event = _make_ed_event(delta_hours=14 * 24)
            enqueue_deferred(user, event, "event_changed", "Subj", "body")
            db.session.commit()
            row = db.session.scalar(db.select(OutboxEmail).where(OutboxEmail.notification_type == "event_changed"))
            assert row is not None
            assert row.send_after is not None
            expected = datetime.now(timezone.utc) + timedelta(minutes=360)
            assert abs((row.send_after - expected).total_seconds()) < 10

    def test_tier4_delta_60d_yields_1440min(self, app):
        with app.app_context():
            user, event = _make_ed_event(delta_hours=60 * 24)
            enqueue_deferred(user, event, "event_cancelled", "Subj", "body")
            db.session.commit()
            row = db.session.scalar(db.select(OutboxEmail).where(OutboxEmail.notification_type == "event_cancelled"))
            assert row is not None
            assert row.send_after is not None
            expected = datetime.now(timezone.utc) + timedelta(minutes=1440)
            assert abs((row.send_after - expected).total_seconds()) < 10

    def test_past_event_uses_tier1(self, app):
        with app.app_context():
            user, event = _make_ed_event(delta_hours=-1)
            enqueue_deferred(user, event, "assignment_released", "Subj", "body")
            db.session.commit()
            row = db.session.scalar(
                db.select(OutboxEmail).where(OutboxEmail.notification_type == "assignment_released")
            )
            assert row is not None
            assert row.send_after is not None
            expected = datetime.now(timezone.utc) + timedelta(minutes=5)
            assert abs((row.send_after - expected).total_seconds()) < 10

    def test_immediate_flag_yields_null_send_after(self, app):
        with app.app_context():
            user, event = _make_ed_event(delta_hours=12)
            with app.test_request_context("/"):
                flask_g._test_notification_immediate = True
                enqueue_deferred(user, event, "assignment_confirmed", "Subj", "body")
            db.session.commit()
            row = db.session.scalar(
                db.select(OutboxEmail).where(OutboxEmail.notification_type == "assignment_confirmed")
            )
            assert row is not None
            assert row.send_after is None

    def test_second_call_later_send_after_keeps_earlier(self, app):
        with app.app_context():
            user, event = _make_ed_event(delta_hours=72)  # tier 2 → 60 min
            enqueue_deferred(user, event, "event_changed", "Subj v1", "body")
            db.session.flush()
            # Second call with far-future event → tier 4 (1440 min) — later; must keep first
            event.start_datetime = datetime.now(timezone.utc) + timedelta(days=60)
            enqueue_deferred(user, event, "event_changed", "Subj v2", "body")
            db.session.commit()
            rows = db.session.scalars(
                db.select(OutboxEmail).where(OutboxEmail.notification_type == "event_changed")
            ).all()
            assert len(rows) == 1
            expected = datetime.now(timezone.utc) + timedelta(minutes=60)
            assert abs((rows[0].send_after - expected).total_seconds()) < 10

    def test_second_call_earlier_send_after_replaces_later(self, app):
        with app.app_context():
            user, event = _make_ed_event(delta_hours=60 * 24)  # tier 4 → 1440 min
            enqueue_deferred(user, event, "event_changed", "Subj v1", "body")
            db.session.flush()
            # Second call with near event → tier 1 (5 min) — earlier; must replace
            event.start_datetime = datetime.now(timezone.utc) + timedelta(hours=12)
            enqueue_deferred(user, event, "event_changed", "Subj v2", "body")
            db.session.commit()
            rows = db.session.scalars(
                db.select(OutboxEmail).where(OutboxEmail.notification_type == "event_changed")
            ).all()
            assert len(rows) == 1
            expected = datetime.now(timezone.utc) + timedelta(minutes=5)
            assert abs((rows[0].send_after - expected).total_seconds()) < 10

    def test_existing_null_send_after_stays_null(self, app):
        with app.app_context():
            user, event = _make_ed_event(delta_hours=12)
            with app.test_request_context("/"):
                flask_g._test_notification_immediate = True
                enqueue_deferred(user, event, "event_changed", "Subj v1", "body")
            db.session.flush()
            # Second call without immediate flag — must NOT overwrite NULL
            enqueue_deferred(user, event, "event_changed", "Subj v2", "body")
            db.session.commit()
            rows = db.session.scalars(
                db.select(OutboxEmail).where(OutboxEmail.notification_type == "event_changed")
            ).all()
            assert len(rows) == 1
            assert rows[0].send_after is None

    def test_two_calls_same_triple_single_row(self, app):
        with app.app_context():
            user, event = _make_ed_event(delta_hours=72)
            send_event_changed(user, event, {"name": ["A", "B"]})
            db.session.flush()
            send_event_changed(user, event, {"name": ["B", "C"]})
            db.session.commit()
            count = db.session.scalar(
                db.select(db.func.count(OutboxEmail.id)).where(
                    OutboxEmail.notification_type == "event_changed",
                    OutboxEmail.status == "pending",
                )
            )
            assert count == 1

    def test_two_users_two_rows(self, app):
        with app.app_context():
            user1, event = _make_ed_event(delta_hours=72)
            role = db.session.scalar(db.select(Role).where(Role.name == "Member"))
            user2 = UserAccount(email="ed_user2@test.cz", name="ED User 2", is_active=True)
            user2.set_password("x")
            user2.roles = [role]
            db.session.add(user2)
            db.session.flush()
            enqueue_deferred(user1, event, "event_changed", "Subj", "body")
            enqueue_deferred(user2, event, "event_changed", "Subj", "body")
            db.session.commit()
            count = db.session.scalar(
                db.select(db.func.count(OutboxEmail.id)).where(OutboxEmail.notification_type == "event_changed")
            )
            assert count == 2

    def test_two_events_two_rows(self, app):
        with app.app_context():
            user, event1 = _make_ed_event(delta_hours=72)
            me2 = MasterEvent(name="ED-ME2")
            db.session.add(me2)
            db.session.flush()
            start2 = datetime.now(timezone.utc) + timedelta(hours=72)
            event2 = Event(
                name="ED Event 2",
                master_event_id=me2.id,
                start_datetime=start2,
                end_datetime=start2 + timedelta(hours=2),
            )
            db.session.add(event2)
            db.session.flush()
            enqueue_deferred(user, event1, "event_changed", "Subj", "body")
            enqueue_deferred(user, event2, "event_changed", "Subj", "body")
            db.session.commit()
            count = db.session.scalar(
                db.select(db.func.count(OutboxEmail.id)).where(OutboxEmail.notification_type == "event_changed")
            )
            assert count == 2

    def test_gate_off_no_row_created(self, app):
        with app.app_context():
            settings = get_settings()
            settings.notify_assignment = False
            db.session.commit()

            user, event = _make_ed_event(delta_hours=12)
            send_assignment_confirmed(user, event)
            db.session.commit()
            count = db.session.scalar(db.select(db.func.count(OutboxEmail.id)))
            assert count == 0


class TestEnqueueDeferredErrorPolicy:
    """Enqueue_deferred: IntegrityError triggers retry-via-merge; other exceptions propagate."""

    def test_integrity_error_falls_back_to_merge(self, app):
        """IntegrityError on insert → rollback, re-select finds the winning row,
        merge branch runs, no exception propagates."""
        with app.app_context():
            user, event = _make_ed_event(delta_hours=72)
            db.session.commit()

            # Seed a winning row that the re-select after rollback will find.
            winner = OutboxEmail(
                to_email=user.email,
                subject="winner",
                body="winner-body",
                notification_type="event_published",
                user_id=user.id,
                event_id=event.id,
                send_after=datetime.now(timezone.utc) + timedelta(minutes=999),
            )
            db.session.add(winner)
            db.session.commit()
            winner_id = winner.id

            # Real flush; the racing IntegrityError is simulated on the INSERT path.
            real_flush = db.session.flush
            calls = {"n": 0}

            def flaky_flush(*args, **kwargs):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise IntegrityError("stmt", {}, Exception("unique violation"))
                return real_flush(*args, **kwargs)

            with patch.object(db.session, "flush", side_effect=flaky_flush):
                result = enqueue_deferred(user, event, "event_published", "newer", "newer-body")

            # Merge branch ran; winner row's subject was overwritten.
            db.session.commit()
            row = db.session.get(OutboxEmail, winner_id)
            assert row is not None
            assert row.subject == "newer"
            assert result is not None  # merged send_after

    def test_operational_error_propagates(self, app):
        """Non-integrity DB errors must NOT be swallowed — they signal infrastructure problems."""
        with app.app_context():
            user, event = _make_ed_event(delta_hours=72)
            db.session.commit()
            with patch.object(db.session, "flush", side_effect=OperationalError("stmt", {}, Exception("conn lost"))):
                with pytest.raises(OperationalError):
                    enqueue_deferred(user, event, "event_published", "S", "B")

    def test_programming_error_propagates(self, app):
        """ProgrammingError (bad SQL, developer bug) must NOT be swallowed."""
        with app.app_context():
            user, event = _make_ed_event(delta_hours=72)
            db.session.commit()
            with patch.object(db.session, "flush", side_effect=ProgrammingError("stmt", {}, Exception("bad sql"))):
                with pytest.raises(ProgrammingError):
                    enqueue_deferred(user, event, "event_published", "S", "B")

    def test_value_error_propagates(self, app):
        """Non-DB exceptions must NOT be swallowed."""
        with app.app_context():
            user, event = _make_ed_event(delta_hours=72)
            db.session.commit()
            with patch.object(db.session, "flush", side_effect=ValueError("boom")):
                with pytest.raises(ValueError):
                    enqueue_deferred(user, event, "event_published", "S", "B")


class TestOutboxPendingUniqueIndex:
    """Filtered unique index uq_outbox_pending_by_user_event_type behaviour."""

    def test_two_pending_rows_same_triple_raises_integrity_error(self, app):
        """Two pending rows with identical (user_id, event_id, notification_type) violate the index."""
        with app.app_context():
            user, event = _make_ed_event(delta_hours=72)
            db.session.commit()
            db.session.add(
                OutboxEmail(
                    to_email=user.email,
                    subject="a",
                    body="b",
                    notification_type="event_published",
                    user_id=user.id,
                    event_id=event.id,
                )
            )
            db.session.flush()
            db.session.add(
                OutboxEmail(
                    to_email=user.email,
                    subject="a2",
                    body="b2",
                    notification_type="event_published",
                    user_id=user.id,
                    event_id=event.id,
                )
            )
            with pytest.raises(IntegrityError):
                db.session.flush()
            db.session.rollback()

    def test_pending_and_sent_can_coexist(self, app):
        """A sent row plus a new pending row on the same triple must be allowed."""
        with app.app_context():
            user, event = _make_ed_event(delta_hours=72)
            db.session.commit()
            db.session.add(
                OutboxEmail(
                    to_email=user.email,
                    subject="old",
                    body="b",
                    notification_type="event_published",
                    user_id=user.id,
                    event_id=event.id,
                    status="sent",
                )
            )
            db.session.flush()
            db.session.add(
                OutboxEmail(
                    to_email=user.email,
                    subject="new",
                    body="b",
                    notification_type="event_published",
                    user_id=user.id,
                    event_id=event.id,
                    status="pending",
                )
            )
            db.session.flush()  # must not raise
            db.session.rollback()

    def test_null_user_and_event_rows_can_coexist(self, app):
        """Legacy pending rows with NULL user_id/event_id (invite, digest, ...) do not collide."""
        with app.app_context():
            db.session.add_all(
                [
                    OutboxEmail(
                        to_email="a@x.cz",
                        subject="a",
                        body="b",
                        notification_type="auth",
                        status="pending",
                    ),
                    OutboxEmail(
                        to_email="b@x.cz",
                        subject="a",
                        body="b",
                        notification_type="auth",
                        status="pending",
                    ),
                ]
            )
            db.session.flush()  # must not raise
            db.session.rollback()


# ── drain send_after filter ──────────────────────────────────────────────────


class TestDrainSendAfterFilter:
    """Rows with future send_after are held; past/null are sent."""

    def _seed(self, app, send_after: datetime | None) -> int:
        with app.app_context():
            row = OutboxEmail(to_email="d@test.cz", subject="S", body="B", send_after=send_after)
            db.session.add(row)
            db.session.commit()
            return row.id

    def test_drain_null_send_after_is_sent(self, app):
        row_id = self._seed(app, send_after=None)
        with app.app_context():
            with patch("flask_mail.Mail.send"):
                drain_one_outbox_email()
        with app.app_context():
            row = db.session.get(OutboxEmail, row_id)
            assert row.status == "sent"

    def test_drain_future_send_after_is_skipped(self, app):
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        row_id = self._seed(app, send_after=future)
        with app.app_context():
            with patch("flask_mail.Mail.send") as mock_send:
                drain_one_outbox_email()
        mock_send.assert_not_called()
        with app.app_context():
            row = db.session.get(OutboxEmail, row_id)
            assert row.status == "pending"

    def test_drain_past_send_after_is_sent(self, app):
        past = datetime.now(timezone.utc) - timedelta(minutes=1)
        row_id = self._seed(app, send_after=past)
        with app.app_context():
            with patch("flask_mail.Mail.send"):
                drain_one_outbox_email()
        with app.app_context():
            row = db.session.get(OutboxEmail, row_id)
            assert row.status == "sent"


# ── structured change_value + merge for event_changed ───────────────────────


class TestMergeEventChangedPayloads:
    """Unit tests for _merge_event_changed_payloads (pure function, no DB)."""

    def test_merge_same_field_keeps_earliest_old(self):
        result = _merge_event_changed_payloads('{"name": ["A", "B"]}', {"name": ["B", "C"]})
        assert result == {"name": ["A", "C"]}

    def test_merge_new_field_added(self):
        result = _merge_event_changed_payloads('{"name": ["A", "B"]}', {"address": ["X", "Y"]})
        assert result == {"name": ["A", "B"], "address": ["X", "Y"]}

    def test_merge_full_revert_returns_none(self):
        result = _merge_event_changed_payloads('{"name": ["A", "B"]}', {"name": ["B", "A"]})
        assert result is None

    def test_merge_partial_revert_keeps_other_fields(self):
        result = _merge_event_changed_payloads('{"name": ["A", "B"], "address": ["X", "Y"]}', {"name": ["B", "A"]})
        assert result == {"address": ["X", "Y"]}

    def test_merge_bool_round_trip(self):
        result = _merge_event_changed_payloads('{"paid": [false, true]}', {"paid": [True, False]})
        assert result is None  # reverted: False → True → False

    def test_merge_none_values(self):
        result = _merge_event_changed_payloads('{"master_event_id": [null, "3"]}', {"master_event_id": ["3", "5"]})
        assert result == {"master_event_id": [None, "5"]}


class TestEventChangedMerge:
    """Structured change_value + merge for event_changed."""

    def test_first_call_stores_field_edit_and_payload(self, app):
        """First call creates row with change_type=field_edit and JSON payload."""
        with app.app_context():
            user, event = _make_ed_event(delta_hours=72)
            send_event_changed(user, event, {"name": ["A", "B"]})
            db.session.commit()
            row = db.session.scalar(db.select(OutboxEmail).where(OutboxEmail.notification_type == "event_changed"))
            assert row is not None
            assert row.change_type == _EVENT_CHANGED_CHANGE_TYPE
            assert row.change_type == "field_edit"
            payload = json.loads(row.change_value)
            assert payload == {"name": ["A", "B"]}
            assert row.send_after is not None

    def test_two_edits_same_field_merge_endpoints(self, app):
        """Second edit to same field keeps earliest old, latest new."""
        with app.app_context():
            user, event = _make_ed_event(delta_hours=72)
            send_event_changed(user, event, {"name": ["A", "B"]})
            db.session.flush()
            send_event_changed(user, event, {"name": ["B", "C"]})
            db.session.commit()
            count = db.session.scalar(
                db.select(db.func.count(OutboxEmail.id)).where(
                    OutboxEmail.notification_type == "event_changed",
                    OutboxEmail.status == "pending",
                )
            )
            assert count == 1
            row = db.session.scalar(db.select(OutboxEmail).where(OutboxEmail.notification_type == "event_changed"))
            payload = json.loads(row.change_value)
            assert payload == {"name": ["A", "C"]}

    def test_two_edits_different_fields_both_kept(self, app):
        """Second edit to a different field adds it alongside the first."""
        with app.app_context():
            user, event = _make_ed_event(delta_hours=72)
            send_event_changed(user, event, {"name": ["A", "B"]})
            db.session.flush()
            send_event_changed(user, event, {"name": ["B", "C"], "address": ["X", "Y"]})
            db.session.commit()
            row = db.session.scalar(db.select(OutboxEmail).where(OutboxEmail.notification_type == "event_changed"))
            payload = json.loads(row.change_value)
            assert payload == {"address": ["X", "Y"], "name": ["A", "C"]}

    def test_full_revert_deletes_row(self, app):
        """Reverting all changed fields deletes the pending row."""
        with app.app_context():
            user, event = _make_ed_event(delta_hours=72)
            send_event_changed(user, event, {"name": ["A", "B"]})
            db.session.flush()
            send_event_changed(user, event, {"name": ["B", "A"]})
            db.session.commit()
            count = db.session.scalar(
                db.select(db.func.count(OutboxEmail.id)).where(
                    OutboxEmail.notification_type == "event_changed",
                    OutboxEmail.status == "pending",
                )
            )
            assert count == 0

    def test_partial_revert_keeps_other_fields(self, app):
        """Reverting one field keeps the other field's row."""
        with app.app_context():
            user, event = _make_ed_event(delta_hours=72)
            send_event_changed(user, event, {"name": ["A", "B"], "address": ["X", "Y"]})
            db.session.flush()
            send_event_changed(user, event, {"name": ["B", "A"]})
            db.session.commit()
            row = db.session.scalar(db.select(OutboxEmail).where(OutboxEmail.notification_type == "event_changed"))
            assert row is not None
            payload = json.loads(row.change_value)
            assert payload == {"address": ["X", "Y"]}

    def test_three_consecutive_edits_merge_to_endpoints(self, app):
        """Three chained edits collapse to earliest old, latest new."""
        with app.app_context():
            user, event = _make_ed_event(delta_hours=72)
            t0, t1, t2, t3 = "2025-01-01T10:00:00", "2025-01-01T11:00:00", "2025-01-01T12:00:00", "2025-01-01T13:00:00"
            send_event_changed(user, event, {"start_datetime": [t0, t1]})
            db.session.flush()
            send_event_changed(user, event, {"start_datetime": [t1, t2]})
            db.session.flush()
            send_event_changed(user, event, {"start_datetime": [t2, t3]})
            db.session.commit()
            count = db.session.scalar(
                db.select(db.func.count(OutboxEmail.id)).where(
                    OutboxEmail.notification_type == "event_changed",
                    OutboxEmail.status == "pending",
                )
            )
            assert count == 1
            row = db.session.scalar(db.select(OutboxEmail).where(OutboxEmail.notification_type == "event_changed"))
            payload = json.loads(row.change_value)
            assert payload == {"start_datetime": [t0, t3]}

    def test_null_change_value_row_upgraded_no_merge(self, app):
        """Existing row with change_value=NULL adopts incoming payload (no merge)."""
        with app.app_context():
            user, event = _make_ed_event(delta_hours=72)
            # Seed a legacy row with NULL change_value.
            row = OutboxEmail(
                to_email=user.email,
                subject="Old",
                body="body",
                html_body="<p>old</p>",
                notification_type="event_changed",
                user_id=user.id,
                event_id=event.id,
                change_value=None,
            )
            db.session.add(row)
            db.session.flush()
            send_event_changed(user, event, {"name": ["A", "B"]})
            db.session.commit()
            count = db.session.scalar(
                db.select(db.func.count(OutboxEmail.id)).where(
                    OutboxEmail.notification_type == "event_changed",
                    OutboxEmail.status == "pending",
                )
            )
            assert count == 1
            fetched = db.session.scalar(db.select(OutboxEmail).where(OutboxEmail.notification_type == "event_changed"))
            assert fetched.change_value is not None
            payload = json.loads(fetched.change_value)
            assert payload == {"name": ["A", "B"]}

    def test_immediate_flag_still_merges(self, app):
        """G._test_notification_immediate=True still triggers merge; send_after=NULL wins."""
        with app.app_context():
            user, event = _make_ed_event(delta_hours=72)
            send_event_changed(user, event, {"name": ["A", "B"]})
            db.session.flush()
            with app.test_request_context("/"):
                flask_g._test_notification_immediate = True
                send_event_changed(user, event, {"name": ["B", "C"]})
            db.session.commit()
            count = db.session.scalar(
                db.select(db.func.count(OutboxEmail.id)).where(
                    OutboxEmail.notification_type == "event_changed",
                    OutboxEmail.status == "pending",
                )
            )
            assert count == 1
            row = db.session.scalar(db.select(OutboxEmail).where(OutboxEmail.notification_type == "event_changed"))
            payload = json.loads(row.change_value)
            assert payload == {"name": ["A", "C"]}
            assert row.send_after is None

    def test_two_users_change_values_isolated(self, app):
        """Edits for different users create isolated rows."""
        with app.app_context():
            user1, event = _make_ed_event(delta_hours=72)
            role = db.session.scalar(db.select(Role).where(Role.name == "Member"))
            user2 = UserAccount(email="ec_user2@test.cz", name="EC User 2", is_active=True)
            user2.set_password("x")
            user2.roles = [role]
            db.session.add(user2)
            db.session.flush()
            send_event_changed(user1, event, {"name": ["A", "B"]})
            send_event_changed(user2, event, {"name": ["A", "Z"]})
            db.session.commit()
            rows = db.session.scalars(
                db.select(OutboxEmail).where(OutboxEmail.notification_type == "event_changed")
            ).all()
            assert len(rows) == 2
            payloads = {r.to_email: json.loads(r.change_value) for r in rows}
            assert payloads[user1.email] == {"name": ["A", "B"]}
            assert payloads[user2.email] == {"name": ["A", "Z"]}

    def test_two_events_change_values_isolated(self, app):
        """Edits for different events create isolated rows."""
        with app.app_context():
            user, event1 = _make_ed_event(delta_hours=72)
            me2 = MasterEvent(name="EC-ME2")
            db.session.add(me2)
            db.session.flush()
            start2 = datetime.now(timezone.utc) + timedelta(hours=72)
            event2 = Event(
                name="EC Event 2",
                master_event_id=me2.id,
                start_datetime=start2,
                end_datetime=start2 + timedelta(hours=2),
            )
            db.session.add(event2)
            db.session.flush()
            send_event_changed(user, event1, {"name": ["A", "B"]})
            send_event_changed(user, event2, {"name": ["X", "Y"]})
            db.session.commit()
            rows = db.session.scalars(
                db.select(OutboxEmail).where(OutboxEmail.notification_type == "event_changed")
            ).all()
            assert len(rows) == 2
            payloads = {r.event_id: json.loads(r.change_value) for r in rows}
            assert payloads[event1.id] == {"name": ["A", "B"]}
            assert payloads[event2.id] == {"name": ["X", "Y"]}

    def test_gate_off_produces_no_row(self, app):
        """Notify_event_changed=False → no row created."""
        with app.app_context():
            settings = get_settings()
            settings.notify_event_changed = False
            db.session.commit()

            user, event = _make_ed_event(delta_hours=72)
            send_event_changed(user, event, {"name": ["A", "B"]})
            db.session.commit()
            count = db.session.scalar(db.select(db.func.count(OutboxEmail.id)))
            assert count == 0


# ── batched drain + aggregated template ──────────────────────────────────────


def _make_batched_row(
    user: UserAccount,
    event: Event,
    notification_type: str,
    change_type: str | None = None,
    change_value: dict | None = None,
    send_after: datetime | None = None,
) -> OutboxEmail:
    row = OutboxEmail(
        to_email=user.email,
        subject=f"MedCover — {notification_type}",
        body="fallback",
        html_body=f"<p>legacy body for {notification_type}</p>",
        notification_type=notification_type,
        user_id=user.id,
        event_id=event.id,
        change_type=change_type,
        change_value=(
            json.dumps(change_value, ensure_ascii=False, sort_keys=True) if change_value is not None else None
        ),
        send_after=send_after,
    )
    db.session.add(row)
    db.session.flush()
    return row


class TestDrainBatchedOutbox:
    """Drain_batched_outbox() behaviour."""

    def test_matured_row_triggers_batch(self, app):
        """One matured pending row → drain sends it and returns True."""
        with app.app_context():
            user, event = _make_ed_event(delta_hours=72)
            past = datetime.now(timezone.utc) - timedelta(minutes=5)
            row = _make_batched_row(user, event, "event_published", send_after=past)
            db.session.commit()
            row_id = row.id
        with app.app_context():
            with patch("flask_mail.Mail.send") as mock_send:
                result = drain_batched_outbox()
        assert result is True
        mock_send.assert_called_once()
        with app.app_context():
            r = db.session.get(OutboxEmail, row_id)
            assert r.status == "sent"

    def test_immature_rows_join_batch(self, app):
        """One matured + one immature row → both sent in one call."""
        with app.app_context():
            user, event1 = _make_ed_event(delta_hours=72)
            me2 = MasterEvent(name="B5-ME2")
            db.session.add(me2)
            db.session.flush()
            start2 = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)
            event2 = Event(
                name="B5 Event 2",
                master_event_id=me2.id,
                start_datetime=start2,
                end_datetime=start2 + timedelta(hours=2),
            )
            db.session.add(event2)
            db.session.flush()
            past = datetime.now(timezone.utc) - timedelta(minutes=1)
            future = datetime.now(timezone.utc) + timedelta(hours=2)
            r1 = _make_batched_row(user, event1, "event_published", send_after=past)
            r2 = _make_batched_row(user, event2, "event_cancelled", send_after=future)
            db.session.commit()
            ids = (r1.id, r2.id)
        html_captured: list[str] = []

        def capture_send(msg):
            html_captured.append(msg.html or "")

        with app.app_context():
            with patch("flask_mail.Mail.send", side_effect=capture_send):
                result = drain_batched_outbox()
        assert result is True
        assert len(html_captured) == 1
        assert "B5 Event 2" in html_captured[0] or "ED Event" in html_captured[0]
        with app.app_context():
            for rid in ids:
                assert db.session.get(OutboxEmail, rid).status == "sent"

    def test_only_immature_no_trigger(self, app):
        """Only immature rows → drain returns False, row stays pending."""
        with app.app_context():
            user, event = _make_ed_event(delta_hours=72)
            future = datetime.now(timezone.utc) + timedelta(hours=2)
            row = _make_batched_row(user, event, "event_published", send_after=future)
            db.session.commit()
            row_id = row.id
        with app.app_context():
            with patch("flask_mail.Mail.send") as mock_send:
                result = drain_batched_outbox()
        assert result is False
        mock_send.assert_not_called()
        with app.app_context():
            assert db.session.get(OutboxEmail, row_id).status == "pending"

    def test_empty_queue_returns_false(self, app):
        """Empty outbox → drain returns False."""
        with app.app_context():
            result = drain_batched_outbox()
        assert result is False

    def test_multiple_events_two_sections(self, app):
        """Rows for two events → both event names in the rendered HTML."""
        with app.app_context():
            user, event1 = _make_ed_event(delta_hours=72)
            event1.name = "Akce Jedna"
            me2 = MasterEvent(name="B5-ME3")
            db.session.add(me2)
            db.session.flush()
            start2 = datetime(2026, 9, 15, 9, 0, tzinfo=timezone.utc)
            event2 = Event(
                name="Akce Dvě",
                master_event_id=me2.id,
                start_datetime=start2,
                end_datetime=start2 + timedelta(hours=2),
            )
            db.session.add(event2)
            db.session.flush()
            past = datetime.now(timezone.utc) - timedelta(minutes=1)
            _make_batched_row(user, event1, "event_cancelled", send_after=past)
            _make_batched_row(user, event2, "event_published", send_after=past)
            db.session.commit()
        html_captured: list[str] = []

        def capture_send(msg):
            html_captured.append(msg.html or "")

        with app.app_context():
            with patch("flask_mail.Mail.send", side_effect=capture_send):
                drain_batched_outbox()
        assert len(html_captured) == 1
        assert "Akce Jedna" in html_captured[0]
        assert "Akce Dvě" in html_captured[0]

    def test_subject_single_event(self, app):
        """One event section → subject includes event name."""
        with app.app_context():
            user, event = _make_ed_event(delta_hours=72)
            event.name = "Plavecký závod"
            past = datetime.now(timezone.utc) - timedelta(minutes=1)
            _make_batched_row(user, event, "event_published", send_after=past)
            db.session.commit()
        msgs_captured: list = []

        def capture_send(msg):
            msgs_captured.append(msg)

        with app.app_context():
            with patch("flask_mail.Mail.send", side_effect=capture_send):
                drain_batched_outbox()
        assert len(msgs_captured) == 1
        assert msgs_captured[0].subject == "MedCover — Změny v akci: Plavecký závod"

    def test_subject_multiple_events(self, app):
        """Two event sections → subject is the N-event summary format."""
        with app.app_context():
            user, event1 = _make_ed_event(delta_hours=72)
            me2 = MasterEvent(name="B5-ME4")
            db.session.add(me2)
            db.session.flush()
            start2 = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
            event2 = Event(
                name="Akce B",
                master_event_id=me2.id,
                start_datetime=start2,
                end_datetime=start2 + timedelta(hours=2),
            )
            db.session.add(event2)
            db.session.flush()
            past = datetime.now(timezone.utc) - timedelta(minutes=1)
            _make_batched_row(user, event1, "event_published", send_after=past)
            _make_batched_row(user, event2, "event_cancelled", send_after=past)
            db.session.commit()
        msgs_captured: list = []

        def capture_send(msg):
            msgs_captured.append(msg)

        with app.app_context():
            with patch("flask_mail.Mail.send", side_effect=capture_send):
                drain_batched_outbox()
        assert len(msgs_captured) == 1
        assert msgs_captured[0].subject == "MedCover — Souhrn změn (2 akcí)"

    def test_event_changed_diff_table_rendered(self, app):
        """Event_changed row with field_edit → HTML contains field label + old/new values."""
        with app.app_context():
            user, event = _make_ed_event(delta_hours=72)
            past = datetime.now(timezone.utc) - timedelta(minutes=1)
            _make_batched_row(
                user,
                event,
                "event_changed",
                change_type="field_edit",
                change_value={"name": ["Stará akce", "Nová akce"]},
                send_after=past,
            )
            db.session.commit()
        html_captured: list[str] = []

        def capture_send(msg):
            html_captured.append(msg.html or "")

        with app.app_context():
            with patch("flask_mail.Mail.send", side_effect=capture_send):
                drain_batched_outbox()
        assert len(html_captured) == 1
        assert "Název akce" in html_captured[0] or "name" in html_captured[0]
        assert "Stará akce" in html_captured[0]
        assert "Nová akce" in html_captured[0]

    def test_assignment_confirmed_spot_description(self, app):
        """Assignment_confirmed row → HTML contains confirmation sentence with spot name."""
        with app.app_context():
            user, event = _make_ed_event(delta_hours=72)
            past = datetime.now(timezone.utc) - timedelta(minutes=1)
            _make_batched_row(
                user,
                event,
                "assignment_confirmed",
                change_type="assignment",
                change_value={"action": "confirmed", "spot_description": "Záchranář"},
                send_after=past,
            )
            db.session.commit()
        html_captured: list[str] = []

        def capture_send(msg):
            html_captured.append(msg.html or "")

        with app.app_context():
            with patch("flask_mail.Mail.send", side_effect=capture_send):
                drain_batched_outbox()
        assert "Záchranář" in html_captured[0]
        assert "přihlášeni" in html_captured[0]

    def test_assignment_released_spot_description(self, app):
        """Assignment_released row → HTML contains release sentence with spot name."""
        with app.app_context():
            user, event = _make_ed_event(delta_hours=72)
            past = datetime.now(timezone.utc) - timedelta(minutes=1)
            _make_batched_row(
                user,
                event,
                "assignment_released",
                change_type="assignment",
                change_value={"action": "released", "spot_description": "Řidič"},
                send_after=past,
            )
            db.session.commit()
        html_captured: list[str] = []

        def capture_send(msg):
            html_captured.append(msg.html or "")

        with app.app_context():
            with patch("flask_mail.Mail.send", side_effect=capture_send):
                drain_batched_outbox()
        assert "Řidič" in html_captured[0]
        assert "odhlášeni" in html_captured[0]

    def test_unfilled_reminder_count(self, app):
        """Unfilled_reminder row → HTML contains count and Czech word."""
        with app.app_context():
            user, event = _make_ed_event(delta_hours=72)
            past = datetime.now(timezone.utc) - timedelta(minutes=1)
            _make_batched_row(
                user,
                event,
                "unfilled_reminder",
                change_type="unfilled_reminder",
                change_value={"unfilled_count": 3},
                send_after=past,
            )
            db.session.commit()
        html_captured: list[str] = []

        def capture_send(msg):
            html_captured.append(msg.html or "")

        with app.app_context():
            with patch("flask_mail.Mail.send", side_effect=capture_send):
                drain_batched_outbox()
        assert "3" in html_captured[0]
        assert "neobsazen" in html_captured[0]

    def test_debriefing_invitation_link(self, app):
        """Debriefing_invitation row → HTML contains a debriefing link."""
        with app.app_context():
            user, event = _make_ed_event(delta_hours=72)
            past = datetime.now(timezone.utc) - timedelta(minutes=1)
            _make_batched_row(
                user,
                event,
                "debriefing_invitation",
                change_type="debriefing",
                change_value={"assignment_id": 42},
                send_after=past,
            )
            db.session.commit()
        html_captured: list[str] = []

        def capture_send(msg):
            html_captured.append(msg.html or "")

        with app.app_context():
            with patch("flask_mail.Mail.send", side_effect=capture_send):
                drain_batched_outbox()
        assert "debriefing" in html_captured[0].lower() or "42" in html_captured[0]

    def test_deleted_event_row_dropped(self, app):
        """Row with event_id=None + live row → dead row deleted, live row sent, email sent."""
        with app.app_context():
            user, event = _make_ed_event(delta_hours=72)
            past = datetime.now(timezone.utc) - timedelta(minutes=1)
            dead_row = OutboxEmail(
                to_email=user.email,
                subject="MedCover — event_published",
                body="fallback",
                notification_type="event_published",
                user_id=user.id,
                event_id=None,
                send_after=past,
            )
            db.session.add(dead_row)
            live_row = _make_batched_row(user, event, "event_published", send_after=past)
            db.session.commit()
            dead_id, live_id = dead_row.id, live_row.id
        with app.app_context():
            with patch("flask_mail.Mail.send") as mock_send:
                result = drain_batched_outbox()
        assert result is True
        mock_send.assert_called_once()
        with app.app_context():
            assert db.session.get(OutboxEmail, dead_id) is None
            live = db.session.get(OutboxEmail, live_id)
            assert live.status == "sent"

    def test_deleted_event_only_no_email(self, app):
        """Only a NULL-event row → row deleted, no email sent, drain returns True."""
        with app.app_context():
            user, event = _make_ed_event(delta_hours=72)
            past = datetime.now(timezone.utc) - timedelta(minutes=1)
            dead_row = OutboxEmail(
                to_email=user.email,
                subject="MedCover — event_published",
                body="fallback",
                notification_type="event_published",
                user_id=user.id,
                event_id=None,
                send_after=past,
            )
            db.session.add(dead_row)
            db.session.commit()
            dead_id = dead_row.id
        with app.app_context():
            with patch("flask_mail.Mail.send") as mock_send:
                result = drain_batched_outbox()
        assert result is True
        mock_send.assert_not_called()
        with app.app_context():
            assert db.session.get(OutboxEmail, dead_id) is None

    def test_smtp_failure_batch_retry(self, app):
        """SMTP raises → both rows stay pending with retry_count=1, one AuditLogEntry."""
        with app.app_context():
            user, event = _make_ed_event(delta_hours=72)
            past = datetime.now(timezone.utc) - timedelta(minutes=1)
            r1 = _make_batched_row(user, event, "event_published", send_after=past)
            r2 = _make_batched_row(user, event, "event_cancelled", send_after=past)
            db.session.commit()
            ids = (r1.id, r2.id)
        with app.app_context():
            with patch("flask_mail.Mail.send", side_effect=Exception("SMTP down")):
                result = drain_batched_outbox()
        assert result is True
        with app.app_context():
            for rid in ids:
                r = db.session.get(OutboxEmail, rid)
                assert r.status == "pending"
                assert r.retry_count == 1
            audit_count = db.session.scalar(
                db.select(db.func.count(AuditLogEntry.id)).where(AuditLogEntry.action_type == "email_failed")
            )
            assert audit_count == 1

    def test_smtp_permanent_failure_all_failed(self, app):
        """Rows at MAX_RETRIES-1 → after SMTP failure, both status='failed', one audit."""
        max_r = OutboxEmail.MAX_RETRIES
        with app.app_context():
            user, event = _make_ed_event(delta_hours=72)
            past = datetime.now(timezone.utc) - timedelta(minutes=1)
            r1 = _make_batched_row(user, event, "event_published", send_after=past)
            r2 = _make_batched_row(user, event, "event_cancelled", send_after=past)
            r1.retry_count = max_r - 1
            r2.retry_count = max_r - 1
            db.session.commit()
            ids = [r1.id, r2.id]
        with app.app_context():
            with patch("flask_mail.Mail.send", side_effect=Exception("permanent")):
                drain_batched_outbox()
        with app.app_context():
            for rid in ids:
                assert db.session.get(OutboxEmail, rid).status == "failed"
            audit = db.session.scalar(db.select(AuditLogEntry).where(AuditLogEntry.action_type == "email_failed"))
            assert audit is not None
            cj = json.loads(audit.changes_json) if isinstance(audit.changes_json, str) else audit.changes_json
            assert set(ids).issubset(set(cj["row_ids"]))

    def test_dev_email_block_batch_skipped(self, app):
        """Dev_email_block blocks recipient → both rows skipped, no send."""
        with app.app_context():
            s = get_settings()
            s.dev_email_block = True
            s.dev_email_allowlist = "other@example.com"
            db.session.commit()
        with app.app_context():
            user, event = _make_ed_event(delta_hours=72)
            past = datetime.now(timezone.utc) - timedelta(minutes=1)
            r1 = _make_batched_row(user, event, "event_published", send_after=past)
            r2 = _make_batched_row(user, event, "event_cancelled", send_after=past)
            db.session.commit()
            ids = (r1.id, r2.id)
        with app.app_context():
            with patch("flask_mail.Mail.send") as mock_send:
                result = drain_batched_outbox()
        mock_send.assert_not_called()
        assert result is True
        with app.app_context():
            for rid in ids:
                r = db.session.get(OutboxEmail, rid)
                assert r.status == "skipped"
                assert "dev_email_block" in r.last_error
            # reset
            s = get_settings()
            s.dev_email_block = False
            db.session.commit()

    def test_one_user_per_tick(self, app):
        """Two users → exactly one email per drain call, other user's rows still pending."""
        with app.app_context():
            role = db.session.scalar(db.select(Role).where(Role.name == "Member"))
            userA = UserAccount(email="userA_b5@test.cz", name="User A", is_active=True)
            userA.set_password("x")
            userA.roles = [role]
            userB = UserAccount(email="userB_b5@test.cz", name="User B", is_active=True)
            userB.set_password("x")
            userB.roles = [role]
            db.session.add_all([userA, userB])
            db.session.flush()

            me = MasterEvent(name="B5-ME-tick")
            db.session.add(me)
            db.session.flush()
            start = datetime(2026, 8, 1, 9, 0, tzinfo=timezone.utc)
            event = Event(
                name="Tick Event",
                master_event_id=me.id,
                start_datetime=start,
                end_datetime=start + timedelta(hours=2),
            )
            db.session.add(event)
            db.session.flush()

            past = datetime.now(timezone.utc) - timedelta(minutes=1)
            _make_batched_row(userA, event, "event_published", send_after=past)
            _make_batched_row(userB, event, "event_cancelled", send_after=past)
            db.session.commit()

        send_count = [0]

        def capture_send(msg):
            send_count[0] += 1

        with app.app_context():
            with patch("flask_mail.Mail.send", side_effect=capture_send):
                drain_batched_outbox()
        assert send_count[0] == 1

        with app.app_context():
            pending_rows = db.session.scalars(
                db.select(OutboxEmail).where(
                    OutboxEmail.status == "pending",
                    OutboxEmail.notification_type.in_(["event_published", "event_cancelled"]),
                )
            ).all()
            assert len(pending_rows) == 1

    def test_missing_change_value_graceful(self, app):
        """Assignment_confirmed with change_value=None → row sent, no crash."""
        with app.app_context():
            user, event = _make_ed_event(delta_hours=72)
            past = datetime.now(timezone.utc) - timedelta(minutes=1)
            row = _make_batched_row(
                user,
                event,
                "assignment_confirmed",
                change_type="assignment",
                change_value=None,
                send_after=past,
            )
            db.session.commit()
            row_id = row.id
        with app.app_context():
            with patch("flask_mail.Mail.send") as mock_send:
                result = drain_batched_outbox()
        assert result is True
        mock_send.assert_called_once()
        with app.app_context():
            assert db.session.get(OutboxEmail, row_id).status == "sent"

    def test_event_sections_sorted_by_start_datetime(self, app):
        """Two events — earlier start_datetime section appears first in HTML."""
        with app.app_context():
            user, _ = _make_ed_event(delta_hours=72)
            me2 = MasterEvent(name="B5-sort-ME")
            db.session.add(me2)
            db.session.flush()

            start_later = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)
            start_earlier = datetime(2026, 8, 1, 9, 0, tzinfo=timezone.utc)

            event_later = Event(
                name="Later Event",
                master_event_id=me2.id,
                start_datetime=start_later,
                end_datetime=start_later + timedelta(hours=2),
            )
            event_earlier = Event(
                name="Earlier Event",
                master_event_id=me2.id,
                start_datetime=start_earlier,
                end_datetime=start_earlier + timedelta(hours=2),
            )
            db.session.add_all([event_later, event_earlier])
            db.session.flush()

            past = datetime.now(timezone.utc) - timedelta(minutes=1)
            _make_batched_row(user, event_later, "event_published", send_after=past)
            _make_batched_row(user, event_earlier, "event_cancelled", send_after=past)
            db.session.commit()

        html_captured: list[str] = []

        def capture_send(msg):
            html_captured.append(msg.html or "")

        with app.app_context():
            with patch("flask_mail.Mail.send", side_effect=capture_send):
                drain_batched_outbox()
        assert len(html_captured) == 1
        html = html_captured[0]
        idx_earlier = html.index("Earlier Event")
        idx_later = html.index("Later Event")
        assert idx_earlier < idx_later, "Earlier-start event section must appear before later-start event section"


def _add_spot(
    event: Event,
    description: str,
    qualifications: list[Qualification] | None = None,
    is_optional: bool = False,
) -> EventSpot:
    spot = EventSpot(event_id=event.id, description=description, is_optional=is_optional)
    if qualifications:
        spot.required_qualifications = qualifications
    db.session.add(spot)
    db.session.flush()
    return spot


class TestBatchedSpotQualificationsInEmail:
    """event_published / assignments_opened emails must list each spot's
    description, required qualifications, and current fill state so recipients
    can decide whether to open the app."""

    def test_event_published_lists_spots_with_qualifications(self, app):
        with app.app_context():
            user, event = _make_ed_event(delta_hours=72)
            q_med = Qualification(name="Lékař")
            q_drv = Qualification(name="Řidič")
            db.session.add_all([q_med, q_drv])
            db.session.flush()
            user.qualifications = [q_med, q_drv]
            _add_spot(event, "Stanoviště 1", [q_med, q_drv])
            _add_spot(event, "Doprava", [q_drv])
            _add_spot(event, "Volný pomocník", [])
            past = datetime.now(timezone.utc) - timedelta(minutes=1)
            _make_batched_row(user, event, "event_published", send_after=past)
            db.session.commit()

        html_captured: list[str] = []
        with app.app_context():
            with patch("flask_mail.Mail.send", side_effect=lambda m: html_captured.append(m.html or "")):
                drain_batched_outbox()

        html = html_captured[0]
        assert "Stanoviště 1" in html
        assert "Doprava" in html
        assert "Volný pomocník" in html
        assert "Lékař" in html
        assert "Řidič" in html
        assert "Bez požadavků" in html
        assert "Volné" in html
        assert "Seznam pozic, pro které splňujete požadavky:" in html

    def test_assignments_opened_lists_spots_with_qualifications(self, app):
        with app.app_context():
            user, event = _make_ed_event(delta_hours=72)
            q_med = Qualification(name="Lékař")
            db.session.add(q_med)
            db.session.flush()
            user.qualifications = [q_med]
            _add_spot(event, "Stanoviště A", [q_med])
            past = datetime.now(timezone.utc) - timedelta(minutes=1)
            _make_batched_row(user, event, "assignments_opened", send_after=past)
            db.session.commit()

        html_captured: list[str] = []
        with app.app_context():
            with patch("flask_mail.Mail.send", side_effect=lambda m: html_captured.append(m.html or "")):
                drain_batched_outbox()

        html = html_captured[0]
        assert "Stanoviště A" in html
        assert "Lékař" in html
        assert "Požadovaná kvalifikace" in html

    def test_deleted_qualification_hidden_from_email(self, app):
        with app.app_context():
            user, event = _make_ed_event(delta_hours=72)
            q_alive = Qualification(name="Aktivní")
            q_dead = Qualification(name="Zrušená", is_deleted=True)
            db.session.add_all([q_alive, q_dead])
            db.session.flush()
            user.qualifications = [q_alive]
            _add_spot(event, "Pozice X", [q_alive, q_dead])
            past = datetime.now(timezone.utc) - timedelta(minutes=1)
            _make_batched_row(user, event, "event_published", send_after=past)
            db.session.commit()

        html_captured: list[str] = []
        with app.app_context():
            with patch("flask_mail.Mail.send", side_effect=lambda m: html_captured.append(m.html or "")):
                drain_batched_outbox()

        html = html_captured[0]
        assert "Aktivní" in html
        assert "Zrušená" not in html

    def test_optional_spot_marked_in_email(self, app):
        with app.app_context():
            user, event = _make_ed_event(delta_hours=72)
            _add_spot(event, "Rezerva", [], is_optional=True)
            past = datetime.now(timezone.utc) - timedelta(minutes=1)
            _make_batched_row(user, event, "event_published", send_after=past)
            db.session.commit()

        html_captured: list[str] = []
        with app.app_context():
            with patch("flask_mail.Mail.send", side_effect=lambda m: html_captured.append(m.html or "")):
                drain_batched_outbox()

        assert "(volitelná)" in html_captured[0]

    def test_filled_spot_shown_as_obsazeno(self, app):
        with app.app_context():
            user, event = _make_ed_event(delta_hours=72)
            spot = _add_spot(event, "Zapln\u011bn\u00e1 pozice", [])
            other = UserAccount(email="filler@test.cz", name="Filler", is_active=True)
            other.set_password("x")
            other.roles = [db.session.scalar(db.select(Role).where(Role.name == "Member"))]
            db.session.add(other)
            db.session.flush()
            db.session.add(Assignment(spot_id=spot.id, user_id=other.id))
            past = datetime.now(timezone.utc) - timedelta(minutes=1)
            _make_batched_row(user, event, "event_published", send_after=past)
            db.session.commit()

        html_captured: list[str] = []
        with app.app_context():
            with patch("flask_mail.Mail.send", side_effect=lambda m: html_captured.append(m.html or "")):
                drain_batched_outbox()

        assert "Obsazeno" in html_captured[0]

    def test_event_without_spots_omits_spot_table(self, app):
        with app.app_context():
            user, event = _make_ed_event(delta_hours=72)
            past = datetime.now(timezone.utc) - timedelta(minutes=1)
            _make_batched_row(user, event, "event_published", send_after=past)
            db.session.commit()

        html_captured: list[str] = []
        with app.app_context():
            with patch("flask_mail.Mail.send", side_effect=lambda m: html_captured.append(m.html or "")):
                drain_batched_outbox()

        html = html_captured[0]
        assert "Požadovaná kvalifikace" not in html
        assert "Seznam pozic" not in html
        assert "Nesplňujete" not in html
        assert "Akce byla zveřejněna." in html

    def test_published_and_assignments_opened_render_spot_table_once(self, app):
        with app.app_context():
            user, event = _make_ed_event(delta_hours=72)
            q = Qualification(name="Sanitář")
            db.session.add(q)
            db.session.flush()
            user.qualifications = [q]
            _add_spot(event, "Jediná pozice", [q])
            past = datetime.now(timezone.utc) - timedelta(minutes=1)
            _make_batched_row(user, event, "event_published", send_after=past)
            _make_batched_row(user, event, "assignments_opened", send_after=past)
            db.session.commit()

        html_captured: list[str] = []
        with app.app_context():
            with patch("flask_mail.Mail.send", side_effect=lambda m: html_captured.append(m.html or "")):
                drain_batched_outbox()

        html = html_captured[0]
        assert html.count("Jediná pozice") == 1
        assert html.count("Požadovaná kvalifikace") == 1
        assert html.count("Seznam pozic, pro které splňujete požadavky:") == 1


class TestBatchedEligibleSpotsOnly:
    """event_published / assignments_opened must show only the spots the
    recipient is qualified for, and add a preamble explaining the list.
    An event with spots but none matching the user's qualifications must
    render an explicit \"not eligible\" message instead of an empty table."""

    def test_only_eligible_spots_listed(self, app):
        with app.app_context():
            user, event = _make_ed_event(delta_hours=72)
            q_med = Qualification(name="Lékař")
            q_drv = Qualification(name="Řidič")
            db.session.add_all([q_med, q_drv])
            db.session.flush()
            user.qualifications = [q_drv]
            _add_spot(event, "Pouze pro lékaře", [q_med])
            _add_spot(event, "Pouze pro řidiče", [q_drv])
            _add_spot(event, "Kdokoli", [])
            past = datetime.now(timezone.utc) - timedelta(minutes=1)
            _make_batched_row(user, event, "event_published", send_after=past)
            db.session.commit()

        html_captured: list[str] = []
        with app.app_context():
            with patch("flask_mail.Mail.send", side_effect=lambda m: html_captured.append(m.html or "")):
                drain_batched_outbox()

        html = html_captured[0]
        assert "Pouze pro řidiče" in html
        assert "Kdokoli" in html
        assert "Pouze pro lékaře" not in html
        assert "Seznam pozic, pro které splňujete požadavky:" in html
        assert "Nesplňujete" not in html

    def test_user_qualifies_for_none_shows_fallback_message(self, app):
        with app.app_context():
            user, event = _make_ed_event(delta_hours=72)
            q_med = Qualification(name="Lékař")
            db.session.add(q_med)
            db.session.flush()
            # user has no qualifications
            _add_spot(event, "Pouze pro lékaře", [q_med])
            past = datetime.now(timezone.utc) - timedelta(minutes=1)
            _make_batched_row(user, event, "event_published", send_after=past)
            db.session.commit()

        html_captured: list[str] = []
        with app.app_context():
            with patch("flask_mail.Mail.send", side_effect=lambda m: html_captured.append(m.html or "")):
                drain_batched_outbox()

        html = html_captured[0]
        assert "Nesplňujete požadavky žádné z pozic této akce." in html
        assert "Seznam pozic" not in html
        assert "Požadovaná kvalifikace" not in html
        assert "Pouze pro lékaře" not in html

    def test_qualification_hierarchy_respected(self, app):
        """A user holding a parent (substitute) qualification is eligible for
        spots requiring the child. Verified via user_fillable_qual_ids."""
        with app.app_context():
            user, event = _make_ed_event(delta_hours=72)
            q_first_aider = Qualification(name="První pomoc")
            q_doctor = Qualification(name="Lékař")
            db.session.add_all([q_first_aider, q_doctor])
            db.session.flush()
            # Doctor is a parent of First Aider → doctor can fill first-aider spots.
            q_first_aider.parents = [q_doctor]
            user.qualifications = [q_doctor]
            _add_spot(event, "Pomocník první pomoci", [q_first_aider])
            past = datetime.now(timezone.utc) - timedelta(minutes=1)
            _make_batched_row(user, event, "event_published", send_after=past)
            db.session.commit()

        html_captured: list[str] = []
        with app.app_context():
            with patch("flask_mail.Mail.send", side_effect=lambda m: html_captured.append(m.html or "")):
                drain_batched_outbox()

        html = html_captured[0]
        assert "Pomocník první pomoci" in html
        assert "Seznam pozic, pro které splňujete požadavky:" in html


class TestBatchedEventDatetimeRange:
    """Every event section header must show the start–end window in local
    time so recipients can gauge availability without opening the app."""

    def test_datetime_range_rendered_in_section_header(self, app):
        with app.app_context():
            user, event = _make_ed_event(delta_hours=72)
            event.start_datetime = datetime(2026, 8, 1, 8, 0, tzinfo=timezone.utc)
            event.end_datetime = datetime(2026, 8, 1, 18, 30, tzinfo=timezone.utc)
            past = datetime.now(timezone.utc) - timedelta(minutes=1)
            _make_batched_row(user, event, "event_published", send_after=past)
            db.session.commit()

        html_captured: list[str] = []
        with app.app_context():
            with patch("flask_mail.Mail.send", side_effect=lambda m: html_captured.append(m.html or "")):
                drain_batched_outbox()

        html = html_captured[0]
        # Local time is UTC+2 in August (CEST) in the default app TZ (Europe/Prague).
        assert "01.08.2026 10:00 – 01.08.2026 20:30" in html

    def test_datetime_range_spans_multiple_days(self, app):
        with app.app_context():
            user, event = _make_ed_event(delta_hours=72)
            event.start_datetime = datetime(2026, 8, 1, 8, 0, tzinfo=timezone.utc)
            event.end_datetime = datetime(2026, 8, 3, 16, 0, tzinfo=timezone.utc)
            past = datetime.now(timezone.utc) - timedelta(minutes=1)
            _make_batched_row(user, event, "event_published", send_after=past)
            db.session.commit()

        html_captured: list[str] = []
        with app.app_context():
            with patch("flask_mail.Mail.send", side_effect=lambda m: html_captured.append(m.html or "")):
                drain_batched_outbox()

        assert "01.08.2026 10:00 – 03.08.2026 18:00" in html_captured[0]


class TestUnfilledReminderSpotTable:
    """unfilled_reminder must list the individual unfilled mandatory spots
    with the same table shape used by event_published, so the coordinator
    knows what's still open without opening the app."""

    def test_unfilled_reminder_lists_only_unfilled_mandatory_spots(self, app):
        with app.app_context():
            user, event = _make_ed_event(delta_hours=72)
            q_med = Qualification(name="Lékař")
            q_drv = Qualification(name="Řidič")
            db.session.add_all([q_med, q_drv])
            db.session.flush()
            # unfilled + mandatory → shown
            _add_spot(event, "Volná pozice", [q_med])
            _add_spot(event, "Volná doprava", [q_drv])
            # filled mandatory → hidden
            filled = _add_spot(event, "Obsazená pozice", [q_med])
            other = UserAccount(email="filler2@test.cz", name="Filler", is_active=True)
            other.set_password("x")
            other.roles = [db.session.scalar(db.select(Role).where(Role.name == "Member"))]
            db.session.add(other)
            db.session.flush()
            db.session.add(Assignment(spot_id=filled.id, user_id=other.id))
            # optional → hidden even if empty
            _add_spot(event, "Volitelná rezerva", [], is_optional=True)
            past = datetime.now(timezone.utc) - timedelta(minutes=1)
            _make_batched_row(
                user,
                event,
                "unfilled_reminder",
                change_type="unfilled_reminder",
                change_value={"unfilled_count": 2},
                send_after=past,
            )
            db.session.commit()

        html_captured: list[str] = []
        with app.app_context():
            with patch("flask_mail.Mail.send", side_effect=lambda m: html_captured.append(m.html or "")):
                drain_batched_outbox()

        html = html_captured[0]
        assert "Volná pozice" in html
        assert "Volná doprava" in html
        assert "Lékař" in html
        assert "Řidič" in html
        assert "Obsazená pozice" not in html
        assert "Volitelná rezerva" not in html
        # Table header rendered too
        assert "Požadovaná kvalifikace" in html

    def test_unfilled_reminder_all_filled_shows_reassurance(self, app):
        with app.app_context():
            user, event = _make_ed_event(delta_hours=72)
            spot = _add_spot(event, "Jediná pozice", [])
            other = UserAccount(email="filler3@test.cz", name="Filler", is_active=True)
            other.set_password("x")
            other.roles = [db.session.scalar(db.select(Role).where(Role.name == "Member"))]
            db.session.add(other)
            db.session.flush()
            db.session.add(Assignment(spot_id=spot.id, user_id=other.id))
            past = datetime.now(timezone.utc) - timedelta(minutes=1)
            _make_batched_row(
                user,
                event,
                "unfilled_reminder",
                change_type="unfilled_reminder",
                change_value={"unfilled_count": 0},
                send_after=past,
            )
            db.session.commit()

        html_captured: list[str] = []
        with app.app_context():
            with patch("flask_mail.Mail.send", side_effect=lambda m: html_captured.append(m.html or "")):
                drain_batched_outbox()

        html = html_captured[0]
        assert "Všechna povinná místa jsou nyní obsazená." in html
        assert "Požadovaná kvalifikace" not in html


class TestBatchKeyIncludesToEmail:
    """Regression: batches must be keyed on (user_id, to_email), not user_id alone.

    The admin test-notification form can set g._test_notification_email so a row
    is stored with user_id=<admin> but to_email=<tester@example.com>. If the
    batched drain groups on user_id alone, that row's tester address would be
    reused as the recipient for the admin's own real notifications (or vice
    versa). Each distinct to_email must produce its own batch.
    """

    def test_rows_with_different_to_email_do_not_batch(self, app):
        with app.app_context():
            user, event = _make_ed_event(delta_hours=72)
            past = datetime.now(timezone.utc) - timedelta(minutes=1)
            # Row 1: normal notification for the admin — real address.
            row_admin = _make_batched_row(user, event, "event_published", send_after=past)
            # Row 2: same user_id, but redirected to a tester address (mimics the
            # test-form's g._test_notification_email override).
            row_test = OutboxEmail(
                to_email="tester@example.com",
                subject="MedCover — test",
                body="fallback",
                html_body="<p>test</p>",
                notification_type="assignment_confirmed",
                user_id=user.id,
                event_id=event.id,
                send_after=past,
            )
            db.session.add(row_test)
            db.session.commit()
            admin_email = user.email
            row_admin_id = row_admin.id
            row_test_id = row_test.id

        seen_recipients: list[str] = []

        def capture_send(msg):
            seen_recipients.extend(msg.recipients)

        with app.app_context():
            with patch("flask_mail.Mail.send", side_effect=capture_send):
                for _ in range(5):
                    if not drain_batched_outbox():
                        break

        assert sorted(seen_recipients) == sorted([admin_email, "tester@example.com"])
        with app.app_context():
            assert db.session.get(OutboxEmail, row_admin_id).status == "sent"
            assert db.session.get(OutboxEmail, row_test_id).status == "sent"


class TestLegacyDrainGuard:
    """Drain_one_outbox_email ignores batched rows; drain_batched_outbox ignores NULL-user rows.."""

    def test_batched_drain_ignores_user_null_rows(self, app):
        """Row with user_id=None → drain_batched_outbox returns False."""
        with app.app_context():
            row = OutboxEmail(to_email="legacy@test.cz", subject="S", body="B")
            db.session.add(row)
            db.session.commit()
            row_id = row.id
        with app.app_context():
            result = drain_batched_outbox()
        assert result is False
        with app.app_context():
            assert db.session.get(OutboxEmail, row_id).status == "pending"

    def test_legacy_drain_handles_user_null_row(self, app):
        """Row with user_id=None → drain_one_outbox_email sends it."""
        with app.app_context():
            row = OutboxEmail(to_email="legacy2@test.cz", subject="S2", body="B2")
            db.session.add(row)
            db.session.commit()
            row_id = row.id
        with app.app_context():
            with patch("flask_mail.Mail.send"):
                drain_one_outbox_email()
        with app.app_context():
            assert db.session.get(OutboxEmail, row_id).status == "sent"

    def test_legacy_drain_ignores_batched_rows(self, app):
        """Row with user_id set → drain_one_outbox_email leaves it pending."""
        with app.app_context():
            user, event = _make_ed_event(delta_hours=72)
            row = _make_batched_row(user, event, "event_published")
            db.session.commit()
            row_id = row.id
        with app.app_context():
            with patch("flask_mail.Mail.send") as mock_send:
                result = drain_one_outbox_email()
        mock_send.assert_not_called()
        assert result is False
        with app.app_context():
            assert db.session.get(OutboxEmail, row_id).status == "pending"


class TestBatchedPayloadCallers:
    """Send_* helpers store structured change_value in the row.."""

    def test_send_assignment_confirmed_stores_change_value(self, app):
        """Confirmed row has change_type='assignment' and action/spot_description."""
        with app.app_context():
            event = _make_event("Payload Test Event")
            user = _make_member_user("payload_a@test.cz", "Payload User A")
            send_assignment_confirmed(user, event, spot_description="Záchranář")
            db.session.commit()
            row = db.session.scalar(
                db.select(OutboxEmail).where(OutboxEmail.notification_type == "assignment_confirmed")
            )
            assert row is not None
            assert row.change_type == "assignment"
            payload = json.loads(row.change_value)
            assert payload == {"action": "confirmed", "spot_description": "Záchranář"}

    def test_send_assignment_released_stores_change_value(self, app):
        """Released row has change_type='assignment' and action/spot_description."""
        with app.app_context():
            event = _make_event("Payload Test Event 2")
            user = _make_member_user("payload_b@test.cz", "Payload User B")
            send_assignment_released(user, event, spot_description="Řidič")
            db.session.commit()
            row = db.session.scalar(
                db.select(OutboxEmail).where(OutboxEmail.notification_type == "assignment_released")
            )
            assert row is not None
            assert row.change_type == "assignment"
            payload = json.loads(row.change_value)
            assert payload == {"action": "released", "spot_description": "Řidič"}

    def test_send_unfilled_spots_reminder_stores_count(self, app):
        """Unfilled_reminder row has change_type='unfilled_reminder' and count."""
        with app.app_context():
            event = _make_event("Unfilled Event")
            user = _make_member_user("payload_c@test.cz", "Payload User C")
            spot1 = EventSpot(event_id=event.id, description="Spot 1")
            spot2 = EventSpot(event_id=event.id, description="Spot 2")
            db.session.add_all([spot1, spot2])
            db.session.flush()
            send_unfilled_spots_reminder(user, event, unfilled=[spot1, spot2])
            db.session.commit()
            row = db.session.scalar(db.select(OutboxEmail).where(OutboxEmail.notification_type == "unfilled_reminder"))
            assert row is not None
            assert row.change_type == "unfilled_reminder"
            payload = json.loads(row.change_value)
            assert payload == {"unfilled_count": 2}

    def test_send_debriefing_invitation_stores_assignment_id(self, app):
        """Debriefing_invitation row has change_type='debriefing' and assignment_id."""
        with app.app_context():
            event = _make_event("Debriefing Event")
            user = _make_member_user("payload_d@test.cz", "Payload User D")
            spot = EventSpot(event_id=event.id, description="Debriefing Spot")
            db.session.add(spot)
            db.session.flush()
            assignment = Assignment(
                spot_id=spot.id,
                user_id=user.id,
            )
            db.session.add(assignment)
            db.session.flush()
            send_debriefing_invitation(assignment, event)
            db.session.commit()
            row = db.session.scalar(
                db.select(OutboxEmail).where(OutboxEmail.notification_type == "debriefing_invitation")
            )
            assert row is not None
            assert row.change_type == "debriefing"
            payload = json.loads(row.change_value)
            assert payload == {"assignment_id": assignment.id}


class TestSchedulerRequestContextBoundary:
    """Regression: process_email_queue must provide its own request context.

        B-1/B-2 fix: scheduler/main.py wraps the drain in app.test_request_context.
    Two-part test:
          1. Proves the bug exists — drain raises RuntimeError without a request context.
          2. Proves the fix works — process_email_queue succeeds because it provides one.
    """

    def test_drain_without_request_context_succeeds(self, app):
        """B-4 regression: process_email_queue provides its own request context."""
        with app.app_context():
            user, event = _make_ed_event(delta_hours=72)
            past = datetime.now(timezone.utc) - timedelta(minutes=5)
            row = _make_batched_row(user, event, "event_published", send_after=past)
            db.session.commit()
            row_id = row.id

        saved = app.config.get("SERVER_NAME")
        app.config["SERVER_NAME"] = None
        try:
            # Part 1: drain_batched_outbox needs a request context; without one it raises.
            # This proves the bug is real — if the scheduler called the drain bare, it
            # would crash in production (no SERVER_NAME in ProductionConfig).
            with app.app_context():
                with pytest.raises(RuntimeError):
                    drain_batched_outbox()

            # Part 2: reload scheduler.main with the test app injected so that
            # scheduler_main.app points at the test DB. process_email_queue wraps the
            # drain in app.test_request_context, so it must NOT raise.
            with patch("app.create_app", return_value=app):
                importlib.reload(scheduler_main)

            with patch("flask_mail.Mail.send"):
                scheduler_main.process_email_queue()  # must not raise

            with app.app_context():
                r = db.session.get(OutboxEmail, row_id)
                assert r.status == "sent"
        finally:
            app.config["SERVER_NAME"] = saved


class TestProcessEmailQueueDispatch:
    """M-3: process_email_queue calls the REAL dispatch; patching the drains proves it."""

    @pytest.fixture(autouse=True)
    def _import_scheduler(self, app):
        """Reload scheduler.main per test with the test app injected."""
        with patch("app.create_app", return_value=app):
            importlib.reload(scheduler_main)
        self._sm = scheduler_main
        yield

    def test_batched_drain_called_first(self, app):
        """Drain_batched_outbox returns True → drain_one_outbox_email NOT called."""
        with (
            patch.object(self._sm, "drain_batched_outbox", return_value=True) as mock_batch,
            patch.object(self._sm, "drain_one_outbox_email") as mock_legacy,
            patch.object(self._sm, "get_settings"),
        ):
            self._sm.process_email_queue()
        mock_batch.assert_called_once()
        mock_legacy.assert_not_called()

    def test_legacy_drain_called_when_batch_returns_false(self, app):
        """Drain_batched_outbox returns False → drain_one_outbox_email IS called."""
        with (
            patch.object(self._sm, "drain_batched_outbox", return_value=False) as mock_batch,
            patch.object(self._sm, "drain_one_outbox_email") as mock_legacy,
            patch.object(self._sm, "get_settings"),
        ):
            self._sm.process_email_queue()
        mock_batch.assert_called_once()
        mock_legacy.assert_called_once()
