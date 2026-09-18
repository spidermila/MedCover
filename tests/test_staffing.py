import pytest

from app.extensions import db
from app.models.assignment import Assignment
from app.models.event import Event, EventQualificationRequirement, StaffingMode
from app.models.qualification import Qualification
from app.models.role import Role
from app.staffing import evaluate_staffing, user_helps_staffing, validate_condition_plan, validate_qualification_graph
from tests.conftest import _make_event_with_spot, _make_user


def _plan(app):
    event_id, _ = _make_event_with_spot(app)
    event = db.session.get(Event, event_id)
    event.spots.clear()
    event.staffing_mode = StaffingMode.CONDITIONS
    event.minimum_participants, event.maximum_participants = 2, 3
    doctor = Qualification(name="Doctor", can_be_rp=True)
    medic = Qualification(name="Medic", parents=[doctor])
    driver = Qualification(name="Driver")
    db.session.add_all([doctor, medic, driver])
    db.session.flush()
    event.qualification_requirements = [
        EventQualificationRequirement(qualification=q, minimum_count=1) for q in (doctor, medic, driver)
    ]
    db.session.commit()
    return event, doctor, medic, driver


def test_matching_reuses_users_only_across_independent_hierarchies(app):
    with app.app_context():
        event, doctor, medic, driver = _plan(app)
        first = _make_user("first@test.com", "First", Role.MEMBER)
        second = _make_user("second@test.com", "Second", Role.MEMBER)
        first.qualifications = [doctor, driver]
        second.qualifications = [doctor]
        event.assignments.append(Assignment(user=first))
        event.responsible_person_id = first.id
        db.session.commit()
        before = evaluate_staffing(event)
        assert before.participant_count == 1
        assert before.free_capacity == 2
        assert sum(r.covered for r in before.requirements) == 2
        assert sum(r.deficit for r in before.requirements) == 1
        assert user_helps_staffing(event, second)
        assert not user_helps_staffing(event, first)
        event.assignments.append(Assignment(user=second))
        db.session.commit()
        summary = evaluate_staffing(event)
        assert summary.is_staffing_sufficient
        assert all(r.covered == 1 for r in summary.requirements)
        assert [r.participants for r in summary.requirements] == [
            r.participants for r in evaluate_staffing(event).requirements
        ]
        medical_users = [
            u.id for r in summary.requirements if r.qualification.id in {doctor.id, medic.id} for u in r.participants
        ]
        assert len(set(medical_users)) == 2
        event.responsible_person_id = None
        assert not evaluate_staffing(event).is_staffing_sufficient


@pytest.mark.parametrize("case", ["minimum", "maximum", "count", "duplicate", "hierarchy", "rp", "capacity", "missing"])
def test_invalid_plans_are_rejected(app, case):
    with app.app_context():
        event, doctor, medic, driver = _plan(app)
        minimum, maximum, current = 2, 3, 0
        requirements = [(doctor.id, 1), (medic.id, 1), (driver.id, 1)]
        if case == "minimum":
            minimum = 0
        elif case == "maximum":
            maximum = 1
        elif case == "count":
            requirements[0] = (doctor.id, 0)
        elif case == "duplicate":
            requirements.append((doctor.id, 1))
        elif case == "hierarchy":
            minimum = 1
        elif case == "rp":
            requirements = [(driver.id, 1)]
        elif case == "capacity":
            current = 4
        else:
            requirements = [(-1, 1)]
        with pytest.raises(ValueError):
            validate_condition_plan(minimum, maximum, requirements, participant_count=current)
        validate_condition_plan(2, 3, [(doctor.id, 1), (medic.id, 1), (driver.id, 2)])


def test_graph_cycle_and_weak_components():
    assert validate_qualification_graph({1: set(), 2: {1}, 3: set(), 4: {2, 3}}) == {1: 1, 2: 1, 3: 1, 4: 1}
    with pytest.raises(ValueError, match="cyklus"):
        validate_qualification_graph({1: {2}, 2: {1}})
    with pytest.raises(ValueError, match="cyklus"):
        validate_qualification_graph({1: {1}})
