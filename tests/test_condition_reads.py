from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app.extensions import db
from app.mail import flush_and_notify_archived
from app.models.assignment import Assignment
from app.models.event import Event, EventStatus, StaffingMode
from app.models.role import Role
from app.printout_generator import generate_printout
from app.queries import conflicting_events_for_users
from app.routes.reports import _spot_and_assignment_data, _work_summary_data
from app.work_report_generator import _fetch_events_for_month
from tests.conftest import _login, _make_event_with_spot, _make_user


@pytest.mark.parametrize("mode", list(StaffingMode))
def test_shared_participation_reads(app, client, mode):
    event_id, spot_id = _make_event_with_spot(app, name="Shared participation")
    now = datetime.now(timezone.utc)
    with app.app_context():
        user = _make_user("participant@test.com", "Condition Participant", Role.ADMIN)
        user_id, token = user.id, user.ical_token
        event = db.session.get(Event, event_id)
        event.start_datetime, event.end_datetime = now + timedelta(days=1), now + timedelta(days=1, hours=4)
        if mode == StaffingMode.CONDITIONS:
            event.staffing_mode = mode
            event.minimum_participants, event.maximum_participants = 1, 3
            event.spots.clear()
        a = Assignment(event_id=event_id, spot_id=spot_id if mode == StaffingMode.SPOTS else None, user_id=user.id)
        db.session.add(a)
        db.session.commit()
        assignment_id = a.id
        assert (
            conflicting_events_for_users([user.id], event.start_datetime, event.end_datetime)[user.id][0]["id"]
            == event_id
        )
        _, pairs = _spot_and_assignment_data([event_id], [event])
        assert [a.id for a, _ in pairs] == [assignment_id]
        workbook = generate_printout([event], "Test", None)
        for sheet in workbook:
            assert any("Condition Participant" in str(c.value) for row in sheet for c in row)
    _login(client, "participant@test.com")
    for url in ("/", "/users/profile", f"/calendar/{token}.ics", f"/reports/user/{user_id}"):
        response = client.get(url, follow_redirects=True)
        assert response.status_code == 200
        assert b"Shared participation" in response.data
    with app.app_context():
        event = db.session.get(Event, event_id)
        event.status = EventStatus.COMPLETED
        event.paid = True
        event.start_datetime, event.end_datetime = now - timedelta(days=1), now - timedelta(days=1) + timedelta(hours=4)
        db.session.commit()
        groups = _work_summary_data(now - timedelta(days=3), now)
        assert any(g.total.user_name == "Condition Participant" for g in groups)
        day_data = _fetch_events_for_month(str(user_id), event.start_datetime.year, event.start_datetime.month)
        assert event.name in day_data[event.start_datetime.day][1]
    response = client.get(f"/debriefing/{assignment_id}")
    assert response.status_code == 200
    assert b"Shared participation" in response.data
    with app.app_context(), patch("app.mail.send_event_archived") as notify:
        event = db.session.get(Event, event_id)
        event.archived = True
        flush_and_notify_archived(event)
        assert any(call.args[0].id == user_id for call in notify.call_args_list)
