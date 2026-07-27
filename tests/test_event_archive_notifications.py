"""Tests for the archive/unarchive notification behaviour.

Covers:
- enqueue_deferred(immediate=True) forces send_after=NULL and merges into
  existing rows.
- send_event_archived / send_event_unarchived helpers.
- flush_and_notify_archived: bulk-clears send_after for the event and
  enqueues an archive notice to the union of assigned users + users with
  pending outbox rows for the event.
- Route wiring for /events/<id>/archive, /events/<id>/cancel,
  /events/<id>/unarchive, /events/<id>/restore, and master-event archive
  cascade.
"""

from datetime import datetime, timezone

import pytest

import app.mail as mailer
from app.extensions import db
from app.models.assignment import Assignment
from app.models.event import Event, EventSpot, EventStatus
from app.models.master_event import MasterEvent
from app.models.outbox import OutboxEmail
from app.models.role import Role
from app.models.settings import get_settings
from app.models.user import UserAccount


@pytest.fixture(autouse=True)
def _enable_archive_notifications(app):
    """Other tests toggle AppSettings notify_* flags off via the admin form and
    don't restore them, which leaks across test files. Re-enable the flags
    this file's tests rely on before each test."""
    with app.app_context():
        s = get_settings()
        s.notify_event_archived = True
        s.notify_event_unarchived = True
        s.notify_event_published = True
        s.notify_event_changed = True
        db.session.commit()


def _mk_member(email: str, name: str = "Member") -> UserAccount:
    role = db.session.scalar(db.select(Role).where(Role.name == Role.MEMBER))
    u = UserAccount(email=email, name=name, is_active=True)
    u.set_password("pass")
    u.roles = [role]
    db.session.add(u)
    db.session.flush()
    return u


def _mk_event_with_assignments(app, member_emails: list[str], name: str = "Archive Test Event") -> tuple[int, list]:
    """Create ME + Event + one spot per member + Assignment. Returns (event_id, [user_id,...])."""
    with app.app_context():
        me = MasterEvent(name=f"ME for {name}")
        db.session.add(me)
        db.session.flush()
        event = Event(
            name=name,
            master_event_id=me.id,
            status=EventStatus.ASSIGNMENTS_OPEN,
            start_datetime=datetime(2031, 6, 1, 10, 0, tzinfo=timezone.utc),
            end_datetime=datetime(2031, 6, 1, 18, 0, tzinfo=timezone.utc),
        )
        db.session.add(event)
        db.session.flush()
        user_ids = []
        for email in member_emails:
            u = _mk_member(email)
            spot = EventSpot(event_id=event.id)
            db.session.add(spot)
            db.session.flush()
            db.session.add(Assignment(spot_id=spot.id, user_id=u.id))
            user_ids.append(u.id)
        db.session.commit()
        return event.id, user_ids


class TestEnqueueDeferredImmediateKwarg:
    def test_immediate_true_sets_send_after_null_on_new_row(self, app):
        event_id, [uid] = _mk_event_with_assignments(app, ["imm1@test.cz"])
        with app.app_context():
            user = db.session.get(UserAccount, uid)
            event = db.session.get(Event, event_id)
            mailer.enqueue_deferred(
                user=user,
                event=event,
                notification_type="event_archived",
                subject="s",
                body="b",
                html_body="<p>h</p>",
                immediate=True,
            )
            db.session.commit()
            row = db.session.scalar(
                db.select(OutboxEmail).where(
                    OutboxEmail.user_id == uid,
                    OutboxEmail.event_id == event_id,
                )
            )
            assert row is not None
            assert row.send_after is None

    def test_immediate_true_overrides_existing_deferred_row(self, app):
        event_id, [uid] = _mk_event_with_assignments(app, ["imm2@test.cz"])
        with app.app_context():
            user = db.session.get(UserAccount, uid)
            event = db.session.get(Event, event_id)
            # First enqueue: deferred (default).
            mailer.enqueue_deferred(
                user=user,
                event=event,
                notification_type="event_published",
                subject="s",
                body="b",
                html_body="<p>h</p>",
            )
            db.session.commit()
            initial = db.session.scalar(db.select(OutboxEmail).where(OutboxEmail.event_id == event_id))
            assert initial.send_after is not None
            # Second enqueue: immediate — must collapse send_after to NULL.
            mailer.enqueue_deferred(
                user=user,
                event=event,
                notification_type="event_published",
                subject="s2",
                body="b2",
                html_body="<p>h2</p>",
                immediate=True,
            )
            db.session.commit()
            row = db.session.scalar(db.select(OutboxEmail).where(OutboxEmail.event_id == event_id))
            assert row.send_after is None

    def test_immediate_false_computes_send_after(self, app):
        event_id, [uid] = _mk_event_with_assignments(app, ["imm3@test.cz"])
        with app.app_context():
            user = db.session.get(UserAccount, uid)
            event = db.session.get(Event, event_id)
            mailer.enqueue_deferred(
                user=user,
                event=event,
                notification_type="event_published",
                subject="s",
                body="b",
                html_body="<p>h</p>",
            )
            db.session.commit()
            row = db.session.scalar(db.select(OutboxEmail).where(OutboxEmail.event_id == event_id))
            assert row.send_after is not None


class TestSendEventArchivedHelper:
    def test_enqueues_immediate_row(self, app):
        event_id, [uid] = _mk_event_with_assignments(app, ["arch-h@test.cz"])
        with app.app_context():
            user = db.session.get(UserAccount, uid)
            event = db.session.get(Event, event_id)
            mailer.send_event_archived(user, event)
            db.session.commit()
            row = db.session.scalar(
                db.select(OutboxEmail).where(
                    OutboxEmail.user_id == uid,
                    OutboxEmail.notification_type == "event_archived",
                )
            )
            assert row is not None
            assert row.send_after is None
            assert "archivov" in (row.subject or "").lower()

    def test_disabled_by_settings_flag(self, app):
        event_id, [uid] = _mk_event_with_assignments(app, ["arch-off@test.cz"])
        with app.app_context():
            s = get_settings()
            s.notify_event_archived = False
            db.session.commit()
            try:
                user = db.session.get(UserAccount, uid)
                event = db.session.get(Event, event_id)
                mailer.send_event_archived(user, event)
                db.session.commit()
                row = db.session.scalar(
                    db.select(OutboxEmail).where(
                        OutboxEmail.user_id == uid,
                        OutboxEmail.notification_type == "event_archived",
                    )
                )
                assert row is None
            finally:
                get_settings().notify_event_archived = True
                db.session.commit()


class TestSendEventUnarchivedHelper:
    def test_enqueues_deferred_row(self, app):
        event_id, [uid] = _mk_event_with_assignments(app, ["unarch-h@test.cz"])
        with app.app_context():
            user = db.session.get(UserAccount, uid)
            event = db.session.get(Event, event_id)
            mailer.send_event_unarchived(user, event)
            db.session.commit()
            row = db.session.scalar(
                db.select(OutboxEmail).where(
                    OutboxEmail.user_id == uid,
                    OutboxEmail.notification_type == "event_unarchived",
                )
            )
            assert row is not None
            assert row.send_after is not None


class TestFlushAndNotifyArchived:
    def test_flushes_pending_and_notifies_assigned(self, app):
        event_id, uids = _mk_event_with_assignments(app, ["fna1@test.cz", "fna2@test.cz"])
        with app.app_context():
            event = db.session.get(Event, event_id)
            # Seed a deferred change notification for one assigned user.
            u0 = db.session.get(UserAccount, uids[0])
            mailer.enqueue_deferred(
                user=u0,
                event=event,
                notification_type="event_changed",
                subject="pending edit",
                body="b",
                html_body="<p>edit</p>",
                change_type=mailer._EVENT_CHANGED_CHANGE_TYPE,
                change_value={"name": ["A", "B"]},
            )
            db.session.commit()
            pre = db.session.scalar(
                db.select(OutboxEmail).where(
                    OutboxEmail.user_id == uids[0],
                    OutboxEmail.notification_type == "event_changed",
                )
            )
            assert pre.send_after is not None

            mailer.flush_and_notify_archived(event)
            db.session.commit()

            # Existing deferred row is now immediate.
            edit_row = db.session.scalar(
                db.select(OutboxEmail).where(
                    OutboxEmail.user_id == uids[0],
                    OutboxEmail.notification_type == "event_changed",
                )
            )
            assert edit_row.send_after is None

            # Both assigned users got an archive notice.
            archive_rows = db.session.scalars(
                db.select(OutboxEmail).where(
                    OutboxEmail.event_id == event_id,
                    OutboxEmail.notification_type == "event_archived",
                )
            ).all()
            assert {r.user_id for r in archive_rows} == set(uids)
            assert all(r.send_after is None for r in archive_rows)

    def test_notifies_union_including_pending_only_recipient(self, app):
        """User who has a pending outbox row for the event but is NOT currently
        assigned should still receive the archive notification (union rule)."""
        event_id, [uid_assigned] = _mk_event_with_assignments(app, ["union-a@test.cz"])
        with app.app_context():
            event = db.session.get(Event, event_id)
            ghost = _mk_member("union-ghost@test.cz", name="Ghost")
            db.session.commit()
            # Ghost has a pending row for this event but no assignment.
            ghost_id = ghost.id
            db.session.add(
                OutboxEmail(
                    to_email="union-ghost@test.cz",
                    subject="old change",
                    body="b",
                    html_body="<p>old</p>",
                    notification_type="event_changed",
                    user_id=ghost_id,
                    event_id=event_id,
                    status="pending",
                    change_type=mailer._EVENT_CHANGED_CHANGE_TYPE,
                    change_value='{"name": ["X", "Y"]}',
                )
            )
            db.session.commit()

            mailer.flush_and_notify_archived(event)
            db.session.commit()

            recipients = db.session.scalars(
                db.select(OutboxEmail.user_id).where(
                    OutboxEmail.event_id == event_id,
                    OutboxEmail.notification_type == "event_archived",
                )
            ).all()
            assert set(recipients) == {uid_assigned, ghost_id}


class TestArchiveRouteWiring:
    def test_archive_event_enqueues_archive_notice(self, app, admin_client):
        event_id, uids = _mk_event_with_assignments(app, ["route-arch@test.cz"])
        resp = admin_client.post(f"/events/{event_id}/archive", follow_redirects=True)
        assert resp.status_code == 200
        with app.app_context():
            row = db.session.scalar(
                db.select(OutboxEmail).where(
                    OutboxEmail.event_id == event_id,
                    OutboxEmail.notification_type == "event_archived",
                )
            )
            assert row is not None
            assert row.send_after is None

    def test_cancel_event_flushes_and_notifies_via_archive(self, app, admin_client):
        event_id, uids = _mk_event_with_assignments(app, ["route-cancel@test.cz"])
        resp = admin_client.post(f"/events/{event_id}/cancel", follow_redirects=True)
        assert resp.status_code == 200
        with app.app_context():
            arch = db.session.scalar(
                db.select(OutboxEmail).where(
                    OutboxEmail.event_id == event_id,
                    OutboxEmail.notification_type == "event_archived",
                )
            )
            assert arch is not None
            assert arch.send_after is None

    def test_unarchive_event_enqueues_unarchive_notice(self, app, admin_client):
        event_id, uids = _mk_event_with_assignments(app, ["route-unarch@test.cz"])
        admin_client.post(f"/events/{event_id}/archive", follow_redirects=True)
        resp = admin_client.post(f"/events/{event_id}/unarchive", follow_redirects=True)
        assert resp.status_code == 200
        with app.app_context():
            row = db.session.scalar(
                db.select(OutboxEmail).where(
                    OutboxEmail.event_id == event_id,
                    OutboxEmail.notification_type == "event_unarchived",
                )
            )
            assert row is not None
            assert row.send_after is not None

    def test_restore_event_enqueues_unarchive_notice(self, app, admin_client):
        event_id, uids = _mk_event_with_assignments(app, ["route-restore@test.cz"])
        admin_client.post(f"/events/{event_id}/cancel", follow_redirects=True)
        resp = admin_client.post(f"/events/{event_id}/restore", follow_redirects=True)
        assert resp.status_code == 200
        with app.app_context():
            row = db.session.scalar(
                db.select(OutboxEmail).where(
                    OutboxEmail.event_id == event_id,
                    OutboxEmail.notification_type == "event_unarchived",
                )
            )
            assert row is not None
            assert row.send_after is not None


class TestMasterEventArchiveCascade:
    def test_me_archive_flushes_and_notifies_per_child(self, app, admin_client):
        with app.app_context():
            me = MasterEvent(name="ME cascade")
            db.session.add(me)
            db.session.flush()
            me_id = me.id
            child_ids = []
            for i in range(2):
                event = Event(
                    name=f"child-{i}",
                    master_event_id=me_id,
                    status=EventStatus.PUBLISHED,
                    start_datetime=datetime(2031, 7, 1, 10, 0, tzinfo=timezone.utc),
                    end_datetime=datetime(2031, 7, 1, 18, 0, tzinfo=timezone.utc),
                )
                db.session.add(event)
                db.session.flush()
                spot = EventSpot(event_id=event.id)
                db.session.add(spot)
                db.session.flush()
                u = _mk_member(f"me-child-{i}@test.cz")
                db.session.add(Assignment(spot_id=spot.id, user_id=u.id))
                child_ids.append(event.id)
            db.session.commit()

        resp = admin_client.post(f"/master-events/{me_id}/archive", follow_redirects=True)
        assert resp.status_code == 200
        with app.app_context():
            rows = db.session.scalars(
                db.select(OutboxEmail).where(
                    OutboxEmail.notification_type == "event_archived",
                    OutboxEmail.event_id.in_(child_ids),
                )
            ).all()
            assert {r.event_id for r in rows} == set(child_ids)
            assert all(r.send_after is None for r in rows)

    def test_me_unarchive_stays_silent(self, app, admin_client):
        with app.app_context():
            me = MasterEvent(name="ME silent unarchive", archived=True)
            db.session.add(me)
            db.session.flush()
            me_id = me.id
            event = Event(
                name="silent-child",
                master_event_id=me_id,
                status=EventStatus.PUBLISHED,
                start_datetime=datetime(2031, 8, 1, 10, 0, tzinfo=timezone.utc),
                end_datetime=datetime(2031, 8, 1, 18, 0, tzinfo=timezone.utc),
                archived=True,
            )
            db.session.add(event)
            db.session.commit()
            event_id = event.id

        resp = admin_client.post(f"/master-events/{me_id}/unarchive", follow_redirects=True)
        assert resp.status_code == 200
        with app.app_context():
            n = db.session.scalar(
                db.select(db.func.count(OutboxEmail.id)).where(
                    OutboxEmail.event_id == event_id,
                    OutboxEmail.notification_type == "event_unarchived",
                )
            )
            assert n == 0
