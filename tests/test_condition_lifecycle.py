import pytest
from sqlalchemy import event as sa_event

from app.extensions import db
from app.models.assignment import Assignment
from app.models.event import (
    Event,
    EventQualificationRequirement,
    EventStatus,
    EventTemplate,
    EventTemplateQualificationRequirement,
)
from app.models.qualification import Qualification
from app.models.role import Role
from app.staffing import evaluate_staffing, qualification_graph
from tests.conftest import _make_user
from tests.test_condition_assignments import _condition_event


@pytest.mark.parametrize("parent_kind", ["self", "descendant", "invalid", "deleted"])
def test_qualification_edit_rejects_invalid_graph(app, admin_client, parent_kind):
    with app.app_context():
        ancestor = Qualification(name="Ancestor")
        descendant = Qualification(name="Descendant", parents=[ancestor])
        deleted = Qualification(name="Deleted", is_deleted=True)
        db.session.add_all([ancestor, descendant, deleted])
        db.session.commit()
        qid = ancestor.id
        parent = {"self": str(qid), "descendant": str(descendant.id), "invalid": "bad", "deleted": str(deleted.id)}[
            parent_kind
        ]
    response = admin_client.post(f"/qualifications/{qid}/edit", data={"name": "Changed", "parent_ids": parent})
    assert response.status_code == 200
    with app.app_context():
        qualification = db.session.get(Qualification, qid)
        assert qualification.name == "Ancestor"
        assert not qualification.parents


def test_qualification_create_validates_parents(app, admin_client):
    assert admin_client.post("/qualifications/create", data={"name": "Invalid", "parent_ids": "bad"}).status_code == 200
    with app.app_context():
        assert db.session.scalar(db.select(Qualification).where(Qualification.name == "Invalid")) is None


@pytest.mark.parametrize("fixed_status", [EventStatus.COMPLETED, EventStatus.CANCELLED])
def test_condition_qualification_delete_guards_and_historical_tombstone(app, admin_client, fixed_status):
    event_id = _condition_event(app)
    with app.app_context():
        q = Qualification(name="Protected RP", can_be_rp=True)
        event = db.session.get(Event, event_id)
        event.qualification_requirements = [EventQualificationRequirement(qualification=q, minimum_count=1)]
        template = EventTemplate(
            name="Protected template",
            minimum_participants=1,
            maximum_participants=2,
            qualification_requirements=[EventTemplateQualificationRequirement(qualification=q, minimum_count=1)],
        )
        db.session.add(template)
        db.session.commit()
        qid, tid = q.id, template.id
    response = admin_client.get(f"/qualifications/{qid}/delete")
    assert f"/events/{event_id}".encode() in response.data
    assert f"/templates/{tid}/edit".encode() in response.data
    assert "Potvrdit smazání".encode() not in response.data
    assert admin_client.post(f"/qualifications/{qid}/delete").status_code == 302
    with app.app_context():
        assert not db.session.get(Qualification, qid).is_deleted
        db.session.get(Event, event_id).status = fixed_status
        db.session.commit()
    # Template alone still blocks deletion.
    admin_client.post(f"/qualifications/{qid}/delete")
    with app.app_context():
        assert not db.session.get(Qualification, qid).is_deleted
        db.session.delete(db.session.get(EventTemplate, tid))
        db.session.commit()
    admin_client.post(f"/qualifications/{qid}/delete")
    with app.app_context():
        assert db.session.get(Qualification, qid).is_deleted
        assert db.session.get(Event, event_id).qualification_requirements[0].qualification_id == qid


def test_rp_flag_changes_reassign_then_clear_responsible_person(app, admin_client):
    event_id = _condition_event(app)
    with app.app_context():
        first = _make_user("first-rp@test.com", "First", Role.MEMBER)
        second = _make_user("second-rp@test.com", "Second", Role.MEMBER)
        q1 = Qualification(name="First RP", can_be_rp=True)
        q2 = Qualification(name="Second RP", can_be_rp=True)
        first.qualifications, second.qualifications = [q1], [q2]
        event = db.session.get(Event, event_id)
        event.assignments = [Assignment(user=first), Assignment(user=second)]
        event.responsible_person_id = first.id
        db.session.commit()
        q1id, q2id, second_id = q1.id, q2.id, second.id
    admin_client.post(f"/qualifications/{q1id}/edit", data={"name": "First RP"})
    with app.app_context():
        assert db.session.get(Event, event_id).responsible_person_id == second_id
    admin_client.post(f"/qualifications/{q2id}/delete")
    with app.app_context():
        assert db.session.get(Event, event_id).responsible_person_id is None


@pytest.mark.parametrize("request_context", [False, True])
def test_staffing_batches_queries_and_invalidates_graph(app, request_context):
    event_ids = [_condition_event(app)]
    with app.app_context():
        source = db.session.get(Event, event_ids[0])
        for index in range(3):
            event = Event(
                name=f"Batch event {index}",
                master_event_id=source.master_event_id,
                staffing_mode=source.staffing_mode,
                minimum_participants=1,
                maximum_participants=2,
                status=source.status,
                start_datetime=source.start_datetime,
                end_datetime=source.end_datetime,
            )
            db.session.add(event)
            db.session.flush()
            event_ids.append(event.id)
        db.session.commit()
        q = Qualification(name="Batch RP", can_be_rp=True)
        for index, event_id in enumerate(event_ids):
            user = _make_user(f"batch{index}@test.com", f"Batch {index}", Role.MEMBER)
            user.qualifications = [q]
            event = db.session.get(Event, event_id)
            event.qualification_requirements = [EventQualificationRequirement(qualification=q, minimum_count=1)]
            event.assignments = [Assignment(user=user)]
            event.responsible_person_id = user.id
        db.session.commit()
        qid = q.id
    context = app.test_request_context() if request_context else app.app_context()
    with context:
        events = db.session.scalars(db.select(Event).where(Event.id.in_(event_ids))).all()
        qualification_graph()  # One batch per request/background context.
        statements = []

        def record(_conn, _cursor, statement, _parameters, _context, _many):
            statements.append(statement)

        sa_event.listen(db.engine, "before_cursor_execute", record)
        try:
            assert all(evaluate_staffing(event).is_staffing_sufficient for event in events)
        finally:
            sa_event.remove(db.engine, "before_cursor_execute", record)
        assert statements == [], "Evaluating loaded event lists must not query per event or participant"
        q = db.session.get(Qualification, qid)
        old = qualification_graph()
        events[0].description = "Unrelated write must not reload the graph for every event"
        db.session.flush()
        assert qualification_graph() is old
        parent = Qualification(name="New parent")
        q.parents.append(parent)
        db.session.flush()
        assert parent.id in qualification_graph().fillers(qid)
        assert qualification_graph() is not old
        db.session.rollback()
        assert qualification_graph().parents[qid] == set()


def test_split_full_condition_event_preserves_capacity_closure(app, admin_client):
    event_id = _condition_event(app, maximum=1)
    with app.app_context():
        user = _make_user("split-full@test.com", "Full participant", Role.MEMBER)
        event = db.session.get(Event, event_id)
        event.assignments = [Assignment(user=user)]
        event.status = EventStatus.ASSIGNMENTS_CLOSED
        event.capacity_closed = True
        split_time = event.start_datetime + (event.end_datetime - event.start_datetime) / 2
        db.session.commit()
    response = admin_client.post(f"/events/{event_id}/split", data={"split_datetime": split_time.isoformat()})
    assert response.status_code == 302
    with app.app_context():
        halves = db.session.scalars(db.select(Event)).all()
        assert len(halves) == 2
        assert all(event.status == EventStatus.ASSIGNMENTS_CLOSED and event.capacity_closed for event in halves)
        assert all(len(event.assignments) == 1 for event in halves)
