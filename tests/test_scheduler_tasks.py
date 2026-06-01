"""Additional tests for scheduler_tasks.py (run_send_reminders + no-eligible path)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import sqlalchemy as sa

from app.extensions import db
from app.models.assignment import Assignment
from app.models.digest import get_digest_schedule
from app.models.event import Event, EventSpot, EventStatus
from app.models.master_event import MasterEvent
from app.models.outbox import OutboxEmail
from app.models.role import Role
from app.models.settings import get_settings
from app.models.user import UserAccount
from app.scheduler_tasks import run_admin_digest, run_send_reminders
from tests.conftest import _make_user


def _make_open_event_with_rp(app, start_dt: datetime) -> int:
    """Create an ASSIGNMENTS_OPEN event with one unfilled spot and an RP."""
    with app.app_context():
        me = MasterEvent(name="Reminder ME")
        db.session.add(me)
        db.session.flush()

        rp_role = db.session.scalar(db.select(Role).where(Role.name == Role.COORDINATOR))
        rp = UserAccount(email="rp@test.com", name="RP User", is_active=True)
        rp.set_password("testpass123")
        rp.roles = [rp_role]
        db.session.add(rp)
        db.session.flush()

        creator_role = db.session.scalar(db.select(Role).where(Role.name == Role.ADMIN))
        creator = UserAccount(email="creator_sched@test.com", name="Creator", is_active=True)
        creator.set_password("testpass123")
        creator.roles = [creator_role]
        db.session.add(creator)
        db.session.flush()

        event = Event(
            name="Reminder Event",
            master_event_id=me.id,
            start_datetime=start_dt,
            end_datetime=start_dt + timedelta(hours=8),
            status=EventStatus.ASSIGNMENTS_OPEN,
            created_by_id=creator.id,
            responsible_person_id=rp.id,
        )
        db.session.add(event)
        db.session.flush()
        db.session.add(EventSpot(event_id=event.id))  # unfilled mandatory spot
        db.session.commit()
        return event.id


# ── run_send_reminders ────────────────────────────────────────────────────────


def test_send_reminders_returns_zero_no_events(app):
    """No ASSIGNMENTS_OPEN events → returns 0."""

    with app.app_context():
        now = datetime(2030, 6, 1, 10, 0, tzinfo=timezone.utc)
        result = run_send_reminders(db.session, now=now)
    assert result == 0


def test_send_reminders_skips_when_window_not_open(app):
    """Reminder window hasn't opened yet → returns 0."""

    now = datetime(2030, 6, 1, 10, 0, tzinfo=timezone.utc)
    # Event starts 25h from now, default reminder at 24h → window opens in 1h
    start = now + timedelta(hours=25)
    _make_open_event_with_rp(app, start)

    with app.app_context():
        result = run_send_reminders(db.session, now=now)
    assert result == 0


def test_send_reminders_sends_when_window_open(app):
    """Reminder window has opened → enqueues reminder email."""

    now = datetime(2030, 6, 1, 10, 0, tzinfo=timezone.utc)
    # Event starts 23h from now, default reminder at 24h → window opened 1h ago
    start = now + timedelta(hours=23)
    _make_open_event_with_rp(app, start)

    with app.app_context():
        result = run_send_reminders(db.session, now=now)
        outbox = db.session.scalars(sa.select(OutboxEmail)).all()

    assert result == 1
    assert len(outbox) >= 1


def test_send_reminders_skips_already_sent(app):
    """Reminder already recorded in reminder_sent_json → not sent again."""

    now = datetime(2030, 6, 1, 10, 0, tzinfo=timezone.utc)
    start = now + timedelta(hours=23)
    event_id = _make_open_event_with_rp(app, start)

    with app.app_context():
        # Pre-mark the 24h reminder as already sent
        event = db.session.get(Event, event_id)
        event.reminder_sent_json = {"24": now.isoformat()}
        db.session.commit()

        result = run_send_reminders(db.session, now=now)
    assert result == 0


def test_send_reminders_skips_fully_filled_event(app):
    """All mandatory spots filled → no reminder sent."""

    now = datetime(2030, 6, 1, 10, 0, tzinfo=timezone.utc)
    start = now + timedelta(hours=23)
    event_id = _make_open_event_with_rp(app, start)

    with app.app_context():
        # Fill the spot
        spot = db.session.scalar(sa.select(EventSpot).where(EventSpot.event_id == event_id))
        filler = db.session.scalar(db.select(UserAccount).where(UserAccount.email == "rp@test.com"))
        db.session.add(Assignment(spot_id=spot.id, user_id=filler.id, assigned_by_id=filler.id))
        db.session.commit()

        result = run_send_reminders(db.session, now=now)
    assert result == 0


# ── run_admin_digest no eligible recipients ────────────────────────────────────


def test_run_admin_digest_no_eligible_recipients(app):
    """When no Admin-role users exist, digest is skipped but last_sent_at is updated."""

    with app.app_context():
        schedule = get_digest_schedule()
        hour = schedule.preferred_hour
        now = datetime(2025, 6, 1, hour, 0, tzinfo=ZoneInfo(get_settings().timezone))
        schedule.enabled = True
        schedule.last_sent_at = None
        db.session.commit()
        # No Admin users — only non-admin users won't qualify
        _make_user("member_only@test.com", "Member Only", Role.MEMBER)
        result = run_admin_digest(db.session, now=now)
        updated = get_digest_schedule()
        last_sent = updated.last_sent_at

    assert result is False
    assert last_sent is not None  # last_sent_at updated even when skipped
