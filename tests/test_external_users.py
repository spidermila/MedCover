"""External users: they see only the events they take part in (assigned or
responsible person), and the people of those events."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from app.extensions import db
from app.mail import user_can_receive_notification
from app.models.assignment import Assignment
from app.models.digest import get_digest_schedule
from app.models.equipment import EquipmentItem, EquipmentType
from app.models.event import Event, EventStatus
from app.models.qualification import Qualification
from app.models.role import Role
from app.models.settings import get_settings
from app.models.user import UserAccount
from app.scheduler_tasks import run_admin_digest
from tests.conftest import _login, _make_event_in_status, _make_event_with_spot, _make_rp_qual, _make_user


@pytest.fixture
def world(app):
    """An external user assigned to one event and responsible for another, a
    member on the first event, and events the external does not take part in."""
    with app.app_context():
        ext = _make_user("ext@test.com", "Externí Eva", Role.EXTERNAL)
        member = _make_user("member@test.com", "Členka Jana", Role.MEMBER)
        stranger = _make_user("stranger@test.com", "Cizí Petr", Role.MEMBER)
        ids = {"ext": ext.id, "member": member.id, "stranger": stranger.id}
    ids["assigned"], _ = _make_event_with_spot(app, name="Přiřazená akce")
    ids["rp"] = _make_event_in_status(app, EventStatus.ASSIGNMENTS_CLOSED, name="Akce se ZO")
    ids["other"] = _make_event_in_status(app, EventStatus.ASSIGNMENTS_OPEN, name="Cizí akce")
    ids["draft"] = _make_event_in_status(app, EventStatus.DRAFT, name="Rozpracovaná akce")
    with app.app_context():
        for event_id, user_id in (
            (ids["assigned"], ids["ext"]),
            (ids["assigned"], ids["member"]),
            (ids["draft"], ids["ext"]),
            (ids["other"], ids["stranger"]),
        ):
            db.session.add(Assignment(event_id=event_id, user_id=user_id))
        db.session.get(Event, ids["rp"]).responsible_person_id = ids["ext"]
        db.session.commit()
    return ids


@pytest.fixture
def ext_client(client, world):
    _login(client, "ext@test.com")
    return client


def test_event_list_shows_only_own_published_events(ext_client, world):
    page = ext_client.get("/events/?statuses=" + ",".join(s.name for s in EventStatus)).get_data(as_text=True)
    assert "Přiřazená akce" in page and "Akce se ZO" in page
    assert "Cizí akce" not in page and "Rozpracovaná akce" not in page
    assert "Nadřazené akce" not in page


def test_calendar_feed_shows_only_own_published_events(ext_client, world):
    titles = {item["title"] for item in ext_client.get("/events/feed").get_json()}
    assert titles == {"Přiřazená akce", "Akce se ZO"}


def test_event_detail_only_for_own_published_events(ext_client, world):
    assert ext_client.get(f"/events/{world['assigned']}").status_code == 200
    assert ext_client.get(f"/events/{world['rp']}").status_code == 200
    assert ext_client.get(f"/events/{world['other']}").status_code == 403
    assert ext_client.get(f"/events/{world['draft']}").status_code == 403


def test_external_sees_people_of_shared_events_only(ext_client, world):
    page = ext_client.get(f"/users/{world['member']}")
    assert page.status_code == 200 and "member@test.com" in page.get_data(as_text=True)
    assert ext_client.get(f"/users/{world['stranger']}").status_code == 403
    assert ext_client.get("/users/").status_code == 403


def test_people_of_a_shared_draft_stay_hidden(app, ext_client, world):
    with app.app_context():
        db.session.add(Assignment(event_id=world["draft"], user_id=world["stranger"]))
        db.session.commit()
    assert ext_client.get(f"/users/{world['stranger']}").status_code == 403


def test_no_event_plan_for_external(ext_client, world):
    assert ext_client.get("/master-events/").status_code == 403
    assert 'id="ical-all"' not in ext_client.get("/users/profile").get_data(as_text=True)
    assert ext_client.get("/work-report/").status_code == 200


def test_all_events_calendar_link_refused_for_external(app, client, world):
    with app.app_context():
        token = db.session.get(UserAccount, world["ext"]).regenerate_ical_all_token()
        db.session.commit()
    assert client.get(f"/calendar/all/{token}.ics").status_code == 404


def test_external_may_be_responsible_person_but_not_assign_others(app, world):
    with app.app_context():
        ext = db.session.get(UserAccount, world["ext"])
        ext.qualifications = [db.session.get(Qualification, _make_rp_qual(app))]
        db.session.commit()
        assert ext.is_rp_eligible()
        assert not db.session.get(Event, world["assigned"]).user_can_manage_assignments(ext)


def test_external_kind_is_limited_whatever_the_roles(app, world):
    with app.app_context():
        ext = db.session.get(UserAccount, world["ext"])
        ext.roles = [db.session.scalar(db.select(Role).where(Role.name == Role.MEMBER))]
        ext.kind = "external"
        db.session.commit()
        assert not ext.has_permission("event.view") and not ext.has_permission("event.assign_own")
        assert ext.has_permission("event.view_assigned")
        assert not user_can_receive_notification(ext, "event_published")
        assert user_can_receive_notification(ext, "assignment")


def test_external_gets_only_notifications_about_own_events(app, world):
    with app.app_context():
        ext = db.session.get(UserAccount, world["ext"])
        assert user_can_receive_notification(ext, "event_changed")
        assert not user_can_receive_notification(ext, "assignments_opened")
        assert not user_can_receive_notification(ext, "unfilled_reminder")  # cannot fill spots


def test_external_fills_the_debriefing_of_a_completed_event(app, ext_client, world):
    with app.app_context():
        event = db.session.get(Event, world["assigned"])
        event.status = EventStatus.COMPLETED
        event.end_datetime = datetime(2020, 1, 1, tzinfo=timezone.utc)
        assignment_id = db.session.scalar(
            db.select(Assignment.id).where(Assignment.event_id == event.id, Assignment.user_id == world["ext"])
        )
        db.session.commit()
    assert ext_client.get(f"/debriefing/{assignment_id}").status_code == 200


def test_own_calendar_link_and_statistics_leave_out_drafts(app, ext_client, world):
    with app.app_context():
        token = db.session.get(UserAccount, world["ext"]).regenerate_ical_token()
        db.session.commit()
    ical = ext_client.get(f"/calendar/{token}.ics").get_data(as_text=True)
    assert "Přiřazená akce" in ical and "Rozpracovaná akce" not in ical
    page = ext_client.get(f"/reports/user/{world['ext']}").get_data(as_text=True)
    assert "Přiřazená akce" in page and "Rozpracovaná akce" not in page and "Zpět na přehledy" not in page


def test_external_cannot_leave_an_event(app, ext_client, world):
    with app.app_context():
        assignment_id = db.session.scalar(
            db.select(Assignment.id).where(Assignment.event_id == world["assigned"], Assignment.user_id == world["ext"])
        )
    assert "Odhlásit se" not in ext_client.get(f"/events/{world['assigned']}").get_data(as_text=True)
    assert ext_client.post(f"/assignments/release/{assignment_id}").status_code == 403


def test_all_events_calendar_link_still_works_for_other_roles(app, client, world):
    with app.app_context():
        manager = _make_user("dm@test.com", "Vedoucí Debriefingu", Role.DEBRIEFING_MANAGER)
        token = manager.regenerate_ical_all_token()
        db.session.commit()
    assert client.get(f"/calendar/all/{token}.ics").status_code == 200


def test_master_event_names_stay_hidden(app, ext_client, world):
    with app.app_context():
        me_id = db.session.get(Event, world["other"]).master_event_id
    assert "ME for Cizí akce" not in ext_client.get(f"/events/?me_id={me_id}").get_data(as_text=True)


def test_own_calendar_link_includes_events_as_responsible_person(app, ext_client, world):
    with app.app_context():
        token = db.session.get(UserAccount, world["ext"]).regenerate_ical_token()
        db.session.commit()
    assert "Akce se ZO" in ext_client.get(f"/calendar/{token}.ics").get_data(as_text=True)


def test_colleague_profile_shows_contact_not_roles_or_back_link(ext_client, world):
    page = ext_client.get(f"/users/{world['member']}").get_data(as_text=True)
    assert "member@test.com" in page and "Zpět na uživatele" not in page and "Kvalifikace" not in page


def test_viewer_still_leaves_an_event(app, client, world):
    with app.app_context():
        viewer = _make_user("viewer@test.com", "Divák Karel", Role.VIEWER)
        assignment = Assignment(event_id=world["other"], user_id=viewer.id)
        db.session.add(assignment)
        db.session.commit()
        assignment_id = assignment.id
    _login(client, "viewer@test.com")
    assert client.post(f"/assignments/release/{assignment_id}").status_code == 302


def test_external_with_admin_role_gets_no_admin_digest(app, world):
    with app.app_context():
        schedule = get_digest_schedule()
        now = datetime(2025, 6, 1, schedule.preferred_hour, 0, tzinfo=ZoneInfo(get_settings().timezone))
        schedule.enabled = True
        schedule.last_sent_at = None
        ext = db.session.get(UserAccount, world["ext"])
        ext.kind = "external"
        ext.roles = [db.session.scalar(db.select(Role).where(Role.name == Role.ADMIN))]
        db.session.commit()
        assert run_admin_digest(db.session, now=now) is False


def test_external_role_is_shown_as_externi(app, admin_client, world):
    assert "Externí" in admin_client.get(f"/users/{world['ext']}").get_data(as_text=True)
    assert "Externí" in admin_client.get("/users/").get_data(as_text=True)
    assert "Externí" in admin_client.get("/admin/permissions").get_data(as_text=True)
    with app.app_context():
        role = db.session.scalar(db.select(Role).where(Role.name == Role.EXTERNAL))
        assert (role.label, role.slug) == ("Externí", "external")  # the directory sync matches the slug


def test_event_navigation_list_belongs_to_the_person_logged_in(ext_client, world):
    user_id = str(world["ext"])
    assert f'"userId": "{user_id}"' in ext_client.get("/events/").get_data(as_text=True)
    assert f'data-user-id="{user_id}"' in ext_client.get(f"/events/{world['assigned']}").get_data(as_text=True)


def test_external_takes_and_returns_only_own_equipment(app, ext_client, world):
    with app.app_context():
        kind = EquipmentType(name="Batoh")
        db.session.add(kind)
        db.session.flush()
        mine = EquipmentItem(name="Batoh 1", type_id=kind.id)
        theirs = EquipmentItem(name="Batoh 2", type_id=kind.id, issued_to_id=world["member"])
        free = EquipmentItem(name="Batoh 3", type_id=kind.id)
        db.session.add_all([mine, theirs, free])
        db.session.commit()
        ids = {"mine": mine.id, "theirs": theirs.id, "free": free.id}
    page = ext_client.get("/equipment/items/").get_data(as_text=True)
    assert "Batoh 1" in page and "Vzít s sebou" in page
    assert "issueItemModal" not in page and "stranger@test.com" not in page and "Cizí Petr" not in page
    assert ext_client.post(f"/equipment/items/{ids['mine']}/take").status_code == 302
    assert ext_client.post(f"/equipment/items/{ids['free']}/issue", data={"user_id": world["ext"]}).status_code == 403
    assert ext_client.post(f"/equipment/items/{ids['theirs']}/return").status_code == 403
    assert ext_client.post(f"/equipment/items/{ids['mine']}/return").status_code == 302
    with app.app_context():
        assert db.session.get(EquipmentItem, ids["mine"]).issued_to_id is None
        assert db.session.get(EquipmentItem, ids["theirs"]).issued_to_id == world["member"]
