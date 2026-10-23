"""External opportunity mail: useful recipients, batching, idempotency, and stale delivery."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app.extensions import db
from app.mail import (
    drain_batched_outbox,
    flush_and_notify_cancelled,
    send_assignment_confirmed,
    send_assignments_opened,
    send_debriefing_invitation,
    send_event_changed,
    send_event_published,
    send_unfilled_spots_reminder,
)
from app.models.assignment import Assignment
from app.models.event import Event, EventQualificationRequirement, EventStatus, StaffingMode
from app.models.outbox import OutboxEmail
from app.models.qualification import Qualification
from app.models.role import Role
from app.models.settings import get_settings
from app.models.user import UserAccount
from app.scheduler_tasks import run_send_external_opportunities
from tests.conftest import _make_user
from tests.test_app_settings import _form
from tests.test_external_events import make_opportunity


def external_user():
    return db.session.scalar(db.select(UserAccount).where(UserAccount.email == "external@test.com"))


def immediate_notifications():
    settings = get_settings()
    settings.notify_delay_under_24h_min = 0
    settings.notify_delay_1_7_days_min = 0
    settings.notify_delay_1_4_weeks_min = 0
    settings.notify_delay_over_month_min = 0
    db.session.commit()


@pytest.mark.parametrize("mode", list(StaffingMode))
def test_scheduler_queues_each_opportunity_once(app, external_client, mode):
    event_id, _ = make_opportunity(app, mode=mode)
    with app.app_context():
        assert run_send_external_opportunities(db.session) == 1
        assert run_send_external_opportunities(db.session) == 0
        row = db.session.scalar(db.select(OutboxEmail).where(OutboxEmail.notification_type == "external_opportunity"))
        assert row.event_id == event_id
        assert row.user_id == external_user().id
        row.status = "sent"
        db.session.commit()
        assert run_send_external_opportunities(db.session) == 0


@pytest.mark.parametrize("days", [3, 10, 21])
def test_scheduler_detects_configured_window_boundary(app, external_client, days):
    with app.app_context():
        get_settings().external_opportunity_days = days
        db.session.commit()
    event_id, _ = make_opportunity(app, days=days)
    now = datetime.now(timezone.utc)
    with app.app_context():
        db.session.get(Event, event_id).start_datetime = now + timedelta(days=days)
        db.session.commit()
        assert run_send_external_opportunities(db.session, now) == 0
        assert run_send_external_opportunities(db.session, now + timedelta(seconds=1)) == 1


def test_scheduler_detects_manual_early_opening(app, external_client):
    event_id, _ = make_opportunity(app, days=30)
    with app.app_context():
        assert run_send_external_opportunities(db.session) == 0
        db.session.get(Event, event_id).open_to_externals = True
        db.session.commit()
        assert run_send_external_opportunities(db.session) == 1


@pytest.mark.parametrize(
    "reason", ["inactive", "archived", "member", "unqualified", "assigned", "coordinated", "disabled"]
)
def test_scheduler_excludes_unavailable_recipients(app, external_client, reason):
    event_id, spot_id = make_opportunity(app)
    with app.app_context():
        user = external_user()
        event = db.session.get(Event, event_id)
        if reason == "inactive":
            user.is_active = False
        elif reason == "archived":
            user.is_archived = True
        elif reason == "member":
            user.roles = [db.session.scalar(db.select(Role).where(Role.name == Role.MEMBER))]
        elif reason == "unqualified":
            event.spots[0].required_qualifications.append(Qualification(name="Specialist"))
        elif reason == "assigned":
            event.assignments.append(Assignment(user=user, spot_id=spot_id))
        elif reason == "coordinated":
            event.master_event.coordinator = _make_user("coord@test.com", "Coordinator", Role.COORDINATOR)
        elif reason == "disabled":
            get_settings().notify_external_opportunities = False
        db.session.commit()
        assert run_send_external_opportunities(db.session) == 0
        assert (
            db.session.scalar(db.select(OutboxEmail).where(OutboxEmail.notification_type == "external_opportunity"))
            is None
        )


def test_condition_notification_respects_reserved_qualifications(app, external_client):
    event_id, _ = make_opportunity(app, mode=StaffingMode.CONDITIONS)
    with app.app_context():
        event = db.session.get(Event, event_id)
        qualification = Qualification(name="RP needed", can_be_rp=True)
        db.session.add(qualification)
        db.session.flush()
        event.maximum_participants = 1
        event.qualification_requirements.append(
            EventQualificationRequirement(qualification=qualification, minimum_count=1)
        )
        db.session.commit()
        assert run_send_external_opportunities(db.session) == 0
        user = external_user()
        user.qualifications.append(qualification)
        db.session.commit()
        assert run_send_external_opportunities(db.session) == 1


def test_multiple_opportunities_are_one_email(app, external_client):
    make_opportunity(app, name="First opportunity")
    make_opportunity(app, name="Second opportunity")
    with app.app_context():
        immediate_notifications()
        assert run_send_external_opportunities(db.session) == 2
        with patch("app.mail.mail.send") as send:
            assert drain_batched_outbox()
            assert send.call_count == 1
            message = send.call_args.args[0]
            assert "externí spolupracovníky" in message.subject
            assert "First opportunity" in message.html and "Second opportunity" in message.html
            assert "přihlaste se na akci" in message.html
        assert not drain_batched_outbox()
        assert run_send_external_opportunities(db.session) == 0


@pytest.mark.parametrize(
    "reason", ["filled", "closed", "past", "archived", "inactive", "role", "qualification", "disabled"]
)
def test_delivery_suppresses_stale_opportunity(app, external_client, reason):
    event_id, _ = make_opportunity(app)
    with app.app_context():
        immediate_notifications()
        assert run_send_external_opportunities(db.session) == 1
        event, user = db.session.get(Event, event_id), external_user()
        if reason == "filled":
            member = _make_user("crew@test.com", "Crew", Role.MEMBER)
            event.assignments.append(Assignment(user=member, spot_id=event.spots[0].id))
        elif reason == "closed":
            event.status = EventStatus.ASSIGNMENTS_CLOSED
        elif reason == "past":
            event.start_datetime = datetime.now(timezone.utc) - timedelta(seconds=1)
        elif reason == "archived":
            event.archived = True
        elif reason == "inactive":
            user.is_active = False
        elif reason == "role":
            user.roles = [db.session.scalar(db.select(Role).where(Role.name == Role.MEMBER))]
        elif reason == "qualification":
            event.spots[0].required_qualifications.append(Qualification(name="Now required"))
        elif reason == "disabled":
            get_settings().notify_external_opportunities = False
        db.session.commit()
        with patch("app.mail.mail.send") as send:
            assert drain_batched_outbox()
            send.assert_not_called()
        row = db.session.scalar(db.select(OutboxEmail).where(OutboxEmail.notification_type == "external_opportunity"))
        assert row.status == "skipped"
        assert row.last_error == "external_opportunity_no_longer_available"


def test_suppressed_opportunity_can_notify_after_reopening(app, external_client):
    event_id, _ = make_opportunity(app)
    with app.app_context():
        immediate_notifications()
        assert run_send_external_opportunities(db.session) == 1
        event = db.session.get(Event, event_id)
        event.status = EventStatus.ASSIGNMENTS_CLOSED
        db.session.commit()
        with patch("app.mail.mail.send") as send:
            assert drain_batched_outbox()
            send.assert_not_called()
        event.status = EventStatus.ASSIGNMENTS_OPEN
        db.session.commit()
        assert run_send_external_opportunities(db.session) == 1
        with patch("app.mail.mail.send") as send:
            assert drain_batched_outbox()
            send.assert_called_once()
        assert run_send_external_opportunities(db.session) == 0


def test_cancel_does_not_send_unsolicited_mail_to_external_opportunity_recipient(app, external_client):
    event_id, _ = make_opportunity(app)
    with app.app_context():
        immediate_notifications()
        assert run_send_external_opportunities(db.session) == 1
        event = db.session.get(Event, event_id)
        event.status = EventStatus.CANCELLED
        event.archived = True
        flush_and_notify_cancelled(event)
        db.session.commit()
        with patch("app.mail.mail.send") as send:
            assert drain_batched_outbox()
            send.assert_not_called()
        assert (
            db.session.scalar(db.select(OutboxEmail).where(OutboxEmail.notification_type == "event_cancelled")) is None
        )


def test_external_receives_own_participation_notifications_only(app, external_client):
    event_id, spot_id = make_opportunity(app)
    with app.app_context():
        user, event = external_user(), db.session.get(Event, event_id)
        assignment = Assignment(event_id=event_id, spot_id=spot_id, user_id=user.id)
        db.session.add(assignment)
        db.session.flush()
        send_event_published(user, event)
        send_assignments_opened(user, event)
        send_unfilled_spots_reminder(user, event, [])
        assert db.session.scalar(db.select(OutboxEmail)) is None
        send_assignment_confirmed(user, event)
        send_event_changed(user, event, {"name": ["Before", "After"]})
        send_debriefing_invitation(assignment, event)
        db.session.commit()
        assert set(db.session.scalars(db.select(OutboxEmail.notification_type))) == {
            "assignment_confirmed",
            "event_changed",
            "debriefing_invitation",
        }


def test_admin_can_preview_external_opportunity(app, admin_client):
    event_id, _ = make_opportunity(app, days=30)
    response = admin_client.post(
        "/admin/notifications/test/external_opportunity",
        data={"test_email": "preview@test.com", "test_event_id": str(event_id), "send_immediately": "1"},
    )
    assert response.status_code == 302
    with app.app_context():
        with patch("app.mail.mail.send") as send:
            assert drain_batched_outbox()
            assert send.call_args.args[0].recipients == ["preview@test.com"]


def test_new_notification_toggle_is_visible_and_can_be_disabled(app, admin_client):
    assert b"notify_external_opportunities" in admin_client.get("/admin/notifications/").data
    admin_client.post("/admin/notifications/", data={})
    with app.app_context():
        assert not get_settings().notify_external_opportunities


def test_scheduler_does_not_requeue_dev_blocked_recipients(app, external_client):
    make_opportunity(app)
    with app.app_context():
        settings = get_settings()
        settings.dev_email_block = True
        settings.dev_email_allowlist = "allowed@test.com"
        db.session.commit()
        assert run_send_external_opportunities(db.session) == 0
        assert run_send_external_opportunities(db.session) == 0
        assert db.session.scalar(db.select(OutboxEmail)) is None
        settings.dev_email_allowlist = "external@test.com"
        db.session.commit()
        assert run_send_external_opportunities(db.session) == 1


def test_opportunity_with_maximum_length_name_can_be_queued(app, external_client):
    event_id, _ = make_opportunity(app)
    with app.app_context():
        db.session.get(Event, event_id).name = "X" * 255
        db.session.commit()
        assert run_send_external_opportunities(db.session) == 1
        row = db.session.scalar(db.select(OutboxEmail).where(OutboxEmail.notification_type == "external_opportunity"))
        assert len(row.subject) <= 255


def test_saving_window_changes_next_scheduler_run(app, admin_client):
    make_opportunity(app, days=20)
    with app.app_context():
        _make_user("external@test.com", "External", Role.EXTERNAL)
        assert run_send_external_opportunities(db.session) == 0
    admin_client.post("/admin/settings/", data=_form("", external_opportunity_days="21"))
    with app.app_context():
        assert run_send_external_opportunities(db.session) == 1
