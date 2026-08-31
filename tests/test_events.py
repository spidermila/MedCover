"""Tests for event CRUD and lifecycle transitions."""

import json
import re
from datetime import datetime, timezone

import pytest

from app.extensions import db
from app.models.assignment import Assignment
from app.models.audit import AuditLogEntry
from app.models.equipment import (
    EquipmentItem,
    EquipmentType,
    EventEquipmentPlan,
)
from app.models.event import Event, EventSpot, EventStatus, EventType
from app.models.master_event import MasterEvent
from app.models.outbox import OutboxEmail
from app.models.qualification import Qualification
from app.models.role import Role
from app.models.settings import get_settings
from app.models.user import UserAccount
from tests.conftest import _get_csrf, _login, _make_event_in_status, _make_master_event, _make_rp_qual, _make_user


def _event_form_data(master_event_id: int, name: str = "Test Event", rp_qual_id: int | None = None) -> dict:
    data: dict = {
        "name": name,
        "master_event_id": str(master_event_id),
        "start_datetime": "2030-06-01T10:00",
        "end_datetime": "2030-06-01T18:00",
        "spot_count": "0",
    }
    if rp_qual_id is not None:
        data["spot_total"] = "1"
        data["spot_desc_0"] = "Zdravotník"
        data["spot_cred_0"] = str(rp_qual_id)
    return data


class TestEventListPermissions:
    def test_event_list_requires_login(self, client):
        response = client.get("/events/", follow_redirects=False)
        assert response.status_code == 302

    def test_event_list_accessible_for_member(self, member_client):
        response = member_client.get("/events/")
        assert response.status_code == 200

    def test_event_list_accessible_for_admin(self, admin_client):
        response = admin_client.get("/events/")
        assert response.status_code == 200

    def test_event_list_has_me_filter_select(self, admin_client):
        response = admin_client.get("/events/")
        assert b'id="me-filter-select"' in response.data

    def test_event_row_has_data_me_attribute(self, app, admin_client):
        me_id = _make_master_event(app)
        rp_qual_id = _make_rp_qual(app)
        admin_client.post("/events/create", data=_event_form_data(me_id, rp_qual_id=rp_qual_id), follow_redirects=True)
        # DRAFT is excluded by default; request it explicitly
        response = admin_client.get("/events/?statuses=DRAFT")
        assert b"data-me=" in response.data

    def test_event_list_sort_rp_does_not_error(self, app, admin_client):
        # Regression: rp sorting previously used .nulls_last(), which emits
        # invalid T-SQL on MSSQL.
        me_id = _make_master_event(app)
        rp_qual_id = _make_rp_qual(app)
        admin_client.post("/events/create", data=_event_form_data(me_id, rp_qual_id=rp_qual_id), follow_redirects=True)
        for direction in ("asc", "desc"):
            resp = admin_client.get(f"/events/?sort=rp&dir={direction}&statuses=DRAFT")
            assert resp.status_code == 200, (direction, resp.data[:400])

    def test_event_list_shows_equipment_icon_and_quantity(self, app, admin_client):
        event_id = _make_event_in_status(app, status=EventStatus.ASSIGNMENTS_OPEN)
        with app.app_context():
            equipment_type = EquipmentType(name="Event list AED", icon="🩺")
            db.session.add(equipment_type)
            db.session.flush()
            db.session.add(
                EventEquipmentPlan(event_id=event_id, equipment_type_id=equipment_type.id, quantity_required=2)
            )
            db.session.commit()

        response = admin_client.get("/events/?statuses=ASSIGNMENTS_OPEN")
        html = response.data.decode()
        assert "Vybavení" in html
        assert "Nadřazená<br>akce" not in html
        assert 'title="Event list AED — 2 ks">🩺</span><small class="text-muted">×2</small>' in html


class TestObsazeniBadges:
    """Verify the mandatory/optional obsazení badges render on both list and detail."""

    def _make_event_with_mand_and_opt(self, app) -> int:
        me_id = _make_master_event(app, name="Badge ME")
        with app.app_context():
            event = Event(
                name="Badge Event",
                master_event_id=me_id,
                status=EventStatus.ASSIGNMENTS_OPEN,
                start_datetime=datetime(2030, 6, 1, 10, 0, tzinfo=timezone.utc),
                end_datetime=datetime(2030, 6, 1, 18, 0, tzinfo=timezone.utc),
            )
            db.session.add(event)
            db.session.flush()
            db.session.add(EventSpot(event_id=event.id, is_optional=False))
            db.session.add(EventSpot(event_id=event.id, is_optional=False))
            db.session.add(EventSpot(event_id=event.id, is_optional=True))
            db.session.commit()
            return event.id

    def test_list_shows_both_badges_when_optional_present(self, app, admin_client):
        self._make_event_with_mand_and_opt(app)
        resp = admin_client.get("/events/?statuses=ASSIGNMENTS_OPEN")
        assert resp.status_code == 200
        html = resp.data.decode()
        # Mandatory badge: none filled → danger, 0/2
        assert 'class="badge bg-danger">0/2</span>' in html
        # Optional badge: none filled → warning, 0/1 vol.
        assert 'class="badge bg-warning text-black">0/1&nbsp;vol.</span>' in html

    def test_detail_shows_verbose_czech_sentence(self, app, admin_client):
        event_id = self._make_event_with_mand_and_opt(app)
        resp = admin_client.get(f"/events/{event_id}")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "0/2 nutných pozic obsazeno" in html
        assert "0/1 volitelných pozic obsazeno" in html

    def test_list_mandatory_badge_turns_green_when_full(self, app, admin_client):
        """Filling all mandatory spots turns the mandatory badge green while optional stays yellow."""
        event_id = self._make_event_with_mand_and_opt(app)
        with app.app_context():
            user = _make_user("badge_filler@test.com", "Filler", Role.MEMBER)
            user2 = _make_user("badge_filler2@test.com", "Filler 2", Role.MEMBER)
            spots = db.session.scalars(
                db.select(EventSpot).where(EventSpot.event_id == event_id, EventSpot.is_optional == False)  # noqa: E712
            ).all()
            db.session.add(Assignment(spot_id=spots[0].id, user_id=user.id, assigned_by_id=user.id))
            db.session.add(Assignment(spot_id=spots[1].id, user_id=user2.id, assigned_by_id=user2.id))
            db.session.commit()

        resp = admin_client.get("/events/?statuses=ASSIGNMENTS_OPEN")
        html = resp.data.decode()
        assert 'class="badge bg-success">2/2</span>' in html
        # Optional still unfilled → warning
        assert 'class="badge bg-warning text-black">0/1&nbsp;vol.</span>' in html


class TestEventCreate:
    def test_create_page_loads_for_admin(self, admin_client):
        response = admin_client.get("/events/create")
        assert response.status_code == 200

    def test_create_prefills_master_event_from_query_param(self, app, admin_client):
        general_id = _make_master_event(app, name="Obecné", is_general=True)
        target_id = _make_master_event(app, name="Konkrétní ME")
        response = admin_client.get(f"/events/create?master_event_id={target_id}")
        assert response.status_code == 200
        html = response.data.decode()
        target_option = html.split(f'value="{target_id}"', 1)[1].split("</option>", 1)[0]
        general_option = html.split(f'value="{general_id}"', 1)[1].split("</option>", 1)[0]
        assert "selected" in target_option
        assert "selected" not in general_option

    def test_create_page_forbidden_for_member(self, member_client):
        response = member_client.get("/events/create")
        assert response.status_code == 403

    def test_admin_can_create_event(self, app, admin_client):
        me_id = _make_master_event(app)
        rp_qual_id = _make_rp_qual(app)
        response = admin_client.post(
            "/events/create",
            data=_event_form_data(me_id, rp_qual_id=rp_qual_id),
            follow_redirects=False,
        )
        assert response.status_code == 302
        with app.app_context():
            event = db.session.scalar(db.select(Event).where(Event.name == "Test Event"))
            assert event is not None
            assert event.status == EventStatus.DRAFT

    def test_create_event_end_before_start_returns_error(self, app, admin_client):
        me_id = _make_master_event(app)
        data = _event_form_data(me_id)
        data["start_datetime"] = "2030-06-01T18:00"
        data["end_datetime"] = "2030-06-01T10:00"
        response = admin_client.post("/events/create", data=data, follow_redirects=True)
        assert response.status_code == 200
        with app.app_context():
            assert db.session.scalar(db.select(db.func.count()).select_from(Event)) == 0

    def test_create_event_missing_name_returns_error(self, app, admin_client):
        me_id = _make_master_event(app)
        data = _event_form_data(me_id)
        data["name"] = ""
        response = admin_client.post(
            "/events/create",
            data=data,
            follow_redirects=True,
        )
        assert response.status_code == 200
        with app.app_context():
            count = db.session.scalar(db.select(db.func.count()).select_from(Event))
            assert count == 0

    def test_create_event_with_responsible_person_uuid(self, app, admin_client):
        """Regression: responsible_person_id is a UUID string — must not be cast to int."""
        me_id = _make_master_event(app)
        with app.app_context():
            role = db.session.scalar(db.select(Role).where(Role.name == Role.MEMBER))
            rp = UserAccount(email="rp_uuid@test.com", name="RP User", is_active=True)
            rp.set_password("testpass123")
            rp.roles = [role]
            db.session.add(rp)
            db.session.commit()
            rp_id = str(rp.id)

        rp_qual_id = _make_rp_qual(app)
        data = _event_form_data(me_id, rp_qual_id=rp_qual_id)
        data["responsible_person_id"] = rp_id
        response = admin_client.post("/events/create", data=data, follow_redirects=False)
        assert response.status_code == 302  # not 500


class TestEventDetail:
    def test_event_detail_loads(self, app, admin_client):
        me_id = _make_master_event(app)
        rp_qual_id = _make_rp_qual(app)
        admin_client.post("/events/create", data=_event_form_data(me_id, rp_qual_id=rp_qual_id), follow_redirects=True)
        with app.app_context():
            event = db.session.scalar(db.select(Event).where(Event.name == "Test Event"))
            event_id = event.id
        response = admin_client.get(f"/events/{event_id}")
        assert response.status_code == 200


class TestEventLifecycle:
    def _create_event(self, app, admin_client):
        me_id = _make_master_event(app)
        rp_qual_id = _make_rp_qual(app)
        admin_client.post("/events/create", data=_event_form_data(me_id, rp_qual_id=rp_qual_id), follow_redirects=True)
        with app.app_context():
            event = db.session.scalar(db.select(Event).where(Event.name == "Test Event"))
            return event.id

    def test_draft_to_published(self, app, admin_client):
        event_id = self._create_event(app, admin_client)
        response = admin_client.post(
            f"/events/{event_id}/transition",
            data={"target_status": "Zveřejněná"},
            follow_redirects=False,
        )
        assert response.status_code == 302
        with app.app_context():
            event = db.session.get(Event, event_id)
            assert event.status == EventStatus.PUBLISHED

    def test_published_to_assignments_open(self, app, admin_client):
        """PUBLISHED → ASSIGNMENTS_OPEN via /transition covers the email notification path."""
        event_id = self._create_event(app, admin_client)
        # First transition to PUBLISHED
        admin_client.post(
            f"/events/{event_id}/transition",
            data={"target_status": "Zveřejněná"},
            follow_redirects=False,
        )
        # Then to ASSIGNMENTS_OPEN
        response = admin_client.post(
            f"/events/{event_id}/transition",
            data={"target_status": "Přihlášky otevřeny"},
            follow_redirects=False,
        )
        assert response.status_code == 302
        with app.app_context():
            event = db.session.get(Event, event_id)
            assert event.status == EventStatus.ASSIGNMENTS_OPEN

    def test_transition_forbidden_without_permission(self, app, member_client):
        """Member lacks event.publish — valid transition but no permission → 403."""
        event_id = _make_event_in_status(app, EventStatus.DRAFT)
        response = member_client.post(
            f"/events/{event_id}/transition",
            data={"target_status": "Zveřejněná"},
        )
        assert response.status_code == 403

    def test_transition_to_published_commits_outbox_rows(self, app, admin_client):
        """Regression: DRAFT → PUBLISHED must commit the event_published outbox
        rows enqueued by send_event_published (which only flushes)."""
        with app.app_context():
            _make_user("m-pub@test.com", "Member Pub", Role.MEMBER)
        event_id = self._create_event(app, admin_client)
        admin_client.post(
            f"/events/{event_id}/transition",
            data={"target_status": "Zveřejněná"},
            follow_redirects=False,
        )
        with app.app_context():
            rows = db.session.scalars(
                db.select(OutboxEmail).where(
                    OutboxEmail.event_id == event_id,
                    OutboxEmail.notification_type == "event_published",
                )
            ).all()
            assert rows, "event_published outbox rows were not persisted after commit"

    def test_transition_to_assignments_open_commits_outbox_rows(self, app, admin_client):
        """Regression: PUBLISHED → ASSIGNMENTS_OPEN must commit the
        assignments_opened outbox rows."""
        with app.app_context():
            _make_user("m-open@test.com", "Member Open", Role.MEMBER)
        event_id = self._create_event(app, admin_client)
        admin_client.post(
            f"/events/{event_id}/transition",
            data={"target_status": "Zveřejněná"},
            follow_redirects=False,
        )
        admin_client.post(
            f"/events/{event_id}/transition",
            data={"target_status": "Přihlášky otevřeny"},
            follow_redirects=False,
        )
        with app.app_context():
            rows = db.session.scalars(
                db.select(OutboxEmail).where(
                    OutboxEmail.event_id == event_id,
                    OutboxEmail.notification_type == "assignments_opened",
                )
            ).all()
            assert rows, "assignments_opened outbox rows were not persisted after commit"

    def test_cannot_skip_status(self, app, admin_client):
        event_id = self._create_event(app, admin_client)
        # Try to jump from DRAFT directly to ASSIGNMENTS_OPEN (not allowed)
        admin_client.post(
            f"/events/{event_id}/transition",
            data={"target_status": "Přihlášky otevřeny"},
            follow_redirects=True,
        )
        with app.app_context():
            event = db.session.get(Event, event_id)
            assert event.status == EventStatus.DRAFT

    def test_cancel_archives_event(self, app, admin_client):
        event_id = self._create_event(app, admin_client)
        admin_client.post(f"/events/{event_id}/cancel", follow_redirects=True)
        with app.app_context():
            event = db.session.get(Event, event_id)
            assert event.status == EventStatus.CANCELLED
            assert event.archived is True

    def test_restore_unarchives_event(self, app, admin_client):
        event_id = self._create_event(app, admin_client)
        admin_client.post(f"/events/{event_id}/cancel", follow_redirects=True)
        admin_client.post(f"/events/{event_id}/restore", follow_redirects=True)
        with app.app_context():
            event = db.session.get(Event, event_id)
            assert event.archived is False

    def test_member_cannot_cancel(self, app, member_client):
        """Create event directly in DB (avoids shared-client conflict) then test permission."""
        with app.app_context():
            me = MasterEvent(name="ME for cancel test")
            db.session.add(me)
            db.session.flush()
            creator_role = db.session.scalar(db.select(Role).where(Role.name == Role.ADMIN))
            creator = UserAccount(email="creator_cancel@test.com", name="Creator", is_active=True)
            creator.set_password("testpass123")
            creator.roles = [creator_role]
            db.session.add(creator)
            db.session.flush()
            event = Event(
                name="Cancel Test Event",
                master_event_id=me.id,
                start_datetime=datetime(2030, 6, 1, 10, 0, tzinfo=timezone.utc),
                end_datetime=datetime(2030, 6, 1, 18, 0, tzinfo=timezone.utc),
                created_by_id=creator.id,
            )
            db.session.add(event)
            db.session.commit()
            event_id = event.id

        response = member_client.post(f"/events/{event_id}/cancel", follow_redirects=False)
        assert response.status_code == 403


class TestEventEdit:
    def test_admin_can_edit_event(self, app, admin_client):
        me_id = _make_master_event(app)
        rp_qual_id = _make_rp_qual(app)
        admin_client.post("/events/create", data=_event_form_data(me_id, rp_qual_id=rp_qual_id), follow_redirects=True)
        with app.app_context():
            event = db.session.scalar(db.select(Event).where(Event.name == "Test Event"))
            event_id = event.id
            version = event.version

        response = admin_client.post(
            f"/events/{event_id}/edit",
            data={
                **_event_form_data(me_id, name="Updated Event"),
                "version": str(version),
            },
            follow_redirects=False,
        )
        assert response.status_code == 302
        with app.app_context():
            event = db.session.get(Event, event_id)
            assert event.name == "Updated Event"

    def test_member_cannot_edit_event(self, app, member_client):
        """Create event directly in DB then test member cannot access edit page."""
        with app.app_context():
            me = MasterEvent(name="ME for edit test")
            db.session.add(me)
            db.session.flush()
            creator_role = db.session.scalar(db.select(Role).where(Role.name == Role.ADMIN))
            creator = UserAccount(email="creator_edit@test.com", name="Creator", is_active=True)
            creator.set_password("testpass123")
            creator.roles = [creator_role]
            db.session.add(creator)
            db.session.flush()
            event = Event(
                name="Edit Test Event",
                master_event_id=me.id,
                start_datetime=datetime(2030, 6, 1, 10, 0, tzinfo=timezone.utc),
                end_datetime=datetime(2030, 6, 1, 18, 0, tzinfo=timezone.utc),
                created_by_id=creator.id,
            )
            db.session.add(event)
            db.session.commit()
            event_id = event.id

        response = member_client.get(f"/events/{event_id}/edit")
        assert response.status_code == 403


class TestCalendarFeed:
    def test_feed_requires_login(self, client):
        response = client.get("/events/feed", follow_redirects=False)
        assert response.status_code == 302

    def test_feed_returns_json(self, admin_client):
        response = admin_client.get("/events/feed")
        assert response.status_code == 200
        data = response.get_json()
        assert isinstance(data, list)

    def test_feed_excludes_archived_events_by_default(self, app, admin_client):
        me_id = _make_master_event(app)
        rp_qual_id = _make_rp_qual(app)
        admin_client.post("/events/create", data=_event_form_data(me_id, rp_qual_id=rp_qual_id), follow_redirects=True)
        with app.app_context():
            event = db.session.scalar(db.select(Event).where(Event.name == "Test Event"))
            event_id = event.id
        # Cancel (archives) the event
        admin_client.post(f"/events/{event_id}/cancel", follow_redirects=True)
        # Feed should not include archived events by default
        feed_data = admin_client.get("/events/feed").get_json()
        titles = [e.get("title", "") for e in feed_data]
        assert not any("Test Event" in t for t in titles)

    def test_completed_events_included_in_index_and_feed(self, app, admin_client):
        """Completed is a status like Published — server always returns it; JS filter controls visibility."""
        with app.app_context():
            me = MasterEvent(name="ME for completed test")
            db.session.add(me)
            db.session.flush()
            creator = db.session.scalar(db.select(UserAccount).where(UserAccount.email == "admin@test.com"))
            event = Event(
                name="Completed Test Event",
                master_event_id=me.id,
                start_datetime=datetime(2020, 1, 1, 10, 0, tzinfo=timezone.utc),
                end_datetime=datetime(2020, 1, 1, 18, 0, tzinfo=timezone.utc),
                status=EventStatus.COMPLETED,
                created_by_id=creator.id,
            )
            db.session.add(event)
            db.session.commit()

        # Table page should include the row when COMPLETED filter is explicitly enabled
        resp = admin_client.get("/events/?statuses=COMPLETED")
        assert b"Completed Test Event" in resp.data

        # Feed should also return the completed event
        feed_data = admin_client.get("/events/feed").get_json()
        titles = [e.get("title", "") for e in feed_data]
        assert any("Completed Test Event" in t for t in titles)


class TestAuditChangeTracking:
    """Verify audit log captures before/after changes in {field: [old, new]} format."""

    def test_event_edit_records_changes(self, app, admin_client):
        me_id = _make_master_event(app)
        rp_qual_id = _make_rp_qual(app)
        admin_client.post("/events/create", data=_event_form_data(me_id, rp_qual_id=rp_qual_id), follow_redirects=True)
        with app.app_context():
            event = db.session.scalar(db.select(Event).where(Event.name == "Test Event"))
            event_id = event.id
            version = event.version

        admin_client.post(
            f"/events/{event_id}/edit",
            data={
                **_event_form_data(me_id, name="Renamed Event"),
                "version": str(version),
            },
            follow_redirects=False,
        )

        with app.app_context():
            entry = db.session.scalar(
                db.select(AuditLogEntry)
                .where(AuditLogEntry.entity_type == "Event")
                .where(AuditLogEntry.action_type == "edit")
                .where(AuditLogEntry.entity_id == str(event_id))
                .order_by(AuditLogEntry.id.desc())
            )
            assert entry is not None
            assert entry.changes_json is not None
            # Must use {field: [old, new]} format
            assert "name" in entry.changes_json
            assert entry.changes_json["name"] == ["Test Event", "Renamed Event"]

    def test_event_edit_no_change_produces_empty_changes(self, app, admin_client):
        """When nothing changes, changes_json should be None or empty dict."""
        me_id = _make_master_event(app)
        rp_qual_id = _make_rp_qual(app)
        admin_client.post("/events/create", data=_event_form_data(me_id, rp_qual_id=rp_qual_id), follow_redirects=True)
        with app.app_context():
            event = db.session.scalar(db.select(Event).where(Event.name == "Test Event"))
            event_id = event.id
            version = event.version

        admin_client.post(
            f"/events/{event_id}/edit",
            data={
                **_event_form_data(me_id, name="Test Event"),
                "version": str(version),
            },
            follow_redirects=False,
        )

        with app.app_context():
            entry = db.session.scalar(
                db.select(AuditLogEntry)
                .where(AuditLogEntry.entity_type == "Event")
                .where(AuditLogEntry.action_type == "edit")
                .where(AuditLogEntry.entity_id == str(event_id))
                .order_by(AuditLogEntry.id.desc())
            )
            assert entry is not None
            # No fields changed → changes_json should be None or {}
            assert not entry.changes_json

    def test_create_event_end_before_start_rejected(self, app, admin_client):
        me_id = _make_master_event(app)
        data = _event_form_data(me_id)
        # Swap: end is before start
        data["start_datetime"] = "2030-06-01T18:00"
        data["end_datetime"] = "2030-06-01T10:00"
        response = admin_client.post(
            "/events/create",
            data=data,
            follow_redirects=True,
        )
        assert response.status_code == 200
        with app.app_context():
            count = db.session.scalar(db.select(db.func.count()).select_from(Event))
            assert count == 0

    def test_create_event_equal_start_end_rejected(self, app, admin_client):
        me_id = _make_master_event(app)
        data = _event_form_data(me_id)
        data["start_datetime"] = "2030-06-01T10:00"
        data["end_datetime"] = "2030-06-01T10:00"
        response = admin_client.post(
            "/events/create",
            data=data,
            follow_redirects=True,
        )
        assert response.status_code == 200
        with app.app_context():
            count = db.session.scalar(db.select(db.func.count()).select_from(Event))
            assert count == 0


class TestBulkAction:
    def _create_multiple_events(self, app, admin_client, count: int = 2) -> list[int]:
        me_id = _make_master_event(app)
        rp_qual_id = _make_rp_qual(app)
        ids = []
        for i in range(count):
            admin_client.post(
                "/events/create",
                data=_event_form_data(me_id, name=f"Bulk Event {i}", rp_qual_id=rp_qual_id),
                follow_redirects=True,
            )
        with app.app_context():
            events = db.session.scalars(db.select(Event).where(Event.name.like("Bulk Event%"))).all()
            ids = [e.id for e in events]
        return ids

    def test_bulk_publish_changes_status(self, app, admin_client):
        ids = self._create_multiple_events(app, admin_client)
        admin_client.post(
            "/events/bulk",
            data={"action": "publish", "event_ids": [str(i) for i in ids]},
            follow_redirects=True,
        )
        with app.app_context():
            for event_id in ids:
                event = db.session.get(Event, event_id)
                assert event.status == EventStatus.PUBLISHED

    def test_bulk_cancel_archives_events(self, app, admin_client):
        ids = self._create_multiple_events(app, admin_client)
        admin_client.post(
            "/events/bulk",
            data={"action": "cancel", "event_ids": [str(i) for i in ids]},
            follow_redirects=True,
        )
        with app.app_context():
            for event_id in ids:
                event = db.session.get(Event, event_id)
                assert event.status == EventStatus.CANCELLED
                assert event.archived is True

    def test_bulk_action_member_forbidden(self, app, member_client):
        response = member_client.post(
            "/events/bulk",
            data={"action": "publish", "event_ids": ["1"]},
            follow_redirects=False,
        )
        assert response.status_code == 403

    def test_bulk_invalid_action_returns_400(self, admin_client):
        response = admin_client.post(
            "/events/bulk",
            data={"action": "destroy_everything", "event_ids": ["1"]},
        )
        assert response.status_code == 400

    def test_bulk_action_skips_events_in_wrong_status(self, app, admin_client):
        """Events not in valid_from statuses are skipped; only valid ones are changed."""
        ids = self._create_multiple_events(app, admin_client)
        # publish the first event so it can't be published again
        admin_client.post("/events/bulk", data={"action": "publish", "event_ids": [str(ids[0])]})
        # now try to publish both — the already-published one should be skipped
        resp = admin_client.post(
            "/events/bulk",
            data={"action": "publish", "event_ids": [str(i) for i in ids]},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert "Přeskočeno".encode() in resp.data

    def test_bulk_empty_selection_flashes_warning(self, admin_client):
        response = admin_client.post(
            "/events/bulk",
            data={"action": "publish", "event_ids": []},
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "Žádné akce".encode() in response.data

    def test_bulk_set_paid_marks_events_as_paid(self, app, admin_client):
        ids = self._create_multiple_events(app, admin_client)
        admin_client.post(
            "/events/bulk",
            data={"action": "set_paid", "event_ids": [str(i) for i in ids]},
            follow_redirects=True,
        )
        with app.app_context():
            for event_id in ids:
                event = db.session.get(Event, event_id)
                assert event.paid is True

    def test_bulk_set_unpaid_marks_events_as_unpaid(self, app, admin_client):
        me_id = _make_master_event(app)
        with app.app_context():
            events = [
                Event(
                    name=f"Paid Event {i}",
                    master_event_id=me_id,
                    start_datetime=datetime(2030, 7, 1, 10, 0, tzinfo=timezone.utc),
                    end_datetime=datetime(2030, 7, 1, 18, 0, tzinfo=timezone.utc),
                    status=EventStatus.DRAFT,
                    paid=True,
                )
                for i in range(2)
            ]
            db.session.add_all(events)
            db.session.commit()
            ids = [e.id for e in events]

        admin_client.post(
            "/events/bulk",
            data={"action": "set_unpaid", "event_ids": [str(i) for i in ids]},
            follow_redirects=True,
        )
        with app.app_context():
            for event_id in ids:
                event = db.session.get(Event, event_id)
                assert event.paid is False

    def test_bulk_set_paid_skips_already_paid(self, app, admin_client):
        me_id = _make_master_event(app)
        with app.app_context():
            already_paid = Event(
                name="Already Paid",
                master_event_id=me_id,
                start_datetime=datetime(2030, 8, 1, 10, 0, tzinfo=timezone.utc),
                end_datetime=datetime(2030, 8, 1, 18, 0, tzinfo=timezone.utc),
                status=EventStatus.DRAFT,
                paid=True,
            )
            not_paid = Event(
                name="Not Paid",
                master_event_id=me_id,
                start_datetime=datetime(2030, 8, 2, 10, 0, tzinfo=timezone.utc),
                end_datetime=datetime(2030, 8, 2, 18, 0, tzinfo=timezone.utc),
                status=EventStatus.DRAFT,
                paid=False,
            )
            db.session.add_all([already_paid, not_paid])
            db.session.commit()
            ids = [already_paid.id, not_paid.id]

        resp = admin_client.post(
            "/events/bulk",
            data={"action": "set_paid", "event_ids": [str(i) for i in ids]},
            follow_redirects=True,
        )
        assert "Změněno 1".encode() in resp.data
        with app.app_context():
            assert db.session.get(Event, ids[0]).paid is True
            assert db.session.get(Event, ids[1]).paid is True

    def test_bulk_set_paid_forbidden_for_member(self, app, member_client):
        ids = self._create_multiple_events(app, member_client)
        response = member_client.post(
            "/events/bulk",
            data={"action": "set_paid", "event_ids": [str(i) for i in ids]},
        )
        assert response.status_code == 403

    def test_bulk_set_paid_audit_logged(self, app, admin_client):
        ids = self._create_multiple_events(app, admin_client)
        admin_client.post(
            "/events/bulk",
            data={"action": "set_paid", "event_ids": [str(i) for i in ids]},
            follow_redirects=True,
        )
        with app.app_context():
            entries = db.session.scalars(
                db.select(AuditLogEntry)
                .where(AuditLogEntry.entity_type == "Event", AuditLogEntry.action_type == "edit")
                .order_by(AuditLogEntry.timestamp.desc())
            ).all()
            paid_entries = [e for e in entries if "označena jako placená" in e.summary]
            assert len(paid_entries) == len(ids)


class TestAddSpot:
    def test_admin_can_add_spot(self, app, admin_client):
        me_id = _make_master_event(app)
        rp_qual_id = _make_rp_qual(app)
        admin_client.post("/events/create", data=_event_form_data(me_id, rp_qual_id=rp_qual_id), follow_redirects=True)
        with app.app_context():
            event = db.session.scalar(db.select(Event).where(Event.name == "Test Event"))
            event_id = event.id

        response = admin_client.post(
            f"/events/{event_id}/spots/add",
            data={"description": "Zdravotník", "quantity": "1"},
            follow_redirects=False,
        )
        assert response.status_code == 302
        with app.app_context():
            count = db.session.scalar(
                db.select(db.func.count()).select_from(EventSpot).where(EventSpot.event_id == event_id)
            )
            assert count >= 1

    def test_member_cannot_add_spot(self, app, member_client):
        with app.app_context():
            me = MasterEvent(name="Spot Test ME")
            db.session.add(me)
            db.session.flush()
            creator_role = db.session.scalar(db.select(Role).where(Role.name == Role.ADMIN))
            creator = UserAccount(email="creator_spot@test.com", name="Creator", is_active=True)
            creator.set_password("testpass123")
            creator.roles = [creator_role]
            db.session.add(creator)
            db.session.flush()
            event = Event(
                name="Spot Test Event",
                master_event_id=me.id,
                start_datetime=datetime(2030, 6, 1, 10, 0, tzinfo=timezone.utc),
                end_datetime=datetime(2030, 6, 1, 18, 0, tzinfo=timezone.utc),
                created_by_id=creator.id,
            )
            db.session.add(event)
            db.session.commit()
            event_id = event.id

        response = member_client.post(
            f"/events/{event_id}/spots/add",
            data={"description": "Zdravotník", "quantity": "1"},
        )
        assert response.status_code == 403


# ── Edit: extended ────────────────────────────────────────────────────────────


class TestEventEditExtended:
    def test_get_returns_200(self, app, admin_client):
        event_id = _make_event_in_status(app, EventStatus.DRAFT)
        response = admin_client.get(f"/events/{event_id}/edit")
        assert response.status_code == 200

    def test_get_404_for_missing(self, admin_client):
        response = admin_client.get("/events/999999/edit")
        assert response.status_code == 404

    def test_completed_event_redirects(self, app, admin_client):
        event_id = _make_event_in_status(app, EventStatus.COMPLETED)
        response = admin_client.get(f"/events/{event_id}/edit", follow_redirects=False)
        assert response.status_code in (200, 302)

    def test_stale_version_flashes(self, app, admin_client):
        me_id = _make_master_event(app)
        event_id = _make_event_in_status(app, EventStatus.DRAFT)
        response = admin_client.post(
            f"/events/{event_id}/edit",
            data={**_event_form_data(me_id), "version": "9999"},
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "mezitím" in response.data.decode()

    def test_empty_name_flashes(self, app, admin_client):
        me_id = _make_master_event(app)
        event_id = _make_event_in_status(app, EventStatus.DRAFT)
        with app.app_context():
            version = db.session.get(Event, event_id).version
        data = {**_event_form_data(me_id), "name": "", "version": str(version)}
        response = admin_client.post(f"/events/{event_id}/edit", data=data, follow_redirects=True)
        assert response.status_code == 200
        assert "povinný" in response.data.decode()

    def test_successful_edit_saves(self, app, admin_client):
        me_id = _make_master_event(app)
        event_id = _make_event_in_status(app, EventStatus.DRAFT)
        with app.app_context():
            version = db.session.get(Event, event_id).version
        response = admin_client.post(
            f"/events/{event_id}/edit",
            data={**_event_form_data(me_id, name="Renamed Event"), "version": str(version)},
            follow_redirects=False,
        )
        assert response.status_code == 302
        with app.app_context():
            assert db.session.get(Event, event_id).name == "Renamed Event"


# ── Transition: edge cases ────────────────────────────────────────────────────


class TestEventTransitionExtended:
    def test_transition_404_for_missing_event(self, admin_client):
        response = admin_client.post("/events/999999/transition", data={"target_status": "PUBLISHED"})
        assert response.status_code == 404

    def test_transition_invalid_status_400(self, app, admin_client):
        event_id = _make_event_in_status(app, EventStatus.DRAFT)
        response = admin_client.post(f"/events/{event_id}/transition", data={"target_status": "NOT_VALID_STATUS"})
        assert response.status_code == 400

    def test_transition_not_allowed_flashes(self, app, admin_client):
        """Transitioning DRAFT → COMPLETED is not a valid transition."""
        event_id = _make_event_in_status(app, EventStatus.DRAFT)
        response = admin_client.post(
            f"/events/{event_id}/transition",
            data={"target_status": EventStatus.COMPLETED.value},
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "povolen" in response.data.decode()


# ── Cancel ────────────────────────────────────────────────────────────────────


class TestEventCancel:
    def test_member_cannot_cancel(self, app, member_client):
        event_id = _make_event_in_status(app, EventStatus.DRAFT)
        response = member_client.post(f"/events/{event_id}/cancel")
        assert response.status_code == 403

    def test_cancel_404_for_missing(self, admin_client):
        response = admin_client.post("/events/999999/cancel")
        assert response.status_code == 404

    def test_cancel_completed_event_flashes(self, app, admin_client):
        event_id = _make_event_in_status(app, EventStatus.COMPLETED)
        response = admin_client.post(f"/events/{event_id}/cancel", follow_redirects=True)
        assert response.status_code == 200
        assert "nelze" in response.data.decode() or "Dokončen" in response.data.decode()

    def test_cancel_draft_event_succeeds(self, app, admin_client):
        event_id = _make_event_in_status(app, EventStatus.DRAFT)
        response = admin_client.post(f"/events/{event_id}/cancel", follow_redirects=False)
        assert response.status_code == 302
        with app.app_context():
            event = db.session.get(Event, event_id)
            assert event.status == EventStatus.CANCELLED
            assert event.archived is True


# ── Restore ───────────────────────────────────────────────────────────────────


class TestEventRestore:
    def test_member_cannot_restore(self, app, member_client):
        event_id = _make_event_in_status(app, EventStatus.CANCELLED)
        response = member_client.post(f"/events/{event_id}/restore")
        assert response.status_code == 403

    def test_restore_404_for_missing(self, admin_client):
        response = admin_client.post("/events/999999/restore")
        assert response.status_code == 404

    def test_restore_non_cancelled_flashes(self, app, admin_client):
        event_id = _make_event_in_status(app, EventStatus.DRAFT)
        response = admin_client.post(f"/events/{event_id}/restore", follow_redirects=True)
        assert response.status_code == 200
        assert "zrušen" in response.data.decode()

    def test_restore_cancelled_succeeds(self, app, admin_client):
        event_id = _make_event_in_status(app, EventStatus.CANCELLED)
        response = admin_client.post(f"/events/{event_id}/restore", follow_redirects=False)
        assert response.status_code == 302
        with app.app_context():
            event = db.session.get(Event, event_id)
            assert event.status == EventStatus.DRAFT
            assert event.archived is False


# ── Calendar feed ─────────────────────────────────────────────────────────────


class TestCalendarFeedExtended:
    def test_feed_returns_json_for_admin(self, app, admin_client):
        _make_event_in_status(app, EventStatus.PUBLISHED)
        response = admin_client.get("/events/feed")
        assert response.status_code == 200
        data = response.get_json()
        assert isinstance(data, list)
        assert len(data) >= 1

    def test_feed_member_can_see_drafts(self, app, member_client):
        """Member has event.view_draft so draft events are included in the feed."""
        _make_event_in_status(app, EventStatus.DRAFT)
        response = member_client.get("/events/feed")
        assert response.status_code == 200
        data = response.get_json()
        statuses = [item["extendedProps"]["status_key"] for item in data]
        assert "DRAFT" in statuses


# ── Edit spot ─────────────────────────────────────────────────────────────────


class TestEditSpot:
    def _create_event_with_spot(self, app) -> tuple[int, int]:
        """Create an event with two spots: one mandatory RP-capable anchor spot, and one test spot.

        The anchor spot ensures the RP constraint is satisfied even when the test spot is modified.
        Returns (event_id, test_spot_id).
        """
        with app.app_context():
            me = MasterEvent(name="EditSpot ME")
            db.session.add(me)
            db.session.flush()
            rp_qual = Qualification(name="EditSpot RP Qual", can_be_rp=True)
            db.session.add(rp_qual)
            db.session.flush()
            event = Event(
                name="EditSpot Event",
                master_event_id=me.id,
                status=EventStatus.DRAFT,
                start_datetime=datetime(2030, 6, 1, 10, 0, tzinfo=timezone.utc),
                end_datetime=datetime(2030, 6, 1, 18, 0, tzinfo=timezone.utc),
            )
            db.session.add(event)
            db.session.flush()
            # Anchor spot: mandatory + RP-capable qual (ensures RP constraint is met)
            anchor = EventSpot(event_id=event.id, description="Anchor RP Spot", is_optional=False)
            anchor.required_qualifications = [rp_qual]
            db.session.add(anchor)
            # Test spot: mandatory, no qual — the one used by tests
            spot = EventSpot(event_id=event.id, description="Old Desc")
            db.session.add(spot)
            db.session.commit()
            return event.id, spot.id

    def test_member_cannot_edit_spot(self, app, member_client):
        event_id, spot_id = self._create_event_with_spot(app)
        response = member_client.post(f"/events/{event_id}/spots/{spot_id}/edit", data={})
        assert response.status_code == 403

    def test_spot_404_for_missing(self, app, admin_client):
        event_id = _make_event_in_status(app, EventStatus.DRAFT)
        response = admin_client.post(f"/events/{event_id}/spots/999999/edit", data={"description": "X"})
        assert response.status_code == 404

    def test_edit_spot_with_ineligible_user_blocked_without_confirm(self, app, admin_client):
        """Changing qualifications on an occupied spot is blocked if confirm checkbox is missing."""
        event_id, spot_id = self._create_event_with_spot(app)
        with app.app_context():
            qual = Qualification(name="RequiredQual")
            db.session.add(qual)
            db.session.flush()
            role = db.session.scalar(db.select(Role).where(Role.name == Role.MEMBER))
            member = UserAccount(email="ineligible_member@test.com", name="Ineligible", is_active=True)
            member.set_password("testpass123")
            member.roles = [role]
            db.session.add(member)
            db.session.flush()
            spot = db.session.get(EventSpot, spot_id)
            spot.assignment = Assignment(user_id=member.id, assigned_by_id=member.id)
            db.session.commit()
            qual_id = qual.id

        resp = admin_client.post(
            f"/events/{event_id}/spots/{spot_id}/edit",
            data={"description": "Updated", "qualification_ids": [str(qual_id)]},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert "Pozice nezměněna".encode() in resp.data

    def test_edit_spot_with_ineligible_user_and_confirm_unassigns(self, app, admin_client):
        """Changing qualifications with confirm checkbox triggers automatic unassign."""
        event_id, spot_id = self._create_event_with_spot(app)
        with app.app_context():
            qual = Qualification(name="RequiredQual2")
            db.session.add(qual)
            db.session.flush()
            role = db.session.scalar(db.select(Role).where(Role.name == Role.MEMBER))
            member = UserAccount(email="ineligible_member2@test.com", name="Ineligible2", is_active=True)
            member.set_password("testpass123")
            member.roles = [role]
            db.session.add(member)
            db.session.flush()
            spot = db.session.get(EventSpot, spot_id)
            spot.assignment = Assignment(user_id=member.id, assigned_by_id=member.id)
            db.session.commit()
            qual_id = qual.id

        resp = admin_client.post(
            f"/events/{event_id}/spots/{spot_id}/edit",
            data={"description": "Updated", "qualification_ids": [str(qual_id)], "confirm_unassign": "1"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        with app.app_context():
            spot = db.session.get(EventSpot, spot_id)
            assert spot.assignment is None

    def test_edit_spot_saves_description(self, app, admin_client):
        event_id, spot_id = self._create_event_with_spot(app)
        response = admin_client.post(
            f"/events/{event_id}/spots/{spot_id}/edit",
            data={"description": "New Desc"},
            follow_redirects=False,
        )
        assert response.status_code == 302
        with app.app_context():
            spot = db.session.get(EventSpot, spot_id)
            assert spot.description == "New Desc"


# ── Delete spot ───────────────────────────────────────────────────────────────


class TestDeleteSpot:
    def _create_event_with_spot(self, app) -> tuple[int, int]:
        """Create an event with two spots: one mandatory RP-capable anchor spot, and one test spot.

        The anchor spot ensures the RP constraint is satisfied even when the test spot is deleted.
        Returns (event_id, test_spot_id).
        """
        with app.app_context():
            me = MasterEvent(name="DelSpot ME")
            db.session.add(me)
            db.session.flush()
            rp_qual = Qualification(name="DelSpot RP Qual", can_be_rp=True)
            db.session.add(rp_qual)
            db.session.flush()
            event = Event(
                name="DelSpot Event",
                master_event_id=me.id,
                status=EventStatus.DRAFT,
                start_datetime=datetime(2030, 6, 1, 10, 0, tzinfo=timezone.utc),
                end_datetime=datetime(2030, 6, 1, 18, 0, tzinfo=timezone.utc),
            )
            db.session.add(event)
            db.session.flush()
            # Anchor spot: mandatory + RP-capable qual (ensures RP constraint is met after test spot deletion)
            anchor = EventSpot(event_id=event.id, description="Anchor RP Spot", is_optional=False)
            anchor.required_qualifications = [rp_qual]
            db.session.add(anchor)
            # Test spot: mandatory, no qual — the one used by tests
            spot = EventSpot(event_id=event.id)
            db.session.add(spot)
            db.session.commit()
            return event.id, spot.id

    def test_member_cannot_delete_spot(self, app, member_client):
        event_id, spot_id = self._create_event_with_spot(app)
        response = member_client.post(f"/events/{event_id}/spots/{spot_id}/delete")
        assert response.status_code == 403

    def test_delete_404_for_missing_spot(self, app, admin_client):
        event_id = _make_event_in_status(app, EventStatus.DRAFT)
        response = admin_client.post(f"/events/{event_id}/spots/999999/delete")
        assert response.status_code == 404

    def test_delete_occupied_spot_is_blocked(self, app, admin_client):
        """Cannot delete a spot that has an assignment."""
        event_id, spot_id = self._create_event_with_spot(app)
        with app.app_context():
            role = db.session.scalar(db.select(Role).where(Role.name == Role.MEMBER))
            member = UserAccount(email="occupied_member@test.com", name="Occupied", is_active=True)
            member.set_password("testpass123")
            member.roles = [role]
            db.session.add(member)
            db.session.flush()
            spot = db.session.get(EventSpot, spot_id)
            spot.assignment = Assignment(user_id=member.id, assigned_by_id=member.id)
            db.session.commit()

        resp = admin_client.post(f"/events/{event_id}/spots/{spot_id}/delete", follow_redirects=True)
        assert resp.status_code == 200
        assert b"Obsazenou pozici nelze smazat" in resp.data
        with app.app_context():
            assert db.session.get(EventSpot, spot_id) is not None

    def test_delete_spot_succeeds(self, app, admin_client):
        event_id, spot_id = self._create_event_with_spot(app)
        response = admin_client.post(f"/events/{event_id}/spots/{spot_id}/delete", follow_redirects=False)
        assert response.status_code == 302
        with app.app_context():
            assert db.session.get(EventSpot, spot_id) is None


# ── Equipment plan: extended ──────────────────────────────────────────────────


class TestEquipmentPlanExtended:
    def _make_event_and_type(self, app):
        event_id = _make_event_in_status(app, EventStatus.DRAFT)
        with app.app_context():
            et = EquipmentType(name="Plan Type")
            db.session.add(et)
            db.session.commit()
            return event_id, et.id

    def test_plan_add_404_for_missing_event(self, app, admin_client):
        with app.app_context():
            et = EquipmentType(name="Plan T2")
            db.session.add(et)
            db.session.commit()
            type_id = et.id
        response = admin_client.post(
            "/events/999999/equipment/plan",
            data={"type_id": str(type_id), "quantity": "1"},
        )
        assert response.status_code == 404

    def test_plan_add_invalid_type_quantity_flashes(self, app, admin_client):
        event_id = _make_event_in_status(app, EventStatus.DRAFT)
        response = admin_client.post(
            f"/events/{event_id}/equipment/plan",
            data={"type_id": "", "quantity": "0"},
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "platný" in response.data.decode() or "typ" in response.data.decode().lower()

    def test_plan_remove_works(self, app, admin_client):
        event_id, type_id = self._make_event_and_type(app)
        # Add a plan entry
        admin_client.post(
            f"/events/{event_id}/equipment/plan",
            data={"type_id": str(type_id), "quantity": "1"},
            follow_redirects=True,
        )
        # Remove it
        response = admin_client.post(
            f"/events/{event_id}/equipment/plan/remove",
            data={"type_id": str(type_id)},
            follow_redirects=False,
        )
        assert response.status_code == 302
        with app.app_context():
            assert db.session.get(EventEquipmentPlan, (event_id, type_id)) is None


# ── Equipment assign: extended ────────────────────────────────────────────────


class TestEventChangedNotification:
    """Verify that editing an event enqueues notifications to assigned users."""

    def test_edit_sends_notification_to_assigned_user(self, app, admin_client):

        me_id = _make_master_event(app)
        rp_qual_id = _make_rp_qual(app)
        admin_client.post("/events/create", data=_event_form_data(me_id, rp_qual_id=rp_qual_id), follow_redirects=True)

        with app.app_context():
            settings = get_settings()
            settings.notify_event_changed = True
            db.session.commit()

            event = db.session.scalar(db.select(Event).where(Event.name == "Test Event"))
            event_id = event.id
            version = event.version

            # Publish so spots can be assigned
            event.status = EventStatus.PUBLISHED
            db.session.flush()

            role = db.session.scalar(db.select(Role).where(Role.name == Role.MEMBER))
            member = UserAccount(
                email="assigned_member_ecn@example.com",
                name="Assigned Member",
                is_active=True,
            )
            member.set_password("testpass")
            member.roles = [role]
            db.session.add(member)
            db.session.flush()

            spot = EventSpot(event_id=event.id)
            db.session.add(spot)
            db.session.flush()

            assignment = Assignment(spot_id=spot.id, user_id=member.id)
            db.session.add(assignment)
            db.session.commit()

        before_count = 0
        with app.app_context():
            before_count = db.session.scalar(
                db.select(db.func.count(OutboxEmail.id)).where(OutboxEmail.notification_type == "event_changed")
            )

        admin_client.post(
            f"/events/{event_id}/edit",
            data={**_event_form_data(me_id, name="Renamed Event"), "version": str(version)},
            follow_redirects=False,
        )

        with app.app_context():
            after_count = db.session.scalar(
                db.select(db.func.count(OutboxEmail.id)).where(OutboxEmail.notification_type == "event_changed")
            )
        assert after_count == before_count + 1

    def test_edit_without_change_sends_no_notification(self, app, admin_client):

        me_id = _make_master_event(app)
        rp_qual_id = _make_rp_qual(app)
        admin_client.post("/events/create", data=_event_form_data(me_id, rp_qual_id=rp_qual_id), follow_redirects=True)

        with app.app_context():
            settings = get_settings()
            settings.notify_event_changed = True
            db.session.commit()

            event = db.session.scalar(db.select(Event).where(Event.name == "Test Event"))
            event_id = event.id
            version = event.version

            event.status = EventStatus.PUBLISHED
            db.session.flush()

            role = db.session.scalar(db.select(Role).where(Role.name == Role.MEMBER))
            member = UserAccount(
                email="assigned_member_nochg@example.com",
                name="Assigned Member 2",
                is_active=True,
            )
            member.set_password("testpass")
            member.roles = [role]
            db.session.add(member)
            db.session.flush()

            spot = EventSpot(event_id=event.id)
            db.session.add(spot)
            db.session.flush()

            assignment = Assignment(spot_id=spot.id, user_id=member.id)
            db.session.add(assignment)
            db.session.commit()

        with app.app_context():
            before_count = db.session.scalar(
                db.select(db.func.count(OutboxEmail.id)).where(OutboxEmail.notification_type == "event_changed")
            )

        # Submit with identical data — no real change
        admin_client.post(
            f"/events/{event_id}/edit",
            data={**_event_form_data(me_id, name="Test Event"), "version": str(version)},
            follow_redirects=False,
        )

        with app.app_context():
            after_count = db.session.scalar(
                db.select(db.func.count(OutboxEmail.id)).where(OutboxEmail.notification_type == "event_changed")
            )
        assert after_count == before_count  # nothing enqueued


class TestEventTypes:
    """Tests for event type field — create, edit, and type filter."""

    def test_create_training_event(self, app, admin_client):
        me_id = _make_master_event(app)
        rp_qual_id = _make_rp_qual(app)
        data = {
            **_event_form_data(me_id, "Training Event", rp_qual_id=rp_qual_id),
            "event_type": "TRAINING",
            "planned_participants_count": "20",
        }
        resp = admin_client.post("/events/create", data=data, follow_redirects=False)
        assert resp.status_code == 302
        with app.app_context():
            ev = db.session.scalar(db.select(Event).where(Event.name == "Training Event"))
            assert ev is not None
            assert ev.event_type == EventType.TRAINING
            assert ev.planned_participants_count == 20

    def test_create_presentation_event(self, app, admin_client):
        me_id = _make_master_event(app)
        rp_qual_id = _make_rp_qual(app)
        data = {
            **_event_form_data(me_id, "Presentation Event", rp_qual_id=rp_qual_id),
            "event_type": "PRESENTATION",
        }
        resp = admin_client.post("/events/create", data=data, follow_redirects=False)
        assert resp.status_code == 302
        with app.app_context():
            ev = db.session.scalar(db.select(Event).where(Event.name == "Presentation Event"))
            assert ev is not None
            assert ev.event_type == EventType.PRESENTATION
            assert ev.planned_participants_count is None

    def test_default_event_type_is_medical_cover(self, app, admin_client):
        me_id = _make_master_event(app)
        rp_qual_id = _make_rp_qual(app)
        admin_client.post("/events/create", data=_event_form_data(me_id, rp_qual_id=rp_qual_id), follow_redirects=True)
        with app.app_context():
            ev = db.session.scalar(db.select(Event).where(Event.name == "Test Event"))
            assert ev.event_type == EventType.MEDICAL_COVER

    def test_type_filter_returns_only_matching_events(self, app, admin_client):
        me_id = _make_master_event(app)
        rp_qual_id = _make_rp_qual(app)
        admin_client.post(
            "/events/create",
            data={**_event_form_data(me_id, "MC Event", rp_qual_id=rp_qual_id), "event_type": "MEDICAL_COVER"},
            follow_redirects=True,
        )
        admin_client.post(
            "/events/create",
            data={**_event_form_data(me_id, "Training Event", rp_qual_id=rp_qual_id), "event_type": "TRAINING"},
            follow_redirects=True,
        )
        # Filter for TRAINING only (include DRAFT so newly created events appear)
        resp = admin_client.get("/events/?types=TRAINING&statuses=DRAFT")
        assert resp.status_code == 200
        assert b"Training Event" in resp.data
        assert b"MC Event" not in resp.data

    def test_type_badge_shown_in_event_list(self, app, admin_client):
        me_id = _make_master_event(app)
        rp_qual_id = _make_rp_qual(app)
        admin_client.post(
            "/events/create",
            data={**_event_form_data(me_id, "Badge Training", rp_qual_id=rp_qual_id), "event_type": "TRAINING"},
            follow_redirects=True,
        )
        resp = admin_client.get("/events/?statuses=DRAFT")
        assert b"Badge Training" in resp.data
        # Training badge label
        assert "Školení".encode() in resp.data


# ── Delete draft event ────────────────────────────────────────────────────────


class TestDeleteDraftEvent:
    def _create_draft(self, app) -> int:
        me_id = _make_master_event(app)
        with app.app_context():
            event = Event(
                name="Delete Me",
                master_event_id=me_id,
                start_datetime=datetime(2030, 9, 1, 8, 0, tzinfo=timezone.utc),
                end_datetime=datetime(2030, 9, 1, 16, 0, tzinfo=timezone.utc),
                status=EventStatus.DRAFT,
            )
            db.session.add(event)
            db.session.commit()
            return event.id

    def test_coordinator_can_trash_draft(self, app, coordinator_client):
        event_id = self._create_draft(app)
        response = coordinator_client.post(
            f"/events/{event_id}/archive",
            follow_redirects=False,
        )
        assert response.status_code in (200, 302)
        # Event should still exist in DB but be archived (soft-delete)
        with app.app_context():
            event = db.session.get(Event, event_id)
            assert event is not None
            assert event.archived is True

    def test_trash_draft_audited(self, app, coordinator_client):
        event_id = self._create_draft(app)
        coordinator_client.post(f"/events/{event_id}/archive")
        with app.app_context():
            entry = db.session.scalar(
                db.select(AuditLogEntry)
                .where(AuditLogEntry.entity_type == "Event")
                .where(AuditLogEntry.action_type == "archive")
                .where(AuditLogEntry.entity_id == str(event_id))
            )
            assert entry is not None

    def test_member_cannot_trash_draft(self, app, member_client):
        event_id = self._create_draft(app)
        response = member_client.post(f"/events/{event_id}/archive")
        assert response.status_code == 403
        with app.app_context():
            event = db.session.get(Event, event_id)
            assert event is not None
            assert event.archived is False

    def test_delete_ajax_returns_json(self, app, coordinator_client):
        event_id = self._create_draft(app)
        response = coordinator_client.post(
            f"/events/{event_id}/archive",
            headers={"X-CSRFToken": "test-csrf", "Accept": "application/json"},
        )
        assert response.status_code == 200
        assert response.get_json()["ok"] is True


class TestEventSplit:
    """Tests for the POST /events/<id>/split route."""

    def _create_open_event(self, app) -> int:
        """Create an ASSIGNMENTS_OPEN event and return its id."""
        with app.app_context():
            me = MasterEvent(name="Split ME")
            db.session.add(me)
            db.session.flush()
            event = Event(
                name="Splitovatelná akce",
                master_event_id=me.id,
                event_type=EventType.MEDICAL_COVER,
                status=EventStatus.ASSIGNMENTS_OPEN,
                start_datetime=datetime(2030, 7, 1, 8, 0, tzinfo=timezone.utc),
                end_datetime=datetime(2030, 7, 1, 20, 0, tzinfo=timezone.utc),
            )
            db.session.add(event)
            db.session.commit()
            return event.id

    def test_successful_split(self, app, admin_client):
        event_id = self._create_open_event(app)
        resp = admin_client.post(
            f"/events/{event_id}/split",
            data={"split_datetime": "2030-07-01T14:00"},
            follow_redirects=False,
        )
        # Should redirect to the new event
        assert resp.status_code == 302
        with app.app_context():
            part1 = db.session.get(Event, event_id)
            assert part1 is not None
            assert "1/2" in part1.name
            assert part1.end_datetime.hour in (12, 13, 14)  # 14:00 local = 12:00 UTC in summer

            # Find part2
            part2 = db.session.scalar(db.select(Event).where(Event.name.like("%2/2%")))
            assert part2 is not None
            assert part2.status == EventStatus.ASSIGNMENTS_OPEN
            assert part2.start_datetime == part1.end_datetime

    def test_split_without_datetime_flashes_error(self, app, admin_client):
        event_id = self._create_open_event(app)
        resp = admin_client.post(
            f"/events/{event_id}/split",
            data={"split_datetime": ""},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"Zadejte datum" in resp.data

    def test_split_invalid_datetime_flashes_error(self, app, admin_client):
        event_id = self._create_open_event(app)
        resp = admin_client.post(
            f"/events/{event_id}/split",
            data={"split_datetime": "not-a-date"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert "Neplatný formát".encode() in resp.data

    def test_split_time_out_of_bounds_flashes_error(self, app, admin_client):
        event_id = self._create_open_event(app)
        resp = admin_client.post(
            f"/events/{event_id}/split",
            data={"split_datetime": "2030-07-02T10:00"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert "musí být mezi" in resp.data.decode()

    def test_split_cancelled_event_redirects_with_error(self, app, admin_client):
        with app.app_context():
            me = MasterEvent(name="Split ME2")
            db.session.add(me)
            db.session.flush()
            event = Event(
                name="Zrušená akce",
                master_event_id=me.id,
                event_type=EventType.MEDICAL_COVER,
                status=EventStatus.CANCELLED,
                start_datetime=datetime(2030, 8, 1, 8, 0, tzinfo=timezone.utc),
                end_datetime=datetime(2030, 8, 1, 20, 0, tzinfo=timezone.utc),
            )
            db.session.add(event)
            db.session.commit()
            event_id = event.id
        resp = admin_client.post(
            f"/events/{event_id}/split",
            data={"split_datetime": "2030-08-01T14:00"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert "nelze rozdělit" in resp.data.decode()

    def test_split_requires_permission(self, app, member_client):
        event_id = self._create_open_event(app)
        resp = member_client.post(
            f"/events/{event_id}/split",
            data={"split_datetime": "2030-07-01T14:00"},
        )
        assert resp.status_code == 403

    def test_split_copies_spots_and_assignments(self, app, admin_client):
        with app.app_context():
            me = MasterEvent(name="Split ME3")
            db.session.add(me)
            db.session.flush()
            event = Event(
                name="Akce s pozicemi",
                master_event_id=me.id,
                event_type=EventType.MEDICAL_COVER,
                status=EventStatus.ASSIGNMENTS_OPEN,
                start_datetime=datetime(2030, 9, 1, 8, 0, tzinfo=timezone.utc),
                end_datetime=datetime(2030, 9, 1, 20, 0, tzinfo=timezone.utc),
            )
            db.session.add(event)
            db.session.flush()
            spot = EventSpot(event_id=event.id, description="Záchranář")
            db.session.add(spot)
            db.session.flush()
            # Assign the admin user to the spot
            user = db.session.scalar(db.select(UserAccount).where(UserAccount.email == "admin@test.com"))
            if user:
                assignment = Assignment(spot_id=spot.id, user_id=user.id)
                db.session.add(assignment)
            db.session.commit()
            event_id = event.id

        admin_client.post(
            f"/events/{event_id}/split",
            data={"split_datetime": "2030-09-01T14:00"},
            follow_redirects=False,
        )

        with app.app_context():
            part2 = db.session.scalar(db.select(Event).where(Event.name.like("%2/2%")))
            assert part2 is not None
            assert len(part2.spots) == 1
            assert part2.spots[0].description == "Záchranář"
            # Assignment should be copied too
            assert part2.spots[0].assignment is not None


class TestUserPickerDuplicateFiltering:
    """User picker on event detail should not show already-assigned users."""

    def test_assigned_user_excluded_from_picker(self, app, admin_client):

        me_id = _make_master_event(app)
        with app.app_context():
            me = db.session.get(MasterEvent, me_id)

            admin_role = db.session.scalar(db.select(Role).where(Role.name == Role.ADMIN))
            # Get the admin user (the one admin_client logs in as)
            admin_user = db.session.scalar(db.select(UserAccount).where(UserAccount.email == "admin@test.com"))

            # Create a second active user
            member = UserAccount(email="member_picker@test.com", name="Member Picker", is_active=True)
            member.set_password("testpass123")
            member.roles = [admin_role]
            db.session.add(member)
            db.session.flush()

            event = Event(
                name="Picker Test Event",
                master_event_id=me.id,
                start_datetime=datetime(2035, 1, 1, 10, 0, tzinfo=timezone.utc),
                end_datetime=datetime(2035, 1, 1, 18, 0, tzinfo=timezone.utc),
                status=EventStatus.ASSIGNMENTS_OPEN,
                created_by_id=admin_user.id,
            )
            db.session.add(event)
            db.session.flush()

            # Two spots
            spot1 = EventSpot(event_id=event.id, description="Spot 1")
            spot2 = EventSpot(event_id=event.id, description="Spot 2")
            db.session.add_all([spot1, spot2])
            db.session.flush()

            # Assign member to spot1
            spot1.assignment = Assignment(user_id=member.id, assigned_by_id=admin_user.id)
            db.session.commit()

            event_id = event.id
            member_id = member.id

        # Load detail page — spot2's picker should NOT contain the member
        resp = admin_client.get(f"/events/{event_id}")
        assert resp.status_code == 200
        html = resp.data.decode()

        # The assigned user's name should appear (they're shown as assigned to spot1)
        assert "Member Picker" in html
        # But should NOT appear as a selectable option value in a picker
        assert f'<option value="{member_id}">' not in html


class TestUserPickerConflictDetection:
    """Users with conflicting assignments on other events get a warning marker in the picker."""

    def _setup_two_events(
        self,
        app,
        *,
        other_start: datetime = datetime(2035, 3, 1, 10, 0, tzinfo=timezone.utc),
        other_end: datetime = datetime(2035, 3, 1, 16, 0, tzinfo=timezone.utc),
        main_start: datetime = datetime(2035, 3, 1, 12, 0, tzinfo=timezone.utc),
        main_end: datetime = datetime(2035, 3, 1, 18, 0, tzinfo=timezone.utc),
        other_status: EventStatus = EventStatus.ASSIGNMENTS_OPEN,
    ) -> tuple[int, str]:
        """Create ME + two events („main“ and „other“) + a member assigned to „other“.

        Returns (main_event_id, member_id_str).
        """
        me_id = _make_master_event(app)
        with app.app_context():
            admin_role = db.session.scalar(db.select(Role).where(Role.name == Role.ADMIN))
            admin_user = db.session.scalar(db.select(UserAccount).where(UserAccount.email == "admin@test.com"))

            member = UserAccount(email="conflict_member@test.com", name="Conflict Member", is_active=True)
            member.set_password("testpass123")
            member.roles = [admin_role]
            db.session.add(member)
            db.session.flush()

            other = Event(
                name="Other Event",
                master_event_id=me_id,
                start_datetime=other_start,
                end_datetime=other_end,
                status=other_status,
                created_by_id=admin_user.id,
            )
            db.session.add(other)
            db.session.flush()
            other_spot = EventSpot(event_id=other.id, description="Spot X")
            db.session.add(other_spot)
            db.session.flush()
            other_spot.assignment = Assignment(user_id=member.id, assigned_by_id=admin_user.id)

            main = Event(
                name="Main Event",
                master_event_id=me_id,
                start_datetime=main_start,
                end_datetime=main_end,
                status=EventStatus.ASSIGNMENTS_OPEN,
                created_by_id=admin_user.id,
            )
            db.session.add(main)
            db.session.flush()
            main_spot = EventSpot(event_id=main.id, description="Main spot")
            db.session.add(main_spot)
            db.session.commit()

            return main.id, str(member.id)

    def test_conflicting_user_gets_warning_marker(self, app, admin_client):
        main_id, member_id = self._setup_two_events(app)
        resp = admin_client.get(f"/events/{main_id}")
        assert resp.status_code == 200
        html = resp.data.decode()
        # Option carries data-conflict="1" for the conflicting member
        assert f'<option value="{member_id}" data-conflict="1"' in html
        assert "⚠️ Conflict Member" in html
        # Warning details reference the conflicting event's name
        assert "Other Event" in html

    def test_back_to_back_no_conflict(self, app, admin_client):
        main_id, member_id = self._setup_two_events(
            app,
            other_start=datetime(2035, 4, 1, 8, 0, tzinfo=timezone.utc),
            other_end=datetime(2035, 4, 1, 12, 0, tzinfo=timezone.utc),
            main_start=datetime(2035, 4, 1, 12, 0, tzinfo=timezone.utc),
            main_end=datetime(2035, 4, 1, 16, 0, tzinfo=timezone.utc),
        )
        resp = admin_client.get(f"/events/{main_id}")
        html = resp.data.decode()
        # The member should be selectable without a conflict marker
        assert f'<option value="{member_id}">Conflict Member</option>' in html
        assert f'<option value="{member_id}" data-conflict="1"' not in html

    def test_cancelled_other_event_no_conflict(self, app, admin_client):
        main_id, member_id = self._setup_two_events(app, other_status=EventStatus.CANCELLED)
        resp = admin_client.get(f"/events/{main_id}")
        html = resp.data.decode()
        assert f'<option value="{member_id}" data-conflict="1"' not in html

    def test_completed_other_event_no_conflict(self, app, admin_client):
        main_id, member_id = self._setup_two_events(app, other_status=EventStatus.COMPLETED)
        resp = admin_client.get(f"/events/{main_id}")
        html = resp.data.decode()
        assert f'<option value="{member_id}" data-conflict="1"' not in html

    def test_draft_other_event_still_conflicts(self, app, admin_client):
        main_id, member_id = self._setup_two_events(app, other_status=EventStatus.DRAFT)
        resp = admin_client.get(f"/events/{main_id}")
        html = resp.data.decode()
        assert f'<option value="{member_id}" data-conflict="1"' in html


# ── Eligible spot map structure ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "spot_desc, qual_name, expected_desc, expected_quals",
    [
        ("Záchranář", "TestQual", "Záchranář", ["TestQual"]),
        ("Řidič", None, "Řidič", []),
        (None, "TestQual", None, ["TestQual"]),
        (None, None, None, []),
    ],
    ids=["desc-and-qual", "desc-only", "qual-only", "neither"],
)
def test_spot_map_includes_description_and_qualifications(
    app, client, spot_desc, qual_name, expected_desc, expected_quals
):
    """data-spots JSON contains (spot_id, description, [qual_names], is_optional) for each eligible spot."""
    with app.app_context():
        qual = None
        if qual_name:
            qual = Qualification(name=qual_name)
            db.session.add(qual)
            db.session.flush()

        role = db.session.scalar(db.select(Role).where(Role.name == Role.MEMBER))
        user = UserAccount(email="spot_map_user@test.com", name="Spot Map User", is_active=True)
        user.set_password("testpass123")
        user.roles = [role]
        if qual:
            user.qualifications = [qual]
        db.session.add(user)
        db.session.flush()

        me = MasterEvent(name="Spot Map ME")
        db.session.add(me)
        db.session.flush()

        event = Event(
            name="Spot Map Event",
            master_event_id=me.id,
            status=EventStatus.ASSIGNMENTS_OPEN,
            start_datetime=datetime(2030, 7, 1, 10, 0, tzinfo=timezone.utc),
            end_datetime=datetime(2030, 7, 1, 18, 0, tzinfo=timezone.utc),
        )
        db.session.add(event)
        db.session.flush()

        # The spot under test (mandatory)
        test_spot = EventSpot(event_id=event.id, description=spot_desc, is_optional=False)
        if qual:
            test_spot.required_qualifications = [qual]
        db.session.add(test_spot)

        # Filler spot — no qualification required so the member is always eligible,
        # and having 2+ spots triggers the modal picker with data-spots JSON.
        filler_spot = EventSpot(event_id=event.id, description="Filler")
        db.session.add(filler_spot)

        db.session.commit()
        test_spot_id = test_spot.id

    _login(client, "spot_map_user@test.com")
    resp = client.get("/events/?statuses=ASSIGNMENTS_OPEN")
    assert resp.status_code == 200

    html = resp.data.decode()
    match = re.search(r"data-spots='([^']+)'", html)
    assert match, "data-spots attribute not found — expected 2+ eligible spots to trigger the picker"

    spots_data = json.loads(match.group(1))
    test_entry = next((s for s in spots_data if s[0] == test_spot_id), None)
    assert test_entry is not None, f"Spot {test_spot_id} not found in data-spots"

    assert test_entry[1] == expected_desc
    assert test_entry[2] == expected_quals
    assert test_entry[3] is False  # mandatory spot


class TestAssignmentsOpenDatetimeValidation:
    def test_create_rejects_open_equal_to_start(self, app, admin_client):
        me_id = _make_master_event(app)
        data = _event_form_data(me_id)
        data["start_datetime"] = "2030-06-01T10:00"
        data["end_datetime"] = "2030-06-01T18:00"
        data["assignments_open_datetime"] = "2030-06-01T10:00"
        response = admin_client.post("/events/create", data=data)
        assert response.status_code == 200
        assert "Datum otevření přihlášek musí být před začátkem akce.".encode() in response.data
        with app.app_context():
            assert db.session.query(Event).count() == 0

    def test_create_rejects_open_after_start(self, app, admin_client):
        me_id = _make_master_event(app)
        data = _event_form_data(me_id)
        data["start_datetime"] = "2030-06-01T10:00"
        data["end_datetime"] = "2030-06-01T18:00"
        data["assignments_open_datetime"] = "2030-06-01T11:00"
        response = admin_client.post("/events/create", data=data)
        assert response.status_code == 200
        assert "Datum otevření přihlášek musí být před začátkem akce.".encode() in response.data
        with app.app_context():
            assert db.session.query(Event).count() == 0

    def test_create_accepts_open_before_start(self, app, admin_client):
        me_id = _make_master_event(app)
        rp_qual_id = _make_rp_qual(app)
        data = _event_form_data(me_id, rp_qual_id=rp_qual_id)
        data["start_datetime"] = "2030-06-01T10:00"
        data["end_datetime"] = "2030-06-01T18:00"
        data["assignments_open_datetime"] = "2030-06-01T09:00"
        response = admin_client.post("/events/create", data=data, follow_redirects=False)
        assert response.status_code == 302
        with app.app_context():
            event = db.session.query(Event).first()
            assert event is not None
            assert event.assignments_open_datetime is not None

    def test_create_accepts_missing_open_datetime(self, app, admin_client):
        me_id = _make_master_event(app)
        rp_qual_id = _make_rp_qual(app)
        data = _event_form_data(me_id, rp_qual_id=rp_qual_id)
        data.pop("assignments_open_datetime", None)
        response = admin_client.post("/events/create", data=data, follow_redirects=False)
        assert response.status_code == 302
        with app.app_context():
            event = db.session.query(Event).first()
            assert event is not None
            assert event.assignments_open_datetime is None

    def test_edit_rejects_open_equal_to_start(self, app, admin_client):
        me_id = _make_master_event(app)
        event_id = _make_event_in_status(app, EventStatus.DRAFT)
        with app.app_context():
            version = db.session.get(Event, event_id).version
        data = {
            **_event_form_data(me_id),
            "start_datetime": "2030-06-01T10:00",
            "end_datetime": "2030-06-01T18:00",
            "assignments_open_datetime": "2030-06-01T10:00",
            "version": str(version),
        }
        response = admin_client.post(f"/events/{event_id}/edit", data=data)
        assert response.status_code == 200
        assert "Datum otevření přihlášek musí být před začátkem akce.".encode() in response.data

    def test_edit_rejects_open_after_start(self, app, admin_client):
        me_id = _make_master_event(app)
        event_id = _make_event_in_status(app, EventStatus.DRAFT)
        with app.app_context():
            version = db.session.get(Event, event_id).version
        data = {
            **_event_form_data(me_id),
            "start_datetime": "2030-06-01T10:00",
            "end_datetime": "2030-06-01T18:00",
            "assignments_open_datetime": "2030-06-01T11:00",
            "version": str(version),
        }
        response = admin_client.post(f"/events/{event_id}/edit", data=data)
        assert response.status_code == 200
        assert "Datum otevření přihlášek musí být před začátkem akce.".encode() in response.data

    def test_edit_accepts_open_before_start(self, app, admin_client):
        me_id = _make_master_event(app)
        event_id = _make_event_in_status(app, EventStatus.DRAFT)
        with app.app_context():
            version = db.session.get(Event, event_id).version
        data = {
            **_event_form_data(me_id),
            "start_datetime": "2030-06-01T10:00",
            "end_datetime": "2030-06-01T18:00",
            "assignments_open_datetime": "2030-06-01T09:00",
            "version": str(version),
        }
        response = admin_client.post(f"/events/{event_id}/edit", data=data, follow_redirects=False)
        assert response.status_code == 302
        with app.app_context():
            event = db.session.get(Event, event_id)
            assert event.assignments_open_datetime is not None


# ── RP spot constraint ────────────────────────────────────────────────────────


class TestEventSpotRpConstraint:
    """Verify that create/edit/delete spot routes enforce the RP-capable spot constraint."""

    def _make_event_with_rp_spot(self, app) -> tuple[int, int, int]:
        """Create an event with a mandatory RP-capable spot. Returns (event_id, spot_id, rp_qual_id)."""
        with app.app_context():
            me = MasterEvent(name="RP Constraint ME")
            db.session.add(me)
            db.session.flush()
            rp_qual = Qualification(name="RP Qual Constraint", can_be_rp=True)
            db.session.add(rp_qual)
            db.session.flush()
            event = Event(
                name="RP Constraint Event",
                master_event_id=me.id,
                status=EventStatus.DRAFT,
                start_datetime=datetime(2030, 6, 1, 10, 0, tzinfo=timezone.utc),
                end_datetime=datetime(2030, 6, 1, 18, 0, tzinfo=timezone.utc),
            )
            db.session.add(event)
            db.session.flush()
            spot = EventSpot(event_id=event.id, description="RP Spot", is_optional=False)
            spot.required_qualifications = [rp_qual]
            db.session.add(spot)
            db.session.commit()
            return event.id, spot.id, rp_qual.id

    def test_create_event_no_spots_rejected(self, app, admin_client):
        """POST to create with no spots must be rejected with a flash error."""
        me_id = _make_master_event(app)
        data = _event_form_data(me_id)
        # No spot data — spot_total defaults to 0
        response = admin_client.post("/events/create", data=data, follow_redirects=True)
        assert response.status_code == 200
        assert "Akce musí mít alespoň jednu pozici".encode() in response.data
        with app.app_context():
            assert db.session.scalar(db.select(db.func.count()).select_from(Event)) == 0

    def test_create_event_all_optional_spots_rejected(self, app, admin_client):
        """POST to create where all spots are optional must be rejected."""
        me_id = _make_master_event(app)
        rp_qual_id = _make_rp_qual(app)
        data = _event_form_data(me_id)
        data["spot_total"] = "1"
        data["spot_desc_0"] = "Volitelná pozice"
        data["spot_cred_0"] = str(rp_qual_id)
        data["spot_optional_0"] = "1"
        response = admin_client.post("/events/create", data=data, follow_redirects=True)
        assert response.status_code == 200
        assert "Akce musí mít alespoň jednu povinnou pozici".encode() in response.data
        with app.app_context():
            assert db.session.scalar(db.select(db.func.count()).select_from(Event)) == 0

    def test_create_event_mandatory_spot_no_rp_qual_rejected(self, app, admin_client):
        """POST to create with a mandatory spot but no RP-capable qual must be rejected."""
        me_id = _make_master_event(app)
        with app.app_context():
            non_rp_qual = Qualification(name="Non RP Qual", can_be_rp=False)
            db.session.add(non_rp_qual)
            db.session.commit()
            qual_id = non_rp_qual.id
        data = _event_form_data(me_id)
        data["spot_total"] = "1"
        data["spot_desc_0"] = "Povinná pozice"
        data["spot_cred_0"] = str(qual_id)
        response = admin_client.post("/events/create", data=data, follow_redirects=True)
        assert response.status_code == 200
        assert "Alespoň jedna povinná pozice musí vyžadovat kvalifikaci".encode() in response.data
        with app.app_context():
            assert db.session.scalar(db.select(db.func.count()).select_from(Event)) == 0

    def test_create_event_with_rp_qual_mandatory_spot_succeeds(self, app, admin_client):
        """POST to create with a mandatory spot with RP-capable qual must succeed."""
        me_id = _make_master_event(app)
        rp_qual_id = _make_rp_qual(app)
        data = _event_form_data(me_id, rp_qual_id=rp_qual_id)
        response = admin_client.post("/events/create", data=data, follow_redirects=False)
        assert response.status_code == 302
        with app.app_context():
            assert db.session.scalar(db.select(db.func.count()).select_from(Event)) == 1

    def test_add_spot_blocked_when_no_rp_spot_would_remain(self, app, admin_client):
        """Adding a non-RP-capable spot when no other RP-capable mandatory spot exists is blocked.

        Note: adding a spot never removes existing spots, so this tests the case where
        the event already has no RP-capable mandatory spots and we try to add a non-RP one.
        In practice, the event itself was bypassed (created directly in DB).
        We directly create an event without a valid RP spot and then verify add_spot validates.
        """
        with app.app_context():
            me = MasterEvent(name="Add Spot Block ME")
            db.session.add(me)
            db.session.flush()
            event = Event(
                name="Add Spot Block Event",
                master_event_id=me.id,
                status=EventStatus.DRAFT,
                start_datetime=datetime(2030, 6, 1, 10, 0, tzinfo=timezone.utc),
                end_datetime=datetime(2030, 6, 1, 18, 0, tzinfo=timezone.utc),
            )
            db.session.add(event)
            db.session.commit()
            event_id = event.id

        # Add a non-RP optional spot — event has no spots yet, so constraint will fail
        response = admin_client.post(
            f"/events/{event_id}/spots/add",
            data={"description": "Pomocník", "quantity": "1", "is_optional": "1"},
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "Akce musí mít alespoň jednu povinnou pozici".encode() in response.data
        with app.app_context():
            count = db.session.scalar(
                db.select(db.func.count()).select_from(EventSpot).where(EventSpot.event_id == event_id)
            )
            assert count == 0

    def test_delete_last_rp_capable_mandatory_spot_blocked(self, app, admin_client):
        """Deleting the only mandatory RP-capable spot must be blocked with a flash error."""
        event_id, spot_id, _rp_qual_id = self._make_event_with_rp_spot(app)

        response = admin_client.post(
            f"/events/{event_id}/spots/{spot_id}/delete",
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "Akce musí mít alespoň jednu pozici".encode() in response.data
        with app.app_context():
            assert db.session.get(EventSpot, spot_id) is not None

    def test_edit_spot_removing_last_rp_qual_blocked(self, app, admin_client):
        """Editing a spot to remove the only RP-capable qual must be blocked."""
        event_id, spot_id, _rp_qual_id = self._make_event_with_rp_spot(app)

        # Edit the spot to have no qualifications
        response = admin_client.post(
            f"/events/{event_id}/spots/{spot_id}/edit",
            data={"description": "RP Spot"},
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "Alespoň jedna povinná pozice musí vyžadovat kvalifikaci".encode() in response.data

    def test_edit_spot_making_only_mandatory_rp_spot_optional_blocked(self, app, admin_client):
        """Editing the only mandatory RP spot to be optional must be blocked."""
        event_id, spot_id, rp_qual_id = self._make_event_with_rp_spot(app)

        # Edit the spot to be optional (keeping the RP qual)
        response = admin_client.post(
            f"/events/{event_id}/spots/{spot_id}/edit",
            data={"description": "RP Spot", "qualification_ids": [str(rp_qual_id)], "is_optional": "1"},
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "Akce musí mít alespoň jednu povinnou pozici".encode() in response.data

    def test_create_event_mandatory_spot_no_qualifications_rejected(self, app, admin_client):
        """POST to create with a mandatory spot that has no spot_cred_* at all must be rejected."""
        me_id = _make_master_event(app)
        data = _event_form_data(me_id)
        data["spot_total"] = "1"
        data["spot_desc_0"] = "Povinná pozice bez kvalifikace"
        # spot_cred_0 is intentionally absent — no qualification at all
        response = admin_client.post("/events/create", data=data, follow_redirects=True)
        assert response.status_code == 200
        assert "Alespoň jedna povinná pozice musí vyžadovat kvalifikaci".encode() in response.data
        with app.app_context():
            assert db.session.scalar(db.select(db.func.count()).select_from(Event)) == 0

    def test_add_spot_to_event_with_rp_spot_succeeds(self, app, admin_client):
        """Adding a non-RP optional spot to an event that already has a valid mandatory RP spot must succeed."""
        event_id, _spot_id, _rp_qual_id = self._make_event_with_rp_spot(app)

        # Add a second optional spot (no RP qual needed — the existing mandatory RP spot satisfies the constraint)
        response = admin_client.post(
            f"/events/{event_id}/spots/add",
            data={"description": "Pomocník", "quantity": "1", "is_optional": "1"},
            follow_redirects=False,
        )
        assert response.status_code == 302
        with app.app_context():
            count = db.session.scalar(
                db.select(db.func.count()).select_from(EventSpot).where(EventSpot.event_id == event_id)
            )
            assert count == 2


# ── Bulk printout ─────────────────────────────────────────────────────────────

XLSX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _post_bulk_printout(client, event_ids: list[int]) -> object:
    csrf = _get_csrf(client, "/events/")
    data = {"csrf_token": csrf}
    for eid in event_ids:
        data.setdefault("event_ids", [])
        if isinstance(data["event_ids"], list):
            data["event_ids"].append(str(eid))
    return client.post("/events/printout", data=data, follow_redirects=False)


class TestBulkPrintout:
    def test_unauthenticated_redirected(self, client):
        resp = client.post("/events/printout", follow_redirects=False)
        assert resp.status_code == 302

    def test_no_ids_redirects_with_warning(self, app, member_client):
        resp = _post_bulk_printout(member_client, [])
        assert resp.status_code == 302

    def test_valid_events_return_xlsx(self, app, member_client):
        event_id = _make_event_in_status(app, EventStatus.PUBLISHED, name="Bulk Printout OK")
        resp = _post_bulk_printout(member_client, [event_id])
        assert resp.status_code == 200
        assert resp.content_type == XLSX_CONTENT_TYPE

    def test_draft_events_excluded_returns_redirect(self, app, member_client):
        event_id = _make_event_in_status(app, EventStatus.DRAFT, name="Bulk Printout Draft")
        resp = _post_bulk_printout(member_client, [event_id])
        # All submitted events filtered out → warning flash + redirect
        assert resp.status_code == 302

    def test_archived_events_excluded_returns_redirect(self, app, member_client):
        event_id = _make_event_in_status(app, EventStatus.COMPLETED, name="Bulk Printout Archived")
        with app.app_context():
            ev = db.session.get(Event, event_id)
            ev.archived = True
            db.session.commit()
        resp = _post_bulk_printout(member_client, [event_id])
        assert resp.status_code == 302

    def test_mixed_selection_excludes_draft(self, app, member_client):
        published_id = _make_event_in_status(app, EventStatus.PUBLISHED, name="Bulk Printout Published")
        draft_id = _make_event_in_status(app, EventStatus.DRAFT, name="Bulk Printout Draft Mix")
        resp = _post_bulk_printout(member_client, [published_id, draft_id])
        # Valid events present → xlsx returned (draft silently dropped)
        assert resp.status_code == 200
        assert resp.content_type == XLSX_CONTENT_TYPE

    def test_user_without_report_view_gets_403(self, app, client):
        """A role that lacks report.view must be denied, not just redirected."""
        with app.app_context():
            role = db.session.scalar(db.select(Role).where(Role.name == Role.DEBRIEFING_MANAGER))
            user = UserAccount(email="dm_printout@test.com", name="DM Printout", is_active=True)
            user.set_password("testpass123")
            user.roles = [role]
            db.session.add(user)
            db.session.commit()
        _login(client, "dm_printout@test.com")
        event_id = _make_event_in_status(app, EventStatus.PUBLISHED, name="Bulk Printout DM")
        resp = _post_bulk_printout(client, [event_id])
        assert resp.status_code == 403


class TestForMeFilter:
    """Server-side 'pro mě' filter: pagination count must match visible events (issue #326)."""

    def _setup(self, app):
        """Create member + 1 eligible event (no-req spot) + 1 ineligible event (req spot user can't fill)."""
        with app.app_context():
            me = MasterEvent(name="ForMe ME")
            db.session.add(me)
            db.session.flush()

            role = db.session.scalar(db.select(Role).where(Role.name == Role.MEMBER))
            user = UserAccount(email="forme@test.com", name="ForMe User", is_active=True)
            user.set_password("testpass123")
            user.roles = [role]
            db.session.add(user)

            # eligible event: spot with no required qualifications
            ev_eligible = Event(
                name="Eligible Event",
                master_event_id=me.id,
                status=EventStatus.ASSIGNMENTS_OPEN,
                start_datetime=datetime(2030, 7, 1, 10, 0, tzinfo=timezone.utc),
                end_datetime=datetime(2030, 7, 1, 18, 0, tzinfo=timezone.utc),
            )
            db.session.add(ev_eligible)
            db.session.flush()
            db.session.add(EventSpot(event_id=ev_eligible.id))

            # ineligible event: spot requires a qual the user doesn't have
            other_qual = Qualification(name="ForMe Other Qual")
            db.session.add(other_qual)
            db.session.flush()
            ev_ineligible = Event(
                name="Ineligible Event",
                master_event_id=me.id,
                status=EventStatus.ASSIGNMENTS_OPEN,
                start_datetime=datetime(2030, 7, 2, 10, 0, tzinfo=timezone.utc),
                end_datetime=datetime(2030, 7, 2, 18, 0, tzinfo=timezone.utc),
            )
            db.session.add(ev_ineligible)
            db.session.flush()
            spot = EventSpot(event_id=ev_ineligible.id)
            db.session.add(spot)
            db.session.flush()
            spot.required_qualifications = [other_qual]

            db.session.commit()
            return user.email

    def test_for_me_shows_only_eligible_events(self, app, client):
        email = self._setup(app)
        _login(client, email)
        resp = client.get("/events/?statuses=ASSIGNMENTS_OPEN&for_me=1")
        assert resp.status_code == 200
        assert b"Eligible Event" in resp.data
        assert b"Ineligible Event" not in resp.data

    def test_for_me_off_shows_all_events(self, app, client):
        email = self._setup(app)
        _login(client, email)
        resp = client.get("/events/?statuses=ASSIGNMENTS_OPEN")
        assert resp.status_code == 200
        assert b"Eligible Event" in resp.data
        assert b"Ineligible Event" in resp.data

    def test_for_me_excluded_when_no_permission(self, app, client):
        """A user without event.assign_own cannot activate the for_me filter.

        VIEWER has event.view but not event.assign_own — the for_me param must be
        silently ignored (page loads normally, no 403 or redirect).
        """
        with app.app_context():
            me = MasterEvent(name="ForMe Viewer ME")
            db.session.add(me)
            db.session.flush()
            ev = Event(
                name="Viewer Visible Event",
                master_event_id=me.id,
                status=EventStatus.ASSIGNMENTS_OPEN,
                start_datetime=datetime(2030, 7, 10, 10, 0, tzinfo=timezone.utc),
                end_datetime=datetime(2030, 7, 10, 18, 0, tzinfo=timezone.utc),
            )
            db.session.add(ev)

            role = db.session.scalar(db.select(Role).where(Role.name == Role.VIEWER))
            user = UserAccount(email="viewer_forme@test.com", name="Viewer ForMe", is_active=True)
            user.set_password("testpass123")
            user.roles = [role]
            db.session.add(user)
            db.session.commit()
            email = user.email
        _login(client, email)
        # Viewer passes for_me=1 but lacks event.assign_own — param is silently ignored
        resp = client.get("/events/?statuses=ASSIGNMENTS_OPEN&for_me=1")
        assert resp.status_code == 200
        # The event is still present — the for_me filter was not applied
        assert b"Viewer Visible Event" in resp.data

    def test_for_me_occupied_spot_excluded(self, app, client):
        """An event where the user's only eligible spot is already taken is excluded."""
        with app.app_context():
            me = MasterEvent(name="Occupied ME")
            db.session.add(me)
            db.session.flush()
            role = db.session.scalar(db.select(Role).where(Role.name == Role.MEMBER))

            user = UserAccount(email="forme_occ@test.com", name="ForMe Occ", is_active=True)
            user.set_password("testpass123")
            user.roles = [role]
            db.session.add(user)

            other = UserAccount(email="other_occ@test.com", name="Other Occ", is_active=True)
            other.set_password("testpass123")
            other.roles = [role]
            db.session.add(other)

            ev = Event(
                name="Occupied Event",
                master_event_id=me.id,
                status=EventStatus.ASSIGNMENTS_OPEN,
                start_datetime=datetime(2030, 7, 3, 10, 0, tzinfo=timezone.utc),
                end_datetime=datetime(2030, 7, 3, 18, 0, tzinfo=timezone.utc),
            )
            db.session.add(ev)
            db.session.flush()
            spot = EventSpot(event_id=ev.id)
            db.session.add(spot)
            db.session.flush()
            other_id = other.id
            db.session.add(Assignment(spot_id=spot.id, user_id=other_id))
            db.session.commit()
            email = user.email

        _login(client, email)
        resp = client.get("/events/?statuses=ASSIGNMENTS_OPEN&for_me=1")
        assert resp.status_code == 200
        assert b"Occupied Event" not in resp.data

    def test_for_me_zero_eligible_events_returns_empty_page(self, app, client):
        """When no ASSIGNMENTS_OPEN event has a claimable spot for the user,
        for_me=1 returns 200 with an empty list — exercises the [-1] sentinel path."""
        with app.app_context():
            me = MasterEvent(name="ZeroElig ME")
            db.session.add(me)
            db.session.flush()
            qual = Qualification(name="ZeroElig Qual")
            db.session.add(qual)
            db.session.flush()
            ev = Event(
                name="ZeroElig Event",
                master_event_id=me.id,
                status=EventStatus.ASSIGNMENTS_OPEN,
                start_datetime=datetime(2030, 8, 1, 10, 0, tzinfo=timezone.utc),
                end_datetime=datetime(2030, 8, 1, 18, 0, tzinfo=timezone.utc),
            )
            db.session.add(ev)
            db.session.flush()
            spot = EventSpot(event_id=ev.id)
            db.session.add(spot)
            db.session.flush()
            spot.required_qualifications = [qual]
            role = db.session.scalar(db.select(Role).where(Role.name == Role.MEMBER))
            user = UserAccount(email="zeroeleg@test.com", name="ZeroElig User", is_active=True)
            user.set_password("testpass123")
            user.roles = [role]
            db.session.add(user)
            db.session.commit()
            email = user.email
        _login(client, email)
        resp = client.get("/events/?statuses=ASSIGNMENTS_OPEN&for_me=1")
        assert resp.status_code == 200
        assert b"ZeroElig Event" not in resp.data


# ── Archive (soft-delete) ─────────────────────────────────────────────────────


class TestEventArchive:
    def _create_published(self, app) -> int:
        return _make_event_in_status(app, EventStatus.PUBLISHED, name="Archive Me")

    def test_coordinator_can_archive_published_event(self, app, coordinator_client):
        event_id = self._create_published(app)
        response = coordinator_client.post(
            f"/events/{event_id}/archive",
            follow_redirects=False,
        )
        assert response.status_code == 302
        with app.app_context():
            event = db.session.get(Event, event_id)
            assert event is not None
            assert event.archived is True

    def test_member_cannot_archive_event(self, app, member_client):
        event_id = self._create_published(app)
        response = member_client.post(f"/events/{event_id}/archive")
        assert response.status_code == 403

    def test_archive_sets_archived_true_without_deleting(self, app, coordinator_client):
        event_id = self._create_published(app)
        coordinator_client.post(f"/events/{event_id}/archive", follow_redirects=False)
        with app.app_context():
            event = db.session.get(Event, event_id)
            assert event is not None
            assert event.archived is True

    def test_archive_writes_audit_log_with_archive_action(self, app, coordinator_client):
        event_id = self._create_published(app)
        coordinator_client.post(f"/events/{event_id}/archive", follow_redirects=False)
        with app.app_context():
            entry = db.session.scalar(
                db.select(AuditLogEntry)
                .where(AuditLogEntry.entity_type == "Event")
                .where(AuditLogEntry.action_type == "archive")
                .where(AuditLogEntry.entity_id == str(event_id))
            )
            assert entry is not None

    def test_archive_already_archived_event_shows_warning(self, app, coordinator_client):
        with app.app_context():
            me = MasterEvent(name="Archive Warning ME")
            db.session.add(me)
            db.session.flush()
            event = Event(
                name="Already Archived",
                master_event_id=me.id,
                status=EventStatus.PUBLISHED,
                archived=True,
                start_datetime=datetime(2030, 9, 1, 8, 0, tzinfo=timezone.utc),
                end_datetime=datetime(2030, 9, 1, 16, 0, tzinfo=timezone.utc),
            )
            db.session.add(event)
            db.session.commit()
            event_id = event.id
        response = coordinator_client.post(
            f"/events/{event_id}/archive",
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "již archivována" in response.data.decode()


# ── Unarchive (restore from archive) ─────────────────────────────────────────


class TestEventUnarchive:
    def _create_archived(self, app) -> int:
        with app.app_context():
            me = MasterEvent(name="Unarchive ME")
            db.session.add(me)
            db.session.flush()
            event = Event(
                name="Archived Event",
                master_event_id=me.id,
                status=EventStatus.PUBLISHED,
                archived=True,
                start_datetime=datetime(2030, 9, 1, 8, 0, tzinfo=timezone.utc),
                end_datetime=datetime(2030, 9, 1, 16, 0, tzinfo=timezone.utc),
            )
            db.session.add(event)
            db.session.commit()
            return event.id

    def test_coordinator_can_unarchive_archived_event(self, app, coordinator_client):
        event_id = self._create_archived(app)
        response = coordinator_client.post(
            f"/events/{event_id}/unarchive",
            follow_redirects=False,
        )
        assert response.status_code == 302
        with app.app_context():
            event = db.session.get(Event, event_id)
            assert event is not None
            assert event.archived is False

    def test_member_cannot_unarchive(self, app, member_client):
        event_id = self._create_archived(app)
        response = member_client.post(f"/events/{event_id}/unarchive")
        assert response.status_code == 403

    def test_unarchive_non_archived_event_shows_warning(self, app, coordinator_client):
        event_id = _make_event_in_status(app, EventStatus.PUBLISHED, name="Not Archived")
        response = coordinator_client.post(
            f"/events/{event_id}/unarchive",
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "není archivována" in response.data.decode()

    def test_unarchive_writes_audit_log(self, app, coordinator_client):
        event_id = self._create_archived(app)
        coordinator_client.post(f"/events/{event_id}/unarchive", follow_redirects=False)
        with app.app_context():
            entry = db.session.scalar(
                db.select(AuditLogEntry)
                .where(AuditLogEntry.entity_type == "Event")
                .where(AuditLogEntry.action_type == "unarchive")
                .where(AuditLogEntry.entity_id == str(event_id))
            )
            assert entry is not None


# ── Equipment plan validation on create/edit ──────────────────────────────────


class TestEquipmentPlanOnCreate:
    """Equipment plan fields are validated and applied when creating an event."""

    def _make_eq_type(self, app, name: str = "Test EQ Type") -> int:
        with app.app_context():
            et = EquipmentType(name=name)
            db.session.add(et)
            db.session.commit()
            return et.id

    def _make_eq_item(self, app, type_id: int, name: str = "Test Item") -> int:

        with app.app_context():
            item = EquipmentItem(name=name, type_id=type_id)
            db.session.add(item)
            db.session.commit()
            return item.id

    def _create_data(self, app, rp_qual_id: int, **eq_fields) -> dict:
        me_id = _make_master_event(app)
        data = _event_form_data(me_id, rp_qual_id=rp_qual_id)
        data.update(eq_fields)
        return data

    def test_create_without_equipment_succeeds(self, app, admin_client):
        rp_qual_id = _make_rp_qual(app)
        me_id = _make_master_event(app)
        data = _event_form_data(me_id, rp_qual_id=rp_qual_id)
        data["eq_total"] = "0"
        resp = admin_client.post("/events/create", data=data, follow_redirects=False)
        assert resp.status_code == 302

    def test_create_with_sufficient_equipment_succeeds(self, app, admin_client):
        rp_qual_id = _make_rp_qual(app)
        type_id = self._make_eq_type(app, "Sufficient EQ")
        self._make_eq_item(app, type_id)
        me_id = _make_master_event(app)
        data = _event_form_data(me_id, rp_qual_id=rp_qual_id)
        data.update({"eq_total": "1", "eq_type_id_0": str(type_id), "eq_qty_0": "1"})
        resp = admin_client.post("/events/create", data=data, follow_redirects=False)
        assert resp.status_code == 302
        with app.app_context():
            event = db.session.scalar(db.select(Event).where(Event.name == "Test Event"))
            assert event is not None
            assert len(event.equipment_plans) == 1
            assert event.equipment_plans[0].quantity_required == 1

    def test_create_blocked_when_equipment_shortage(self, app, admin_client):
        rp_qual_id = _make_rp_qual(app)
        type_id = self._make_eq_type(app, "Shortage EQ")
        # No items → pool = 0, requesting 1 → conflict
        me_id = _make_master_event(app)
        data = _event_form_data(me_id, rp_qual_id=rp_qual_id)
        data.update({"eq_total": "1", "eq_type_id_0": str(type_id), "eq_qty_0": "1"})
        resp = admin_client.post("/events/create", data=data, follow_redirects=True)
        assert resp.status_code == 200
        assert "Nedostatek vybavení" in resp.data.decode()
        with app.app_context():
            assert db.session.scalar(db.select(db.func.count()).select_from(Event)) == 0

    def test_create_form_preserved_on_equipment_error(self, app, admin_client):
        """Form fields survive a validation error so the user doesn't lose data."""
        rp_qual_id = _make_rp_qual(app)
        type_id = self._make_eq_type(app, "Preserved EQ")
        me_id = _make_master_event(app)
        data = _event_form_data(me_id, name="Preserved Name", rp_qual_id=rp_qual_id)
        data.update({"eq_total": "1", "eq_type_id_0": str(type_id), "eq_qty_0": "1"})
        resp = admin_client.post("/events/create", data=data, follow_redirects=True)
        body = resp.data.decode()
        assert "Preserved Name" in body  # event name preserved

    def test_equipment_conflict_message_contains_event_link(self, app, admin_client):
        """Error message links to the conflicting event."""
        rp_qual_id = _make_rp_qual(app)
        type_id = self._make_eq_type(app, "Link EQ")
        self._make_eq_item(app, type_id)
        # Consume the only item with an existing event
        existing_id = _make_event_in_status(app, EventStatus.PUBLISHED, name="Consuming Event")
        with app.app_context():

            db.session.add(EventEquipmentPlan(event_id=existing_id, equipment_type_id=type_id, quantity_required=1))
            db.session.commit()
        # Now try to create another event wanting the same item
        me_id = _make_master_event(app)
        data = _event_form_data(me_id, rp_qual_id=rp_qual_id)
        data["start_datetime"] = "2030-06-01T10:00"
        data["end_datetime"] = "2030-06-01T18:00"
        data.update({"eq_total": "1", "eq_type_id_0": str(type_id), "eq_qty_0": "1"})
        resp = admin_client.post("/events/create", data=data, follow_redirects=True)
        body = resp.data.decode()
        assert "Nedostatek vybavení" in body
        assert "Consuming Event" in body  # link to conflicting event


class TestEquipmentPlanOnEdit:
    """Equipment plans are validated and updated when editing an event."""

    def _setup(self, app, admin_client):
        """Create event + equipment type + item. Returns (event_id, type_id, version, me_id, rp_qual_id)."""
        rp_qual_id = _make_rp_qual(app)
        me_id = _make_master_event(app)
        data = _event_form_data(me_id, rp_qual_id=rp_qual_id)
        admin_client.post("/events/create", data=data, follow_redirects=False)
        with app.app_context():
            event = db.session.scalar(db.select(Event).where(Event.name == "Test Event"))
            event_id = event.id
            version = event.version
            et = EquipmentType(name="Edit EQ")
            db.session.add(et)
            db.session.flush()

            db.session.add(EquipmentItem(name="Edit Item", type_id=et.id))
            db.session.commit()
            type_id = et.id
        return event_id, type_id, version, me_id, rp_qual_id

    def test_edit_adds_equipment_plan(self, app, admin_client):
        event_id, type_id, version, me_id, rp_qual_id = self._setup(app, admin_client)
        data = _event_form_data(me_id, rp_qual_id=rp_qual_id)
        data["version"] = str(version)
        data.update({"eq_total": "1", "eq_type_id_0": str(type_id), "eq_qty_0": "1"})
        resp = admin_client.post(f"/events/{event_id}/edit", data=data, follow_redirects=False)
        assert resp.status_code == 302
        with app.app_context():
            event = db.session.get(Event, event_id)
            assert len(event.equipment_plans) == 1

    def test_edit_blocked_on_equipment_shortage(self, app, admin_client):
        event_id, type_id, version, me_id, rp_qual_id = self._setup(app, admin_client)
        data = _event_form_data(me_id, rp_qual_id=rp_qual_id)
        data["version"] = str(version)
        # Request 2 but only 1 item exists
        data.update({"eq_total": "1", "eq_type_id_0": str(type_id), "eq_qty_0": "2"})
        resp = admin_client.post(f"/events/{event_id}/edit", data=data, follow_redirects=True)
        assert resp.status_code == 200
        assert "Nedostatek vybavení" in resp.data.decode()
        with app.app_context():
            event = db.session.get(Event, event_id)
            assert len(event.equipment_plans) == 0  # unchanged

    def test_edit_clears_equipment_plan_when_eq_total_zero(self, app, admin_client):
        event_id, type_id, version, me_id, rp_qual_id = self._setup(app, admin_client)
        # First add a plan
        with app.app_context():
            db.session.add(EventEquipmentPlan(event_id=event_id, equipment_type_id=type_id, quantity_required=1))
            db.session.commit()
        data = _event_form_data(me_id, rp_qual_id=rp_qual_id)
        with app.app_context():
            version = db.session.get(Event, event_id).version
        data["version"] = str(version)
        data["eq_total"] = "0"  # remove all plans
        resp = admin_client.post(f"/events/{event_id}/edit", data=data, follow_redirects=False)
        assert resp.status_code == 302
        with app.app_context():
            event = db.session.get(Event, event_id)
            assert len(event.equipment_plans) == 0
