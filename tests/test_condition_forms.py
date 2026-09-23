import re

import pytest

from app.extensions import db
from app.models.assignment import Assignment
from app.models.event import Event, EventStatus, EventTemplate, EventType, StaffingMode
from app.models.qualification import Qualification
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


@pytest.mark.parametrize("is_template", [False, True])
def test_condition_requirement_order_survives_create_edit_and_copy(app, admin_client, is_template):
    data = _form(app)
    with app.app_context():
        qualifications = [Qualification(name=f"Ordered {i}", can_be_rp=True) for i in range(3)]
        db.session.add_all(qualifications)
        db.session.commit()
        order = [qualifications[i].id for i in (2, 0, 1)]
    data.update(requirement_qualification=[str(qid) for qid in order], requirement_count=["1"] * 3)
    prefix, model = ("/templates", EventTemplate) if is_template else ("/events", Event)
    assert admin_client.post(f"{prefix}/create", data=data).status_code == 302
    with app.app_context():
        owner = db.session.scalar(db.select(model))
        owner_id, version = owner.id, owner.version
        assert [r.qualification_id for r in owner.qualification_requirements] == order
    for expected_order in (order, list(reversed(order))):
        if expected_order != order:
            data["requirement_qualification"] = [str(qid) for qid in expected_order]
            assert (
                admin_client.post(f"{prefix}/{owner_id}/edit", data={**data, "version": str(version)}).status_code
                == 302
            )
        html = admin_client.get(f"{prefix}/{owner_id}/edit").data.decode()
        assert [int(qid) for qid in re.findall(r'<option value="(\d+)" selected>', html)] == expected_order
        with app.app_context():
            owner = db.session.get(model, owner_id)
            assert [r.qualification_id for r in owner.qualification_requirements] == expected_order
            names = [r.qualification.name for r in owner.qualification_requirements]
            if not is_template:
                assert [r.qualification.id for r in owner.staffing_summary.requirements] == expected_order
        html = admin_client.get(f"{prefix}/{owner_id}").data.decode()
        assert [html.index(f"{name}:") for name in names] == sorted(html.index(f"{name}:") for name in names)
    if is_template:
        html = admin_client.get(f"/events/create-from-template/{owner_id}").data.decode()
        assert [int(qid) for qid in re.findall(r'<option value="(\d+)" selected>', html)] == expected_order
        assert admin_client.post("/events/create", data={**data, "template_id": str(owner_id)}).status_code == 302
        with app.app_context():
            copied = db.session.scalar(db.select(Event))
            assert [r.qualification_id for r in copied.qualification_requirements] == expected_order
    else:
        response = admin_client.post(f"/master-events/{data['master_event_id']}/table/event/{owner_id}/clone")
        assert response.status_code == 200
        with app.app_context():
            copied = db.session.get(Event, response.json["new_event_id"])
            assert [r.qualification_id for r in copied.qualification_requirements] == expected_order


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


def test_condition_edit_corrects_capacity_below_participation(app, admin_client):
    data = _form(app)
    admin_client.post("/events/create", data=data)
    with app.app_context():
        event = db.session.scalar(db.select(Event))
        event_id = event.id
        user = db.session.scalar(db.select(UserAccount).where(UserAccount.email == "admin@test.com"))
        second = _make_user("capacity@test.com", "Second", Role.MEMBER)
        event.assignments.clear()
        event.assignments = [Assignment(user=user), Assignment(user=second)]
        event.status = EventStatus.ASSIGNMENTS_OPEN
        db.session.commit()
        version = event.version
    response = admin_client.post(
        f"/events/{event_id}/edit",
        data={**data, "maximum_participants": "1", "version": str(version)},
        follow_redirects=True,
    )
    assert "Maximum účastníků bylo zvýšeno z 1 na 2" in response.text
    assert "na akci je již přihlášeno 2 účastníků" in response.text
    with app.app_context():
        event = db.session.get(Event, event_id)
        assert event.maximum_participants == 2
        assert len(event.assignments) == 2
        assert event.staffing_summary.is_capacity_full


@pytest.mark.parametrize("is_template", [False, True])
@pytest.mark.parametrize("shared_hierarchy", [False, True])
def test_condition_forms_correct_capacity_for_hierarchies(app, admin_client, is_template, shared_hierarchy):
    data = _form(app, minimum_participants="0", maximum_participants="-1")
    with app.app_context():
        rp = db.session.get(Qualification, int(data["requirement_qualification"]))
        other = Qualification(name="Additional", parents=[rp] if shared_hierarchy else [])
        db.session.add(other)
        db.session.commit()
        data.update(requirement_qualification=[str(rp.id), str(other.id)], requirement_count=["2", "3"])
    required = 5 if shared_hierarchy else 3
    prefix, model = ("/templates", EventTemplate) if is_template else ("/events", Event)
    html = admin_client.get(f"{prefix}/create").text
    for field in ("minimum_participants", "maximum_participants"):
        input_tag = re.search(rf'<input[^>]*name="{field}"[^>]*>', html).group()
        assert 'type="number"' in input_tag and "required" in input_tag
        assert " min=" not in input_tag
    response = admin_client.post(f"{prefix}/create", data=data, follow_redirects=True)
    assert f"Minimum účastníků bylo zvýšeno z 0 na {required}" in response.text
    assert f"Maximum účastníků bylo zvýšeno z -1 na {required}" in response.text
    assert "minimum musí být alespoň 1" in response.text
    assert f"kvalifikační podmínky v jedné hierarchii vyžadují alespoň {required} účastníků" in response.text
    assert "maximum musí být nejméně rovné minimu" in response.text
    with app.app_context():
        owner = db.session.scalar(db.select(model))
        owner_id, version = owner.id, owner.version
        assert (owner.minimum_participants, owner.maximum_participants) == (required, required)
        assert [r.minimum_count for r in owner.qualification_requirements] == [2, 3]
    response = admin_client.post(
        f"{prefix}/{owner_id}/edit",
        data={**data, "minimum_participants": "7", "maximum_participants": "2", "version": str(version)},
        follow_redirects=True,
    )
    assert "Minimum účastníků bylo zvýšeno" not in response.text
    assert "Maximum účastníků bylo zvýšeno z 2 na 7" in response.text
    with app.app_context():
        owner = db.session.get(model, owner_id)
        assert (owner.minimum_participants, owner.maximum_participants) == (7, 7)
        version = owner.version
    # A valid resubmission neither adjusts capacity nor emits another warning.
    response = admin_client.post(
        f"{prefix}/{owner_id}/edit",
        data={**data, "minimum_participants": "7", "maximum_participants": "8", "version": str(version)},
        follow_redirects=True,
    )
    assert "bylo zvýšeno" not in response.text
    with app.app_context():
        owner = db.session.get(model, owner_id)
        assert (owner.minimum_participants, owner.maximum_participants) == (7, 8)


@pytest.mark.parametrize("is_template", [False, True])
@pytest.mark.parametrize("invalid", ["missing", "noninteger", "count", "qualification", "duplicate", "rp"])
def test_condition_capacity_correction_does_not_accept_invalid_form(app, admin_client, is_template, invalid):
    data = _form(app, minimum_participants="0", maximum_participants="0")
    if invalid == "missing":
        del data["minimum_participants"]
    elif invalid == "noninteger":
        data["maximum_participants"] = "1.5"
    elif invalid == "count":
        data["requirement_count"] = "0"
    elif invalid == "qualification":
        data["requirement_qualification"] = "-1"
    elif invalid == "duplicate":
        data["requirement_qualification"] = [data["requirement_qualification"]] * 2
        data["requirement_count"] = ["1", "1"]
    else:
        with app.app_context():
            db.session.get(Qualification, int(data["requirement_qualification"])).can_be_rp = False
            db.session.commit()
    prefix, model = ("/templates", EventTemplate) if is_template else ("/events", Event)
    response = admin_client.post(f"{prefix}/create", data=data)
    assert response.status_code == 200
    assert "bylo zvýšeno" not in response.text
    with app.app_context():
        assert not db.session.scalar(db.select(model))


def test_condition_edit_does_not_warn_about_unsaved_capacity(app, admin_client):
    data = _form(app)
    admin_client.post("/events/create", data=data, follow_redirects=True)
    with app.app_context():
        event = db.session.scalar(db.select(Event))
        event_id, version = event.id, event.version
    response = admin_client.post(
        f"/events/{event_id}/edit",
        data={**data, "minimum_participants": "0", "maximum_participants": "0", "name": "", "version": str(version)},
    )
    assert response.status_code == 200
    assert "bylo zvýšeno" not in response.text
    with app.app_context():
        event = db.session.get(Event, event_id)
        assert (event.minimum_participants, event.maximum_participants) == (1, 3)


def test_condition_create_rejects_forged_rp_and_plan(app, admin_client):
    data = _form(app)
    with app.app_context():
        user = db.session.scalar(db.select(UserAccount).where(UserAccount.email == "admin@test.com"))
        user_id = str(user.id)
    assert admin_client.post("/events/create", data={**data, "responsible_person_id": user_id}).status_code == 200
    assert admin_client.post("/events/create", data={**data, "requirement_count": "0"}).status_code == 200
    with app.app_context():
        assert not db.session.scalars(db.select(Event)).all()
