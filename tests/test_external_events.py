"""External event visibility and self-service authorization."""

from datetime import datetime, timedelta, timezone

import pytest
from icalendar import Calendar

from app.extensions import db
from app.external_collaborators import external_opportunities, external_opportunity
from app.models.assignment import Assignment
from app.models.audit import AuditLogEntry
from app.models.event import Event, EventSpot, EventStatus, StaffingMode
from app.models.qualification import Qualification
from app.models.role import Role
from app.models.settings import get_settings
from app.models.user import UserAccount
from tests.conftest import _login, _make_event_with_spot, _make_user
from tests.test_app_settings import _form


def make_opportunity(app, *, days=3, mode=StaffingMode.SPOTS, name="External opportunity"):
    event_id, spot_id = _make_event_with_spot(app, name=name)
    with app.app_context():
        event = db.session.get(Event, event_id)
        event.start_datetime = datetime.now(timezone.utc) + timedelta(days=days)
        event.end_datetime = event.start_datetime + timedelta(hours=4)
        if mode == StaffingMode.CONDITIONS:
            event.staffing_mode = mode
            event.minimum_participants, event.maximum_participants = 1, 3
            event.spots.clear()
        db.session.commit()
    return event_id, spot_id


@pytest.mark.parametrize(
    "offset,expected",
    [
        (timedelta(0), False),
        (timedelta(seconds=1), True),
        (timedelta(days=10, seconds=-1), True),
        (timedelta(days=10), False),
        (timedelta(days=11), False),
        (timedelta(seconds=-1), False),
    ],
)
def test_external_window_boundary(app, offset, expected):
    event_id, _ = make_opportunity(app)
    now = datetime(2030, 1, 1, tzinfo=timezone.utc)
    with app.app_context():
        event = db.session.get(Event, event_id)
        event.start_datetime = now + offset
        assert external_opportunity(event, now) is expected
        event.open_to_externals = True
        assert external_opportunity(event, now) is (offset > timedelta(0))


@pytest.mark.parametrize("status", list(EventStatus))
def test_override_does_not_bypass_lifecycle(app, status):
    event_id, _ = make_opportunity(app, days=20)
    with app.app_context():
        event = db.session.get(Event, event_id)
        event.open_to_externals = True
        event.status = status
        assert external_opportunity(event) is (status == EventStatus.ASSIGNMENTS_OPEN)
        event.archived = True
        assert not external_opportunity(event)


def test_spots_with_only_optional_vacancy_remain_available(app):
    event_id, spot_id = make_opportunity(app)
    with app.app_context():
        event = db.session.get(Event, event_id)
        member = _make_user("crew@test.com", "Crew", Role.MEMBER)
        event.spots.append(EventSpot(is_optional=True))
        db.session.add(Assignment(event_id=event.id, spot_id=spot_id, user_id=member.id))
        db.session.commit()
        assert event.is_sufficiently_staffed
        assert external_opportunity(event)
        second = _make_user("other@test.com", "Other", Role.MEMBER)
        db.session.add(Assignment(event_id=event.id, spot_id=event.spots[-1].id, user_id=second.id))
        db.session.commit()
        assert not external_opportunity(event)


@pytest.mark.parametrize("sufficient,full", [(True, False), (False, False), (False, True)])
def test_condition_opportunities_follow_requirements_and_capacity(app, sufficient, full):
    event_id, _ = make_opportunity(app, mode=StaffingMode.CONDITIONS)
    with app.app_context():
        event = db.session.get(Event, event_id)
        member = _make_user("crew@test.com", "Crew", Role.MEMBER)
        qualification = Qualification(name="RP", can_be_rp=True)
        member.qualifications.append(qualification)
        event.assignments.append(Assignment(user=member))
        if sufficient:
            event.responsible_person = member
        if full:
            event.maximum_participants = 1
        db.session.commit()
        assert external_opportunity(event) is (not sufficient and not full)


@pytest.mark.parametrize("mode", list(StaffingMode))
def test_external_can_claim_and_release_available_event(app, external_client, mode):
    event_id, spot_id = make_opportunity(app, mode=mode)
    claim_url = (
        f"/assignments/claim/{spot_id}" if mode == StaffingMode.SPOTS else f"/assignments/event/{event_id}/claim"
    )
    assert external_client.post(claim_url).status_code == 302
    with app.app_context():
        user = db.session.scalar(db.select(UserAccount).where(UserAccount.email == "external@test.com"))
        assignment = db.session.scalar(
            db.select(Assignment).where(Assignment.event_id == event_id, Assignment.user_id == user.id)
        )
        assert assignment is not None
        assignment_id = assignment.id
    assert external_client.post(f"/assignments/release/{assignment_id}").status_code == 302
    with app.app_context():
        assert db.session.get(Assignment, assignment_id) is None


@pytest.mark.parametrize("mode", list(StaffingMode))
@pytest.mark.parametrize("reason", ["early", "coordinated", "past", "closed", "archived", "full"])
def test_external_guessed_claim_cannot_bypass_policy(app, external_client, mode, reason):
    event_id, spot_id = make_opportunity(app, days=20 if reason == "early" else 3, mode=mode)
    with app.app_context():
        event = db.session.get(Event, event_id)
        if reason == "coordinated":
            event.master_event.coordinator = _make_user("coordinator@test.com", "Coordinator", Role.COORDINATOR)
        elif reason == "past":
            event.start_datetime = datetime.now(timezone.utc) - timedelta(minutes=1)
        elif reason == "closed":
            event.status = EventStatus.ASSIGNMENTS_CLOSED
        elif reason == "archived":
            event.archived = True
        elif reason == "full":
            member = _make_user("crew@test.com", "Crew", Role.MEMBER)
            event.assignments.append(Assignment(user=member, spot_id=spot_id if mode == StaffingMode.SPOTS else None))
            if mode == StaffingMode.CONDITIONS:
                event.maximum_participants = 1
        db.session.commit()
    claim_url = (
        f"/assignments/claim/{spot_id}" if mode == StaffingMode.SPOTS else f"/assignments/event/{event_id}/claim"
    )
    external_client.post(claim_url)
    with app.app_context():
        user = db.session.scalar(db.select(UserAccount).where(UserAccount.email == "external@test.com"))
        assert (
            db.session.scalar(
                db.select(Assignment).where(Assignment.event_id == event_id, Assignment.user_id == user.id)
            )
            is None
        )
    assert external_client.get(f"/events/{event_id}").status_code == 403


@pytest.mark.parametrize("surface", ["list", "filtered_list", "json", "dashboard", "ical"])
def test_external_browsing_hides_unavailable_events(app, external_client, surface):
    visible_id, _ = make_opportunity(app, name="Visible opportunity")
    hidden_id, _ = make_opportunity(app, days=20, name="Hidden internal event")
    with app.app_context():
        user = db.session.scalar(db.select(UserAccount).where(UserAccount.email == "external@test.com"))
        token = user.ical_all_token
        hidden_me_id = db.session.get(Event, hidden_id).master_event_id
    urls = {
        "list": "/events/",
        "filtered_list": (
            f"/events/?statuses=DRAFT,PUBLISHED,ASSIGNMENTS_OPEN,COMPLETED,CANCELLED"
            f"&archived=1&me_id={hidden_me_id}"
        ),
        "json": "/events/feed?archived=1",
        "dashboard": "/dashboard",
        "ical": f"/calendar/all/{token}.ics",
    }
    response = external_client.get(urls[surface])
    assert response.status_code == 200
    if surface == "json":
        assert {item["id"] for item in response.json} == {visible_id}
    elif surface == "ical":
        assert [str(event.get("summary")) for event in Calendar.from_ical(response.data).walk("VEVENT")] == [
            "Visible opportunity"
        ]
    else:
        assert b"Visible opportunity" in response.data
        assert b"Hidden internal event" not in response.data
        assert b"ME for Hidden internal event" not in response.data


@pytest.mark.parametrize("mode", list(StaffingMode))
@pytest.mark.parametrize("status", [EventStatus.ASSIGNMENTS_CLOSED, EventStatus.COMPLETED, EventStatus.CANCELLED])
def test_external_retains_own_event_details(app, external_client, mode, status):
    event_id, spot_id = make_opportunity(app, days=-1, mode=mode)
    with app.app_context():
        user = db.session.scalar(db.select(UserAccount).where(UserAccount.email == "external@test.com"))
        event = db.session.get(Event, event_id)
        event.status = status
        event.archived = status == EventStatus.CANCELLED
        db.session.add(
            Assignment(event_id=event_id, spot_id=spot_id if mode == StaffingMode.SPOTS else None, user_id=user.id)
        )
        db.session.commit()
    response = external_client.get(f"/events/{event_id}")
    assert response.status_code == 200
    assert b"External opportunity" in response.data


@pytest.mark.parametrize("mode", list(StaffingMode))
def test_external_rp_cannot_manage_other_assignments(app, external_client, mode):
    event_id, spot_id = make_opportunity(app, mode=mode)
    with app.app_context():
        user = db.session.scalar(db.select(UserAccount).where(UserAccount.email == "external@test.com"))
        user.qualifications.append(Qualification(name="External RP", can_be_rp=True))
        other = _make_user("other@test.com", "Other", Role.MEMBER)
        other_id = other.id
        event = db.session.get(Event, event_id)
        db.session.add(
            Assignment(event_id=event_id, spot_id=spot_id if mode == StaffingMode.SPOTS else None, user_id=user.id)
        )
        spare = EventSpot(event_id=event_id)
        if mode == StaffingMode.SPOTS:
            db.session.add(spare)
        db.session.commit()
        spare_id = spare.id
        assert user.is_rp_eligible()
        assert not event.user_can_manage_assignments(user)
    url = f"/assignments/assign/{spare_id}" if mode == StaffingMode.SPOTS else f"/assignments/event/{event_id}/assign"
    assert external_client.post(url, data={"user_id": str(other_id)}).status_code == 403


def test_internal_member_visibility_is_unchanged(app, member_client):
    event_id, _ = make_opportunity(app, days=20, name="Internal future event")
    assert member_client.get(f"/events/{event_id}").status_code == 200
    assert b"Internal future event" in member_client.get("/events/").data


def test_manual_early_opening_is_versioned_and_audited(app, admin_client):
    event_id, _ = make_opportunity(app, days=20)
    url = f"/events/{event_id}/external-opening"
    assert admin_client.post(url, data={"version": "1", "open_to_externals": "1"}).status_code == 302
    with app.app_context():
        event = db.session.get(Event, event_id)
        assert event.open_to_externals and event.version == 2
        assert external_opportunity(event)
        entry = db.session.scalar(
            db.select(AuditLogEntry).where(
                AuditLogEntry.entity_id == str(event_id), AuditLogEntry.entity_type == "Event"
            )
        )
        assert entry.changes_json == {"open_to_externals": [False, True]}
    admin_client.post(url, data={"version": "1", "open_to_externals": "0"})
    with app.app_context():
        assert db.session.get(Event, event_id).open_to_externals
    admin_client.post(url, data={"version": "2", "open_to_externals": "0"})
    with app.app_context():
        assert not db.session.get(Event, event_id).open_to_externals


def test_external_cannot_open_early(app, external_client):
    event_id, _ = make_opportunity(app, days=20)
    assert (
        external_client.post(
            f"/events/{event_id}/external-opening", data={"version": "1", "open_to_externals": "1"}
        ).status_code
        == 403
    )


def test_manual_opening_requires_csrf(app, admin_client):
    event_id, _ = make_opportunity(app, days=20)
    app.config["WTF_CSRF_ENABLED"] = True
    try:
        assert (
            admin_client.post(
                f"/events/{event_id}/external-opening", data={"version": "1", "open_to_externals": "1"}
            ).status_code
            == 400
        )
    finally:
        app.config["WTF_CSRF_ENABLED"] = False


def test_cloning_does_not_copy_early_opening(app, admin_client):
    event_id, _ = make_opportunity(app, days=20)
    with app.app_context():
        event = db.session.get(Event, event_id)
        event.open_to_externals = True
        me_id = event.master_event_id
        db.session.commit()
    response = admin_client.post(f"/master-events/{me_id}/table/event/{event_id}/clone")
    assert response.status_code == 200
    with app.app_context():
        clone = db.session.get(Event, response.json["new_event_id"])
        assert not clone.open_to_externals
        assert clone.status == EventStatus.DRAFT


@pytest.mark.parametrize("surface", ["profile", "report", "csv", "personal_calendar"])
def test_external_own_assignment_does_not_reveal_a_restored_draft(app, external_client, surface):
    event_id, spot_id = make_opportunity(app, name="Unpublished secret event")
    with app.app_context():
        event = db.session.get(Event, event_id)
        event.status = EventStatus.DRAFT
        user = db.session.scalar(db.select(UserAccount).where(UserAccount.email == "external@test.com"))
        db.session.add(Assignment(event_id=event_id, spot_id=spot_id, user_id=user.id))
        db.session.commit()
        user_id, token = user.id, user.ical_token
    urls = {
        "profile": "/users/profile",
        "report": f"/reports/user/{user_id}",
        "csv": f"/reports/user/{user_id}?format=csv",
        "personal_calendar": f"/calendar/{token}.ics",
    }
    response = external_client.get(urls[surface])
    assert response.status_code == 200
    assert b"Unpublished secret event" not in response.data
    assert external_client.get(f"/events/{event_id}").status_code == 403


@pytest.mark.parametrize("rp_qualified", [False, True])
def test_external_cannot_open_master_event_table(app, external_client, rp_qualified):
    event_id, spot_id = make_opportunity(app)
    with app.app_context():
        event = db.session.get(Event, event_id)
        me_id = event.master_event_id
        user = db.session.scalar(db.select(UserAccount).where(UserAccount.email == "external@test.com"))
        if rp_qualified:
            user.qualifications.append(Qualification(name="External RP", can_be_rp=True))
            db.session.add(Assignment(event_id=event_id, spot_id=spot_id, user_id=user.id))
            db.session.commit()
            assert user.is_rp_eligible()
    _make_event_with_spot(app, me_id=me_id, status=EventStatus.DRAFT, name="Hidden master-event draft")
    response = external_client.get(f"/master-events/{me_id}/table")
    assert response.status_code == 403
    assert b"Hidden master-event draft" not in response.data


@pytest.mark.parametrize("role", [Role.ADMIN, Role.COORDINATOR, Role.MEMBER, Role.VIEWER])
def test_internal_roles_keep_master_event_table_access(app, client, role):
    event_id, _ = make_opportunity(app, days=30)
    with app.app_context():
        _make_user("internal@test.com", "Internal User", role)
        me_id = db.session.get(Event, event_id).master_event_id
    _login(client, "internal@test.com")
    response = client.get(f"/master-events/{me_id}/table")
    assert response.status_code == 200
    assert b"External opportunity" in response.data


@pytest.mark.parametrize("action", ["assign", "unassign"])
def test_external_rp_cannot_use_master_event_table_mutations(app, external_client, action):
    event_id, spot_id = make_opportunity(app)
    with app.app_context():
        event = db.session.get(Event, event_id)
        me_id = event.master_event_id
        user = db.session.scalar(db.select(UserAccount).where(UserAccount.email == "external@test.com"))
        user.qualifications.append(Qualification(name="External RP", can_be_rp=True))
        assignment = Assignment(event_id=event_id, spot_id=spot_id, user_id=user.id)
        db.session.add(assignment)
        db.session.commit()
        assignment_id, user_id = assignment.id, user.id
    target_id = spot_id if action == "assign" else assignment_id
    response = external_client.post(
        f"/master-events/{me_id}/table/{action}/{target_id}", data={"user_id": str(user_id)}
    )
    assert response.status_code == 403
    with app.app_context():
        assert db.session.get(Assignment, assignment_id) is not None


@pytest.mark.parametrize("days", [3, 21])
def test_configured_window_matches_sql_and_python_policy(app, days):
    with app.app_context():
        get_settings().external_opportunity_days = days
        db.session.commit()
    now = datetime(2030, 1, 1, tzinfo=timezone.utc)
    ids = []
    for label in ["inside", "boundary", "outside"]:
        event_id, _ = make_opportunity(app, name=f"Configured {label}")
        ids.append(event_id)
    with app.app_context():
        inside, boundary, outside = (db.session.get(Event, event_id) for event_id in ids)
        inside.start_datetime = now + timedelta(days=days, seconds=-1)
        boundary.start_datetime = now + timedelta(days=days)
        outside.start_datetime = now + timedelta(days=days + 1)
        db.session.commit()
        assert external_opportunity(inside, now)
        assert not external_opportunity(boundary, now)
        assert not external_opportunity(outside, now)
        assert {event.id for event in external_opportunities(now)} == {inside.id}
        outside.open_to_externals = True
        db.session.commit()
        assert external_opportunity(outside, now)
        assert {event.id for event in external_opportunities(now)} == {inside.id, outside.id}


def test_saving_window_changes_policy_in_fresh_contexts(app, admin_client):
    event_id, _ = make_opportunity(app, days=20)
    for days, available in [(21, True), (3, False)]:
        assert (
            admin_client.post("/admin/settings/", data=_form("", external_opportunity_days=str(days))).status_code
            == 302
        )
        with app.app_context():
            event = db.session.get(Event, event_id)
            assert external_opportunity(event) is available
            assert (event_id in {candidate.id for candidate in external_opportunities()}) is available
