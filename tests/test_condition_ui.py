import re
from datetime import datetime, timedelta, timezone

from flask_login import login_user

from app.extensions import db
from app.models.assignment import Assignment
from app.models.event import Event, EventQualificationRequirement
from app.models.qualification import Qualification
from app.models.role import Role
from app.models.user import UserAccount
from app.routes.main import _open_events_section
from app.staffing import can_join_event
from tests.conftest import _login, _make_user
from tests.test_condition_assignments import _condition_event


def test_condition_detail_lists_summary_participants_and_generic_claim(app, member_client):
    event_id = _condition_event(app)
    with app.app_context():
        qualification = Qualification(name="Doctor", can_be_rp=True)
        event = db.session.get(Event, event_id)
        event.qualification_requirements = [EventQualificationRequirement(qualification=qualification, minimum_count=1)]
        db.session.commit()
    html = member_client.get(f"/events/{event_id}").data.decode()
    assert "0 / 1 / 2 účastníků" in html
    assert "Doctor: 0 / 1" in html
    assert "nikoliv pracovní role" in html
    assert f"/assignments/event/{event_id}/claim" in html
    assert "Přidat pozici" not in html
    member_client.post(f"/assignments/event/{event_id}/claim")
    html = member_client.get(f"/events/{event_id}").data.decode()
    assert "Test Member" in html and "Odhlásit" in html
    assert f"/assignments/event/{event_id}/claim" not in html


def test_dashboard_filters_helpfulness_but_allows_unqualified_join(app, client):
    event_id = _condition_event(app, maximum=3)
    now = datetime.now(timezone.utc)
    with app.app_context():
        member = _make_user("candidate@test.com", "Candidate", Role.MEMBER)
        participant = _make_user("participant@test.com", "Participant", Role.MEMBER)
        qualification = Qualification(name="Doctor", can_be_rp=True)
        event = db.session.get(Event, event_id)
        event.start_datetime, event.end_datetime = now + timedelta(days=1), now + timedelta(days=1, hours=2)
        event.qualification_requirements = [EventQualificationRequirement(qualification=qualification, minimum_count=1)]
        event.assignments = [Assignment(user=participant)]
        db.session.commit()
        candidate_id = member.id
        assert can_join_event(event, member)
    _login(client, "candidate@test.com")
    with app.test_request_context():
        # Use the same authenticated user as the route helper.
        login_user(db.session.get(UserAccount, candidate_id))
        preferred, all_open = _open_events_section(now, now + timedelta(days=7), set())
        assert event_id not in [e.id for e in preferred]
        assert event_id in [e.id for e in all_open]


def test_condition_full_deficit_is_visible_in_list_dashboard_and_table(app, admin_client):
    event_id = _condition_event(app, maximum=1)
    with app.app_context():
        event = db.session.get(Event, event_id)
        event.start_datetime = datetime.now(timezone.utc) + timedelta(days=1)
        event.end_datetime = event.start_datetime + timedelta(hours=1)
        db.session.commit()
        me_id = event.master_event_id
    admin_client.post(f"/assignments/event/{event_id}/claim")
    for url in ("/events/", "/dashboard", f"/master-events/{me_id}/table"):
        html = admin_client.get(url, follow_redirects=True).data.decode()
        assert "Kapacita naplněna" in html
        assert "bg-danger" in html
    html = admin_client.get(f"/master-events/{me_id}/table").data.decode()
    assert "<details>" in html and "Test Admin" in html


def test_condition_capacity_badges_explain_counts_accessibly(app, admin_client):
    event_id = _condition_event(app)
    with app.app_context():
        event = db.session.get(Event, event_id)
        event.start_datetime = datetime.now(timezone.utc) + timedelta(days=1)
        event.end_datetime = event.start_datetime + timedelta(hours=1)
        db.session.commit()
        me_id = event.master_event_id
    explanation = "Aktuální počet účastníků / požadované minimum / maximální kapacita"
    for url in ("/events/", "/dashboard", f"/master-events/{me_id}/table"):
        page = admin_client.get(url).data.decode()
        assert 'data-bs-toggle="tooltip"' in page, url
        assert 'data-bs-trigger="hover focus"' in page
        assert f'title="{explanation}"' in page
        assert f"{explanation}: </span>0 / 1 / 2" in page
        assert 'type="button"' in page  # Native keyboard focus and tap support; no form submission.
        assert "z-2" in page  # Above the dashboard's stretched event link.
    detail = admin_client.get(f"/events/{event_id}").data.decode()
    assert "(aktuální počet účastníků / požadované minimum / maximální kapacita)" in detail


def test_both_dashboard_open_lists_show_condition_capacity(app, member_client):
    event_id = _condition_event(app)
    with app.app_context():
        participant = _make_user("dashboard-other@test.com", "Other", Role.MEMBER)
        first = db.session.get(Event, event_id)
        first.start_datetime = datetime.now(timezone.utc) + timedelta(days=1)
        first.end_datetime = first.start_datetime + timedelta(hours=1)
        second = Event(
            name="Other open condition",
            master_event_id=first.master_event_id,
            staffing_mode=first.staffing_mode,
            minimum_participants=1,
            maximum_participants=2,
            status=first.status,
            start_datetime=first.start_datetime,
            end_datetime=first.end_datetime,
            assignments=[Assignment(user=participant)],
        )
        db.session.add(second)
        db.session.commit()
        page = member_client.get("/dashboard").data.decode()
    for section_id in ("open-eligible", "open-all"):
        section = re.search(rf'<ul[^>]*id="{section_id}"[^>]*>(.*?)</ul>', page, re.S).group(1)
        assert "0 / 1 / 2 účastníků" in section
        assert 'data-bs-toggle="tooltip"' in section
        assert "obsazeno" not in section
    assert "1 / 1 / 2 účastníků" in re.search(r'<ul[^>]*id="open-all"[^>]*>(.*?)</ul>', page, re.S).group(1)
