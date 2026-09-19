from datetime import datetime, timedelta, timezone

from flask import render_template
from icalendar import Calendar

from app.digest.blocks.upcoming_events import UpcomingEventsBlock
from app.extensions import db
from app.mail import _build_event_section, _format_event_change_value
from app.models.assignment import Assignment
from app.models.event import Event, EventQualificationRequirement, EventStatus
from app.models.outbox import OutboxEmail
from app.models.qualification import Qualification
from app.models.role import Role
from app.printout_generator import generate_printout
from app.routes.reports import _spot_and_assignment_data
from app.scheduler_tasks import run_send_reminders
from tests.conftest import _make_user
from tests.test_condition_assignments import _condition_event


def test_condition_reports_calendar_digest_print_and_full_deficit_reminder(app, admin_client):
    event_id = _condition_event(app, maximum=1)
    now = datetime.now(timezone.utc)
    with app.app_context():
        participant = _make_user("integration@test.com", "Integration Participant", Role.MEMBER)
        event = db.session.get(Event, event_id)
        event.name = "Condition Integration"
        event.start_datetime, event.end_datetime = now + timedelta(hours=1), now + timedelta(hours=4)
        event.status = EventStatus.ASSIGNMENTS_CLOSED
        event.master_event.coordinator = participant
        event.assignments = [Assignment(user=participant)]
        event.qualification_requirements = [
            EventQualificationRequirement(
                qualification=Qualification(name="Missing Doctor", can_be_rp=True), minimum_count=1
            )
        ]
        db.session.commit()
        me_id, token = event.master_event_id, participant.ical_all_token
        counts, assignments = _spot_and_assignment_data([event_id], [event])
        assert counts[event_id] == (1, 1)
        assert len(assignments) == 1
        digest = UpcomingEventsBlock().collect(db.session, {"show_unfilled_only": True})
        assert digest["rows"][0]["staffing"].is_capacity_full
        assert not digest["rows"][0]["staffing"].is_staffing_sufficient
        book = generate_printout([event], "Test", None)
        assert "Podmínky" in book.sheetnames
        values = [c.value for row in book["Podmínky"] for c in row]
        assert "Missing Doctor" in values and "Integration Participant" in values
        assert run_send_reminders(db.session, now=now) == 1
        assert db.session.scalar(db.select(OutboxEmail).where(OutboxEmail.notification_type == "unfilled_reminder"))
    response = admin_client.get(f"/calendar/all/{token}.ics")
    cal = Calendar.from_ical(response.data)
    description = str(cal.walk("VEVENT")[0]["DESCRIPTION"])
    assert "Integration Participant" in description and "Missing Doctor: 0/1" in description
    html = admin_client.get(f"/reports/master-event/{me_id}").data.decode()
    assert "1 / 1 / 1" in html


def test_condition_notification_language_and_live_deficit(app):
    event_id = _condition_event(app)
    with app.test_request_context():
        event = db.session.get(Event, event_id)
        rows = [
            OutboxEmail(notification_type=kind, change_value="{}")
            for kind in ("assignment_confirmed", "assignment_released", "unfilled_reminder")
        ]
        section = _build_event_section(event, rows)
        html = render_template("email/event_batched.html", user_name="Member", event_sections=[section])
        assert "přihlášeni na akci" in html
        assert "odhlášeni z akce" in html
        assert "na pozici" not in html
        assert "nesplněné podmínky" in html
        assert "Chybí způsobilá zodpovědná osoba" in html
        assert _format_event_change_value("qualification_requirements", [["Doctor", 2]]) == "Doctor: 2"
