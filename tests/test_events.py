"""Tests for event CRUD and lifecycle transitions."""

import json
import re
from datetime import datetime, timezone

import pytest
import sqlalchemy as sa

from app.extensions import db
from app.models.assignment import Assignment
from app.models.audit import AuditLogEntry
from app.models.equipment import (
    EquipmentCategory,
    EquipmentItem,
    EquipmentType,
    EventEquipmentAssignment,
    EventEquipmentPlan,
)
from app.models.event import Event, EventSpot, EventStatus, EventType
from app.models.master_event import MasterEvent
from app.models.outbox import OutboxEmail
from app.models.qualification import Qualification
from app.models.role import Role
from app.models.settings import get_settings
from app.models.user import UserAccount
from tests.conftest import _login, _make_event_in_status, _make_master_event, _make_rp_qual


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


class TestEventCreate:
    def test_create_page_loads_for_admin(self, admin_client):
        response = admin_client.get("/events/create")
        assert response.status_code == 200

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
            et = EquipmentType(name="Plan Type", category=EquipmentCategory.SHARED)
            db.session.add(et)
            db.session.commit()
            return event_id, et.id

    def test_plan_add_404_for_missing_event(self, app, admin_client):
        with app.app_context():
            et = EquipmentType(name="Plan T2", category=EquipmentCategory.SHARED)
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


class TestEquipmentAssignExtended:
    def _make_event_type_item(self, app):
        event_id = _make_event_in_status(app, EventStatus.DRAFT)
        with app.app_context():
            et = EquipmentType(name="Assign Type", category=EquipmentCategory.SHARED)
            db.session.add(et)
            db.session.flush()
            item = EquipmentItem(name="Assign Item", type_id=et.id)
            db.session.add(item)
            db.session.commit()
            return event_id, item.id

    def test_assign_duplicate_item_flashes(self, app, admin_client):
        event_id, item_id = self._make_event_type_item(app)
        admin_client.post(
            f"/events/{event_id}/equipment/assign",
            data={"item_id": str(item_id)},
            follow_redirects=True,
        )
        response = admin_client.post(
            f"/events/{event_id}/equipment/assign",
            data={"item_id": str(item_id)},
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "přiřazena" in response.data.decode() or "již" in response.data.decode()

    def test_unassign_no_item_id_flashes(self, app, admin_client):
        event_id = _make_event_in_status(app, EventStatus.DRAFT)
        response = admin_client.post(f"/events/{event_id}/equipment/unassign", data={}, follow_redirects=True)
        assert response.status_code == 200
        assert "Chybí" in response.data.decode() or "položka" in response.data.decode()

    def test_unassign_not_found_flashes(self, app, admin_client):
        event_id = _make_event_in_status(app, EventStatus.DRAFT)
        response = admin_client.post(
            f"/events/{event_id}/equipment/unassign",
            data={"item_id": "999999"},
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "nenalezeno" in response.data.decode() or "přiřazení" in response.data.decode()

    def test_unassign_succeeds(self, app, admin_client):
        event_id, item_id = self._make_event_type_item(app)
        admin_client.post(
            f"/events/{event_id}/equipment/assign",
            data={"item_id": str(item_id)},
            follow_redirects=True,
        )
        response = admin_client.post(
            f"/events/{event_id}/equipment/unassign",
            data={"item_id": str(item_id)},
            follow_redirects=False,
        )
        assert response.status_code == 302
        with app.app_context():
            ea = db.session.scalar(
                sa.select(EventEquipmentAssignment).where(
                    EventEquipmentAssignment.event_id == event_id,
                    EventEquipmentAssignment.equipment_item_id == item_id,
                )
            )
            assert ea is None


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

    def test_coordinator_can_delete_draft(self, app, coordinator_client):
        event_id = self._create_draft(app)
        response = coordinator_client.post(
            f"/events/{event_id}/delete",
            follow_redirects=False,
        )
        assert response.status_code in (200, 302)
        with app.app_context():
            assert db.session.get(Event, event_id) is None

    def test_delete_draft_audited(self, app, coordinator_client):
        event_id = self._create_draft(app)
        coordinator_client.post(f"/events/{event_id}/delete")
        with app.app_context():
            entry = db.session.scalar(
                db.select(AuditLogEntry)
                .where(AuditLogEntry.entity_type == "Event")
                .where(AuditLogEntry.action_type == "delete")
                .where(AuditLogEntry.entity_id == str(event_id))
            )
            assert entry is not None

    def test_cannot_delete_non_draft(self, app, coordinator_client):
        me_id = _make_master_event(app)
        with app.app_context():
            event = Event(
                name="Published Event",
                master_event_id=me_id,
                start_datetime=datetime(2030, 9, 2, 8, 0, tzinfo=timezone.utc),
                end_datetime=datetime(2030, 9, 2, 16, 0, tzinfo=timezone.utc),
                status=EventStatus.PUBLISHED,
            )
            db.session.add(event)
            db.session.commit()
            event_id = event.id
        response = coordinator_client.post(
            f"/events/{event_id}/delete",
            follow_redirects=False,
        )
        # Redirected back to detail with error flash
        assert response.status_code == 302
        with app.app_context():
            assert db.session.get(Event, event_id) is not None

    def test_member_cannot_delete_draft(self, app, member_client):
        event_id = self._create_draft(app)
        response = member_client.post(f"/events/{event_id}/delete")
        assert response.status_code == 403
        with app.app_context():
            assert db.session.get(Event, event_id) is not None

    def test_delete_ajax_returns_json(self, app, coordinator_client):
        event_id = self._create_draft(app)
        response = coordinator_client.post(
            f"/events/{event_id}/delete",
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
        data = _event_form_data(me_id)
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
        data = _event_form_data(me_id)
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
