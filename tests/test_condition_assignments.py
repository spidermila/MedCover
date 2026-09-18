from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch

import pytest
from sqlalchemy import event as sa_event

from app.extensions import db
from app.models.assignment import Assignment
from app.models.event import Event, EventStatus, StaffingMode
from app.models.qualification import Qualification
from app.models.role import Role
from app.models.user import UserAccount
from app.routes.assignments import do_assign_event, do_unassign_user
from app.routes.users import _apply_qualification_update
from tests.conftest import _login, _make_event_with_spot, _make_user


def _condition_event(app, maximum=2):
    event_id, _ = _make_event_with_spot(app)
    with app.app_context():
        event = db.session.get(Event, event_id)
        event.spots.clear()
        event.staffing_mode = StaffingMode.CONDITIONS
        event.minimum_participants, event.maximum_participants = 1, maximum
        db.session.commit()
    return event_id


def test_capacity_claim_release_rp_and_qualification_change(app, client):
    event_id = _condition_event(app)
    with app.app_context():
        first = _make_user("first@test.com", "First", Role.MEMBER)
        second = _make_user("second@test.com", "Second", Role.MEMBER)
        third = _make_user("third@test.com", "Third", Role.MEMBER)
        qualification = Qualification(name="RP", can_be_rp=True)
        first.qualifications = second.qualifications = [qualification]
        db.session.commit()
        first_id, second_id, third_id = first.id, second.id, third.id
    _login(client, "first@test.com")
    assert client.post(f"/assignments/event/{event_id}/claim").status_code == 302
    with app.app_context(), patch("app.routes.assignments.audit"):
        event = db.session.get(Event, event_id)
        assert event.responsible_person_id == first_id
        second = db.session.get(UserAccount, second_id)
        result = do_assign_event(event_id, second, second, self_claim=True)
        assert result.ok
        second_assignment_id = result.assignment.id
        assert event.status == EventStatus.ASSIGNMENTS_CLOSED
        third = db.session.get(UserAccount, third_id)
        assert not do_assign_event(event_id, third, third, self_claim=True).ok
        assert not do_assign_event(event_id, second, second, self_claim=True).ok
        assert len(event.assignments) == 2
        # Manual choice remains restricted to eligible attendees.
        db.session.rollback()
    _login(client, "first@test.com")
    with app.test_request_context():
        with patch("app.routes.users.audit"), patch("app.routes.assignments.audit"):
            first = db.session.get(UserAccount, first_id)
            assert _apply_qualification_update(first, [])
            db.session.commit()
            event = db.session.get(Event, event_id)
            assert event.responsible_person_id == second_id
            result = do_unassign_user(db.session.get(Assignment, second_assignment_id))
            assert result.ok
            assert event.responsible_person_id is None
            assert event.status == EventStatus.ASSIGNMENTS_OPEN
            # Missing qualifications/RP never prevent joining.
            third = db.session.get(UserAccount, third_id)
            assert do_assign_event(event_id, third, third, self_claim=True).ok


def test_last_seat_is_serialized_on_sql_server(app, monkeypatch):
    event_id = _condition_event(app, maximum=1)
    with app.app_context():
        ids = [_make_user(f"race{i}@test.com", f"Race {i}", Role.MEMBER).id for i in range(2)]
        engine = db.engine
    barrier = Barrier(2)
    captured = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        captured.append(statement)

    monkeypatch.setattr("app.routes.assignments.audit", lambda *a, **kw: None)
    monkeypatch.setattr("app.routes.assignments.mailer.send_assignment_confirmed", lambda *a, **kw: None)

    def claim(user_id):
        with app.app_context():
            user = db.session.get(UserAccount, user_id)
            # Deliberately preload stale state before either contender writes.
            db.session.get(Event, event_id)
            barrier.wait(timeout=10)
            return do_assign_event(event_id, user, user, self_claim=True).ok

    sa_event.listen(engine, "before_cursor_execute", capture)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, ids))
    finally:
        sa_event.remove(engine, "before_cursor_execute", capture)
    assert sorted(results) == [False, True]
    assert any("UPDLOCK, HOLDLOCK, ROWLOCK" in statement for statement in captured)
    with app.app_context():
        assert len(db.session.get(Event, event_id).assignments) == 1


@pytest.mark.parametrize("state", [EventStatus.DRAFT, EventStatus.COMPLETED, EventStatus.CANCELLED])
def test_condition_claim_rejects_closed_lifecycle(app, member_client, state):
    event_id = _condition_event(app)
    with app.app_context():
        event = db.session.get(Event, event_id)
        event.status = state
        db.session.commit()
    member_client.post(f"/assignments/event/{event_id}/claim")
    with app.app_context():
        assert not db.session.get(Event, event_id).assignments


def test_condition_permissions_conflict_warning_and_manual_rp(app, admin_client):
    event_id = _condition_event(app, maximum=3)
    _, legacy_spot = _make_event_with_spot(app, name="Overlapping legacy")
    with app.app_context():
        user = _make_user("qualified@test.com", "Qualified", Role.MEMBER)
        outside = _make_user("outside@test.com", "Outside", Role.MEMBER)
        qualification = Qualification(name="RP", can_be_rp=True)
        user.qualifications = outside.qualifications = [qualification]
        db.session.add(Assignment(spot_id=legacy_spot, user=user))
        db.session.commit()
        user_id, outside_id = user.id, outside.id
    response = admin_client.post(
        f"/assignments/event/{event_id}/assign", data={"user_id": str(user_id)}, follow_redirects=True
    )
    assert "překrývající se akci" in response.data.decode()
    admin_client.post(f"/events/{event_id}/set_rp", data={"user_id": str(outside_id)})
    with app.app_context():
        assert db.session.get(Event, event_id).responsible_person_id == user_id
    admin_client.post(f"/assignments/event/{event_id}/assign", data={"user_id": str(outside_id)})
    admin_client.post(f"/events/{event_id}/set_rp", data={"user_id": str(outside_id)})
    with app.app_context():
        event = db.session.get(Event, event_id)
        assert event.responsible_person_id == outside_id
        event.status = EventStatus.ASSIGNMENTS_CLOSED
        assignment_id = event.assignments[0].id
        db.session.commit()
    admin_client.post(f"/assignments/unassign/{assignment_id}")
    with app.app_context():
        assert db.session.get(Event, event_id).status == EventStatus.ASSIGNMENTS_CLOSED
