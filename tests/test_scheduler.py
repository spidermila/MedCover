"""Tests for scheduler auto-transition functions with retry on concurrent modification."""

import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import sqlalchemy as sa
from sqlalchemy.orm.exc import StaleDataError

from app.extensions import db
from app.models.audit import AuditLogEntry
from app.models.event import Event, EventStatus
from app.models.master_event import MasterEvent
from app.models.role import Role
from app.models.user import UserAccount


def _make_event(app, status: EventStatus, end_offset_hours: int = -1) -> int:
    """Create a minimal Event with the given status. Returns event id."""
    with app.app_context():
        me = MasterEvent(name="Sched ME")
        db.session.add(me)
        db.session.flush()

        creator_role = db.session.scalar(db.select(Role).where(Role.name == Role.ADMIN))
        creator = UserAccount(email="sched_creator@test.com", name="Sched Creator", is_active=True)
        creator.set_password("testpass123")
        creator.roles = [creator_role]
        db.session.add(creator)
        db.session.flush()

        now = datetime.now(timezone.utc)
        event = Event(
            name="Sched Event",
            master_event_id=me.id,
            start_datetime=now + timedelta(hours=end_offset_hours - 1),
            end_datetime=now + timedelta(hours=end_offset_hours),
            status=status,
            created_by_id=creator.id,
            responsible_person_id=creator.id,
        )
        if status == EventStatus.PUBLISHED:
            event.assignments_open_datetime = now - timedelta(minutes=1)
        db.session.add(event)
        db.session.commit()
        return event.id


class TestOpenAssignments:
    def test_transitions_published_event(self, app) -> None:
        event_id = _make_event(app, EventStatus.PUBLISHED)
        import scheduler.main as sm  # pylint: disable=import-outside-toplevel

        with patch.object(sm, "app", app):
            sm.open_assignments()

        with app.app_context():
            event = db.session.get(Event, event_id)
            assert event is not None
            assert event.status == EventStatus.ASSIGNMENTS_OPEN
            entry = db.session.scalar(
                sa.select(AuditLogEntry).where(
                    AuditLogEntry.entity_type == "Event",
                    AuditLogEntry.entity_id == str(event_id),
                    AuditLogEntry.action_type == "status_change",
                )
            )
            assert entry is not None
            assert "Přihlašování" in entry.summary

    def test_retries_once_on_stale_version(self, app, caplog) -> None:
        event_id = _make_event(app, EventStatus.PUBLISHED)
        import scheduler.main as sm  # pylint: disable=import-outside-toplevel

        call_count = 0
        original_commit = db.session.commit

        def _flaky_commit():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise StaleDataError("simulated concurrent modification")
            return original_commit()

        with patch.object(sm, "app", app), app.app_context(), caplog.at_level(logging.WARNING, logger="scheduler.main"):
            with patch.object(db.session, "commit", side_effect=_flaky_commit):
                sm._apply_event_transition_with_retry(event_id, sm._open_assignments_mutator, "open_assignments")

        assert "retrying" in caplog.text
        with app.app_context():
            event = db.session.get(Event, event_id)
            assert event is not None
            assert event.status == EventStatus.ASSIGNMENTS_OPEN

    def test_skips_after_two_stale_errors(self, app, caplog) -> None:
        event_id = _make_event(app, EventStatus.PUBLISHED)
        import scheduler.main as sm  # pylint: disable=import-outside-toplevel

        def _always_stale():
            raise StaleDataError("simulated concurrent modification")

        with patch.object(sm, "app", app), app.app_context(), caplog.at_level(logging.ERROR, logger="scheduler.main"):
            with patch.object(db.session, "commit", side_effect=_always_stale):
                sm._apply_event_transition_with_retry(event_id, sm._open_assignments_mutator, "open_assignments")

        assert "skipping" in caplog.text
        with app.app_context():
            event = db.session.get(Event, event_id)
            assert event is not None
            assert event.status == EventStatus.PUBLISHED


class TestCloseCompletedEvents:
    def test_transitions_open_event_to_completed(self, app) -> None:
        event_id = _make_event(app, EventStatus.ASSIGNMENTS_OPEN, end_offset_hours=-1)
        import scheduler.main as sm  # pylint: disable=import-outside-toplevel

        with patch.object(sm, "app", app):
            sm.close_completed_events()

        with app.app_context():
            event = db.session.get(Event, event_id)
            assert event is not None
            assert event.status == EventStatus.COMPLETED

    def test_retries_once_on_stale_version(self, app, caplog) -> None:
        event_id = _make_event(app, EventStatus.ASSIGNMENTS_OPEN, end_offset_hours=-1)
        import scheduler.main as sm  # pylint: disable=import-outside-toplevel

        call_count = 0
        original_commit = db.session.commit

        def _flaky_commit():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise StaleDataError("simulated concurrent modification")
            return original_commit()

        with patch.object(sm, "app", app), app.app_context(), caplog.at_level(logging.WARNING, logger="scheduler.main"):
            with patch.object(db.session, "commit", side_effect=_flaky_commit):
                sm._apply_event_transition_with_retry(event_id, sm._close_completed_mutator, "close_completed_events")

        assert "retrying" in caplog.text
        with app.app_context():
            event = db.session.get(Event, event_id)
            assert event is not None
            assert event.status == EventStatus.COMPLETED

    def test_skips_after_two_stale_errors(self, app, caplog) -> None:
        event_id = _make_event(app, EventStatus.ASSIGNMENTS_OPEN, end_offset_hours=-1)
        import scheduler.main as sm  # pylint: disable=import-outside-toplevel

        def _always_stale():
            raise StaleDataError("simulated concurrent modification")

        with (
            patch.object(sm, "app", app),
            app.app_context(),
            caplog.at_level(logging.ERROR, logger="scheduler.main"),
        ):
            with patch.object(db.session, "commit", side_effect=_always_stale):
                sm._apply_event_transition_with_retry(event_id, sm._close_completed_mutator, "close_completed_events")

        assert "skipping" in caplog.text
        with app.app_context():
            event = db.session.get(Event, event_id)
            assert event is not None
            assert event.status == EventStatus.ASSIGNMENTS_OPEN

    def test_skips_missing_event(self, app, caplog) -> None:
        import scheduler.main as sm  # pylint: disable=import-outside-toplevel

        with app.app_context(), caplog.at_level(logging.WARNING, logger="scheduler.main"):
            sm._apply_event_transition_with_retry(999999, sm._close_completed_mutator, "close_completed_events")

        assert "not found" in caplog.text
