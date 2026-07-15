"""
Tests for the email outbox pipeline and individual send_* helpers.

Strategy:
  - All tests use unittest.mock.patch to replace flask_mail.Mail.send so no
    real SMTP connection is made.
  - Tests verify that the correct OutboxEmail rows are created (subject,
    recipient, body keywords) and that the scheduler's process_email_queue
    function transitions rows through pending → sent / failed correctly.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from click.testing import CliRunner

from app.extensions import db
from app.mail import (
    drain_one_outbox_email,
    enqueue_deferred,
    send_admin_digest,
    send_assignment_confirmed,
    send_assignment_released,
    send_assignments_opened,
    send_event_cancelled,
    send_event_changed,
    send_event_published,
    send_unfilled_spots_reminder,
)
from app.models.audit import AuditLogEntry
from app.models.event import Event, EventStatus
from app.models.master_event import MasterEvent
from app.models.outbox import OutboxEmail
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
            assert "Jan Novák" in row.html_body

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
            assert "3" in row.html_body

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
        """drain_one_outbox_email on an empty outbox must return False."""
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


# ── Phase 1 (#268) — OutboxEmail new columns ─────────────────────────────────


class TestOutboxEmailPhase1Columns:
    """AC-3 / AC-12: new columns default to NULL; populated values round-trip."""

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


# ── Phase 3 (#268) — non-event helper columns null ───────────────────────────


class TestNonEventHelperNullColumns:
    """AC-14: non-event send_* helpers produce rows with send_after/user_id/event_id all NULL."""

    def test_non_event_helper_has_null_batching_columns(self, app):
        with app.app_context():
            send_admin_digest("admin@test.cz", "Digest", "<p>body</p>")
            db.session.commit()
            row = db.session.scalar(db.select(OutboxEmail).where(OutboxEmail.notification_type == "admin_digest"))
            assert row is not None
            assert row.send_after is None
            assert row.user_id is None
            assert row.event_id is None


# ── Phase 3 (#268) — enqueue_deferred helper ─────────────────────────────────


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
    """Phase 3 (#268) — enqueue_deferred tier logic, upsert, and immediate bypass."""

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
                from flask import g as flask_g  # pylint: disable=import-outside-toplevel

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
                from flask import g as flask_g  # pylint: disable=import-outside-toplevel

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
            send_event_changed(user, event, {"name": ["A", "B"]}, event_url="http://x/1")
            db.session.flush()
            send_event_changed(user, event, {"name": ["B", "C"]}, event_url="http://x/1")
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

    def test_html_body_overwritten_on_update(self, app):
        with app.app_context():
            user, event = _make_ed_event(delta_hours=72)
            enqueue_deferred(user, event, "event_changed", "Subj", "body", html_body="<p>v1</p>")
            db.session.flush()
            enqueue_deferred(user, event, "event_changed", "Subj", "body", html_body="<p>v2</p>")
            db.session.commit()
            row = db.session.scalar(db.select(OutboxEmail).where(OutboxEmail.notification_type == "event_changed"))
            assert row is not None
            assert row.html_body == "<p>v2</p>"

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


# ── Phase 3 (#268) — drain send_after filter ─────────────────────────────────


class TestDrainSendAfterFilter:
    """AC-11 / AC-12 / AC-13: rows with future send_after are held; past/null are sent."""

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
