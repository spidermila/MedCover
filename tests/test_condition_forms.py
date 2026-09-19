import pytest

from app.extensions import db
from app.models.assignment import Assignment
from app.models.event import Event, EventStatus, EventType, StaffingMode
from app.models.role import Role
from app.models.user import UserAccount
from tests.conftest import _make_master_event, _make_rp_qual, _make_user


def _form(app, **extra):
    return {
        "name": "Condition form",
        "master_event_id": str(_make_master_event(app)),
        "start_datetime": "2030-06-01T10:00",
        "end_datetime": "2030-06-01T18:00",
        "minimum_participants": "1",
        "maximum_participants": "3",
        "requirement_qualification": str(_make_rp_qual(app)),
        "requirement_count": "1",
        **extra,
    }


@pytest.mark.parametrize("event_type", list(EventType))
def test_condition_create_edit_clone_split(app, admin_client, event_type):
    data = _form(app, event_type=event_type.name)
    assert admin_client.post("/events/create", data=data).status_code == 302
    with app.app_context():
        event = db.session.scalar(db.select(Event))
        event_id, me_id = event.id, event.master_event_id
        assert event.staffing_mode == StaffingMode.CONDITIONS
        assert not event.spots
        assert event.event_type == event_type
        event.status = EventStatus.ASSIGNMENTS_OPEN
        user = db.session.scalar(db.select(UserAccount).where(UserAccount.email == "admin@test.com"))
        user.qualifications = [event.qualification_requirements[0].qualification]
        event.assignments = [Assignment(user=user)]
        event.responsible_person_id = user.id
        db.session.commit()
        version = event.version
    html = admin_client.get(f"/events/{event_id}/edit").data.decode()
    assert 'id="conditionRows"' in html and 'id="spotRows"' not in html
    assert (
        admin_client.post(
            f"/events/{event_id}/edit",
            data={**data, "version": str(version), "maximum_participants": "2", "staffing_mode": "SPOTS"},
        ).status_code
        == 302
    )
    response = admin_client.post(f"/master-events/{me_id}/table/event/{event_id}/clone")
    assert response.status_code == 200
    clone_id = response.json["new_event_id"]
    with app.app_context():
        clone = db.session.get(Event, clone_id)
        assert clone.staffing_mode == StaffingMode.CONDITIONS
        assert clone.maximum_participants == 2
        assert len(clone.qualification_requirements) == 1
        assert not clone.assignments and clone.responsible_person_id is None
    assert (
        admin_client.post(f"/events/{event_id}/split", data={"split_datetime": "2030-06-01T14:00"}).status_code == 302
    )
    with app.app_context():
        halves = db.session.scalars(db.select(Event).where(Event.name.like("%/2"))).all()
        assert len(halves) == 2
        assert all(e.staffing_mode == StaffingMode.CONDITIONS and len(e.assignments) == 1 for e in halves)
        assert all(e.maximum_participants == 2 and len(e.qualification_requirements) == 1 for e in halves)


def test_condition_edit_cannot_reduce_capacity_below_participation(app, admin_client):
    data = _form(app)
    admin_client.post("/events/create", data=data)
    with app.app_context():
        event = db.session.scalar(db.select(Event))
        event_id = event.id
        user = db.session.scalar(db.select(UserAccount).where(UserAccount.email == "admin@test.com"))
        second = _make_user("capacity@test.com", "Second", Role.MEMBER)
        event.assignments.clear()
        event.assignments = [Assignment(user=user), Assignment(user=second)]
        db.session.commit()
    response = admin_client.post(f"/events/{event_id}/edit", data={**data, "maximum_participants": "1"})
    assert response.status_code == 200
    with app.app_context():
        assert db.session.get(Event, event_id).maximum_participants == 3


def test_condition_create_rejects_forged_rp_and_plan(app, admin_client):
    data = _form(app)
    with app.app_context():
        user = db.session.scalar(db.select(UserAccount).where(UserAccount.email == "admin@test.com"))
        user_id = str(user.id)
    assert admin_client.post("/events/create", data={**data, "responsible_person_id": user_id}).status_code == 200
    assert admin_client.post("/events/create", data={**data, "requirement_count": "0"}).status_code == 200
    with app.app_context():
        assert not db.session.scalars(db.select(Event)).all()
