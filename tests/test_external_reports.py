"""Expanded external reporting permissions retain the event visibility boundary."""

from datetime import datetime, timedelta, timezone
from io import BytesIO

import pytest
from flask_login import login_user
from openpyxl import load_workbook

from app.extensions import db
from app.models.assignment import Assignment
from app.models.equipment import EquipmentItem, EquipmentType
from app.models.event import Event, EventSpot, EventStatus
from app.models.role import Role
from app.models.user import UserAccount
from app.routes.reports import UserStats, _resolve_next_shifts
from tests.conftest import _make_event_with_spot, _make_user
from tests.test_external_events import make_opportunity


@pytest.fixture
def report_events(app, external_client):
    visible_id, spot_id = make_opportunity(app, name="Visible report opportunity")
    with app.app_context():
        member = _make_user("crew@test.com", "Report Crew", Role.MEMBER)
        member_id = member.id
        visible = db.session.get(Event, visible_id)
        visible.spots.append(EventSpot())
        me_id = visible.master_event_id
        db.session.add(Assignment(event_id=visible_id, spot_id=spot_id, user_id=member_id))
        db.session.commit()
    hidden_id, hidden_spot_id = _make_event_with_spot(app, me_id=me_id, name="Hidden full report event")
    draft_id, _ = _make_event_with_spot(app, me_id=me_id, name="Hidden draft report event", status=EventStatus.DRAFT)
    unrelated_id, _ = make_opportunity(app, days=30, name="Hidden unrelated report event")
    with app.app_context():
        hidden = db.session.get(Event, hidden_id)
        hidden.start_datetime = datetime.now(timezone.utc) + timedelta(days=1)
        hidden.end_datetime = hidden.start_datetime + timedelta(hours=4)
        db.session.add(Assignment(event_id=hidden_id, spot_id=hidden_spot_id, user_id=member_id))
        db.session.commit()
        unrelated_me_id = db.session.get(Event, unrelated_id).master_event_id
    return visible_id, me_id, member_id, hidden_id, draft_id, unrelated_me_id


def workbook_text(response):
    workbook = load_workbook(BytesIO(response.data))
    return " ".join(str(cell.value) for sheet in workbook for row in sheet for cell in row)


@pytest.mark.parametrize(
    "kind,format",
    [
        ("user", None),
        ("user", "csv"),
        ("master", None),
        ("master", "csv"),
        ("range", None),
        ("range", "csv"),
        ("work-summary", None),
        ("work-summary", "xlsx"),
    ],
)
def test_external_reports_filter_hidden_events(app, external_client, report_events, kind, format):
    visible_id, me_id, _, _, _, _ = report_events
    with app.app_context():
        external = db.session.scalar(db.select(UserAccount).where(UserAccount.email == "external@test.com"))
        external_id = external.id
        if kind in ("user", "work-summary"):
            event = db.session.get(Event, visible_id)
            db.session.add(Assignment(event=event, spot=event.spots[1], user_id=external_id))
            db.session.commit()
    now = datetime.now(timezone.utc)
    dates = {
        "from_date": (now - timedelta(days=60)).date().isoformat(),
        "to_date": (now + timedelta(days=60)).date().isoformat(),
    }
    urls = {
        "user": f"/reports/user/{external_id}",
        "master": f"/reports/master-event/{me_id}",
        "range": "/reports/date-range",
        "work-summary": "/reports/work-summary",
    }
    response = external_client.get(urls[kind], query_string={**dates, **({"format": format} if format else {})})
    assert response.status_code == 200
    text = workbook_text(response) if format == "xlsx" else response.get_data(as_text=True)
    assert "Visible report opportunity" in text
    assert "Hidden full report event" not in text
    assert "Hidden draft report event" not in text
    assert "Hidden unrelated report event" not in text
    if kind == "work-summary":
        assert "External User" in text
        assert "Report Crew" not in text


@pytest.mark.parametrize("format", [None, "xlsx"])
def test_external_work_summary_excludes_events_assigned_only_to_others(external_client, report_events, format):
    now = datetime.now(timezone.utc)
    dates = {
        "from_date": (now - timedelta(days=60)).date().isoformat(),
        "to_date": (now + timedelta(days=60)).date().isoformat(),
    }
    response = external_client.get(
        "/reports/work-summary", query_string={**dates, **({"format": format} if format else {})}
    )
    assert response.status_code == 200
    text = workbook_text(response) if format == "xlsx" else response.get_data(as_text=True)
    assert "Visible report opportunity" not in text
    assert "Report Crew" not in text


@pytest.mark.parametrize("url", ["/reports/", "/reports/printout"])
def test_external_report_selectors_hide_unavailable_master_events(external_client, report_events, url):
    response = external_client.get(url)
    assert response.status_code == 200
    assert b"ME for Visible report opportunity" in response.data
    assert b"ME for Hidden unrelated report event" not in response.data


def test_external_cannot_get_hidden_master_report(external_client, report_events):
    unrelated_me_id = report_events[-1]
    assert external_client.get(f"/reports/master-event/{unrelated_me_id}").status_code == 403


@pytest.mark.parametrize("selected", [False, True])
def test_external_printout_filters_guessed_hidden_event_ids(external_client, report_events, selected):
    visible_id, me_id, _, hidden_id, draft_id, _ = report_events
    url = "/events/printout" if selected else "/reports/printout"
    data = {"event_ids": [visible_id, hidden_id, draft_id]} if selected else {"me_id": str(me_id)}
    response = external_client.post(url, data=data)
    assert response.status_code == 200
    text = workbook_text(response)
    assert "Visible report opportunity" in text
    assert "Hidden full report event" not in text
    assert "Hidden draft report event" not in text


def test_external_next_shift_statistics_do_not_reveal_hidden_assignment(app, external_client, report_events):
    visible_id, _, member_id, _, _, _ = report_events
    with app.test_request_context():
        external = db.session.scalar(db.select(UserAccount).where(UserAccount.email == "external@test.com"))
        login_user(external)
        member = db.session.get(UserAccount, member_id)
        stats = UserStats()
        _resolve_next_shifts([(member, stats)], datetime.now(timezone.utc))
        assert stats.next_shift == db.session.get(Event, visible_id).start_datetime


def test_external_can_generate_own_work_report_but_not_another_users(app, external_client, tmp_path, monkeypatch):
    monkeypatch.setattr(app, "instance_path", str(tmp_path))
    completed_id, completed_spot = _make_event_with_spot(app, name="Own paid completed", status=EventStatus.COMPLETED)
    draft_id, draft_spot = _make_event_with_spot(app, name="Own paid draft", status=EventStatus.DRAFT)
    with app.app_context():
        external = db.session.scalar(db.select(UserAccount).where(UserAccount.email == "external@test.com"))
        for event_id, spot_id in [(completed_id, completed_spot), (draft_id, draft_spot)]:
            event = db.session.get(Event, event_id)
            event.start_datetime = datetime(2026, 1, 14, 10, tzinfo=timezone.utc)
            event.end_datetime = event.start_datetime + timedelta(hours=4)
            event.paid = True
            db.session.add(Assignment(event_id=event_id, spot_id=spot_id, user_id=external.id))
        db.session.commit()
        other = _make_user("other@test.com", "Other", Role.MEMBER)
        other_id = other.id
    assert external_client.get("/work-report/").status_code == 200
    response = external_client.post("/work-report/generate", data={"year": "2026", "month": "1"}, follow_redirects=True)
    assert response.status_code == 200
    assert "/work-report/download" in response.get_data(as_text=True)
    downloaded = external_client.get("/work-report/download?year=2026&month=1")
    assert downloaded.status_code == 200
    text = workbook_text(downloaded)
    assert "Own paid completed" in text
    assert "Own paid draft" not in text
    assert external_client.get(f"/work-report/?user_id={other_id}").status_code == 403
    assert (
        external_client.post(
            "/work-report/generate", data={"year": "2026", "month": "1", "user_id": str(other_id)}
        ).status_code
        == 403
    )


@pytest.mark.parametrize("action", ["take", "issue"])
def test_external_can_take_and_return_personal_equipment(app, external_client, action):
    with app.app_context():
        external_id = db.session.scalar(db.select(UserAccount).where(UserAccount.email == "external@test.com")).id
        equipment_type = EquipmentType(name="External kit")
        item = EquipmentItem(name="Shared kit", equipment_type=equipment_type)
        db.session.add(item)
        db.session.commit()
        item_id = item.id
    assert external_client.get("/equipment/").status_code == 200
    assert external_client.get("/equipment/items/").status_code == 200
    response = external_client.post(f"/equipment/items/{item_id}/{action}", data={"user_id": str(external_id)})
    assert response.status_code == 302
    with app.app_context():
        external = db.session.scalar(db.select(UserAccount).where(UserAccount.email == "external@test.com"))
        assert db.session.get(EquipmentItem, item_id).issued_to_id == external.id
        assert external.has_permission("equipment_item.report_own")
    assert external_client.post(f"/equipment/items/{item_id}/return").status_code == 302
    with app.app_context():
        assert db.session.get(EquipmentItem, item_id).issued_to_id is None


@pytest.mark.parametrize("action", ["issue", "return"])
@pytest.mark.parametrize("role", ["external", "member"])
def test_equipment_actions_on_other_users_items(app, request, action, role):
    client = request.getfixturevalue(f"{role}_client")
    with app.app_context():
        other = _make_user("equipment-holder@test.com", "Equipment Holder", Role.MEMBER)
        other_id = other.id
        equipment_type = EquipmentType(name="Ownership test kit")
        item = EquipmentItem(name="Ownership test item", equipment_type=equipment_type)
        if action == "return":
            item.issued_to_id = other_id
            item.issued_at = datetime.now(timezone.utc)
        db.session.add(item)
        db.session.commit()
        item_id = item.id
        original_holder = item.issued_to_id
    response = client.post(f"/equipment/items/{item_id}/{action}", data={"user_id": str(other_id)})
    assert response.status_code == (403 if role == "external" else 302)
    with app.app_context():
        item = db.session.get(EquipmentItem, item_id)
        if role == "external":
            assert item.issued_to_id == original_holder
        else:
            assert item.issued_to_id == (other_id if action == "issue" else None)


def test_external_equipment_controls_do_not_offer_other_user_actions(app, external_client):
    with app.app_context():
        other = _make_user("other-holder@test.com", "Other Holder", Role.MEMBER)
        item = EquipmentItem(name="Other users kit", equipment_type=EquipmentType(name="Other kit type"))
        item.issued_to_id = other.id
        db.session.add(item)
        db.session.commit()
        item_id = item.id
    response = external_client.get("/equipment/items/")
    assert response.status_code == 200
    assert b'id="issueItemModal"' not in response.data
    assert b"btn-open-issue" not in response.data
    assert f'action="/equipment/items/{item_id}/return"'.encode() not in response.data


def test_external_report_index_does_not_offer_other_users(app, external_client, report_events):
    member_id = report_events[2]
    with app.app_context():
        unrelated = _make_user("unrelated@test.com", "Hidden Directory User", Role.MEMBER)
        unrelated_id = unrelated.id
        external_id = db.session.scalar(db.select(UserAccount).where(UserAccount.email == "external@test.com")).id
    response = external_client.get("/reports/")
    assert response.status_code == 200
    assert f'href="/reports/user/{external_id}"'.encode() in response.data
    assert b'id="user-select-form"' not in response.data
    assert str(member_id).encode() not in response.data
    assert str(unrelated_id).encode() not in response.data
    assert b"Hidden Directory User" not in response.data
    assert b"Report Crew" not in response.data


@pytest.mark.parametrize("format", [None, "csv"])
@pytest.mark.parametrize("role", ["external", "member"])
def test_other_user_reports_respect_external_boundary(app, request, report_events, role, format):
    visible_id, _, member_id, _, _, _ = report_events
    with app.app_context():
        external = db.session.scalar(db.select(UserAccount).where(UserAccount.email == "external@test.com"))
        event = db.session.get(Event, visible_id)
        db.session.add(Assignment(event=event, spot=event.spots[1], user_id=external.id))
        db.session.commit()
    if role == "member":
        request.getfixturevalue("external_client").get("/auth/logout")
    client = request.getfixturevalue(f"{role}_client")
    with app.app_context():
        expected_user = db.session.scalar(db.select(UserAccount).where(UserAccount.email == f"{role}@test.com"))
        expected_user_id = str(expected_user.id)
    with client.session_transaction() as session:
        assert session["_user_id"] == expected_user_id
    response = client.get(f"/reports/user/{member_id}", query_string={"format": format} if format else {})
    assert response.status_code == (403 if role == "external" else 200)


@pytest.mark.parametrize("role", ["external", "member"])
def test_master_event_links_follow_permissions(app, request, role):
    client = request.getfixturevalue(f"{role}_client")
    event_id, _ = make_opportunity(app)
    with app.app_context():
        master_event_id = db.session.get(Event, event_id).master_event_id
    response = client.get(f"/events/{event_id}")
    assert response.status_code == 200
    for link in (b'href="/master-events/"', f'href="/master-events/{master_event_id}"'.encode()):
        assert (link in response.data) is (role == "member")


@pytest.mark.parametrize("url", ["/equipment/types/create", "/equipment/items/create"])
def test_external_does_not_gain_equipment_administration(external_client, url):
    assert external_client.get(url).status_code == 403
