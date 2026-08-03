"""Tests for equipment inventory CRUD and permissions."""

from datetime import datetime, timedelta, timezone

from app.extensions import db
from app.models.equipment import (
    EquipmentItem,
    EquipmentType,
    EventEquipmentPlan,
)
from app.models.event import Event, EventStatus
from app.models.master_event import MasterEvent
from app.models.user import UserAccount
from app.queries import available_quantity_for_type
from tests.conftest import _make_event_in_status


def _make_type(app, name: str = "Test Type") -> int:
    with app.app_context():
        et = EquipmentType(name=name)
        db.session.add(et)
        db.session.commit()
        return et.id


def _make_item(app, type_id: int, name: str = "Test Item") -> int:
    with app.app_context():
        item = EquipmentItem(name=name, type_id=type_id)
        db.session.add(item)
        db.session.commit()
        return item.id


class TestEquipmentTypeList:
    def test_list_accessible_for_admin(self, admin_client):
        response = admin_client.get("/equipment/")
        assert response.status_code == 200

    def test_list_accessible_for_member(self, member_client):
        response = member_client.get("/equipment/")
        assert response.status_code == 200

    def test_list_requires_login(self, client):
        response = client.get("/equipment/", follow_redirects=False)
        assert response.status_code == 302


class TestEquipmentTypeCreate:
    def test_create_page_loads_for_admin(self, admin_client):
        response = admin_client.get("/equipment/types/create")
        assert response.status_code == 200

    def test_create_page_forbidden_for_member(self, member_client):
        response = member_client.get("/equipment/types/create")
        assert response.status_code == 403

    def test_admin_can_create_type(self, app, admin_client):
        response = admin_client.post(
            "/equipment/types/create",
            data={"name": "Defibrilátor", "description": ""},
            follow_redirects=False,
        )
        assert response.status_code == 302
        with app.app_context():
            et = db.session.scalar(db.select(EquipmentType).where(EquipmentType.name == "Defibrilátor"))
            assert et is not None

    def test_create_type_missing_name(self, admin_client):
        response = admin_client.post(
            "/equipment/types/create",
            data={"name": ""},
            follow_redirects=True,
        )
        assert response.status_code == 200

    def test_member_cannot_create_type(self, member_client):
        response = member_client.post(
            "/equipment/types/create",
            data={"name": "Test"},
        )
        assert response.status_code == 403


class TestEquipmentTypeEdit:
    def test_admin_can_edit_type(self, app, admin_client):
        type_id = _make_type(app, "Old Name")
        response = admin_client.post(
            f"/equipment/types/{type_id}/edit",
            data={"name": "New Name", "version": "1"},
            follow_redirects=False,
        )
        assert response.status_code == 302
        with app.app_context():
            et = db.session.get(EquipmentType, type_id)
            assert et.name == "New Name"

    def test_optimistic_lock_conflict(self, app, admin_client):
        type_id = _make_type(app)
        response = admin_client.post(
            f"/equipment/types/{type_id}/edit",
            data={"name": "New Name", "version": "99"},
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "mezitím změněn" in response.data.decode("utf-8")


class TestEquipmentTypeDelete:
    def test_admin_can_delete_empty_type(self, app, admin_client):
        type_id = _make_type(app)
        response = admin_client.post(
            f"/equipment/types/{type_id}/delete",
            follow_redirects=False,
        )
        assert response.status_code == 302
        with app.app_context():
            assert db.session.get(EquipmentType, type_id) is None

    def test_cannot_delete_type_with_items(self, app, admin_client):
        type_id = _make_type(app)
        _make_item(app, type_id)
        response = admin_client.post(
            f"/equipment/types/{type_id}/delete",
            follow_redirects=True,
        )
        assert response.status_code == 200
        with app.app_context():
            assert db.session.get(EquipmentType, type_id) is not None


class TestEquipmentItemCreate:
    def test_admin_can_create_item(self, app, admin_client):
        type_id = _make_type(app)
        response = admin_client.post(
            "/equipment/items/create",
            data={"name": "AED Unit 1", "type_id": str(type_id), "serial_number": "SN001"},
            follow_redirects=False,
        )
        assert response.status_code == 302
        with app.app_context():
            item = db.session.scalar(db.select(EquipmentItem).where(EquipmentItem.name == "AED Unit 1"))
            assert item is not None
            assert item.serial_number == "SN001"

    def test_member_cannot_create_item(self, app, member_client):
        type_id = _make_type(app)
        response = member_client.post(
            "/equipment/items/create",
            data={"name": "Test", "type_id": str(type_id)},
        )
        assert response.status_code == 403


class TestEquipmentItemIssue:
    def test_admin_can_issue_item(self, app, admin_client):
        type_id = _make_type(app)
        item_id = _make_item(app, type_id)
        # get user id

        with app.app_context():
            user = db.session.scalar(db.select(UserAccount).where(UserAccount.email == "admin@test.com"))
            user_id = str(user.id)
        response = admin_client.post(
            f"/equipment/items/{item_id}/issue",
            data={"user_id": user_id},
            follow_redirects=False,
        )
        assert response.status_code == 302
        with app.app_context():
            item = db.session.get(EquipmentItem, item_id)
            assert item.issued_to_id is not None

    def test_admin_can_return_item(self, app, admin_client):
        type_id = _make_type(app)
        item_id = _make_item(app, type_id)

        with app.app_context():
            user = db.session.scalar(db.select(UserAccount).where(UserAccount.email == "admin@test.com"))
            item = db.session.get(EquipmentItem, item_id)
            item.issued_to_id = user.id
            item.issued_at = datetime.now(timezone.utc)
            db.session.commit()
        response = admin_client.post(
            f"/equipment/items/{item_id}/return",
            follow_redirects=False,
        )
        assert response.status_code == 302
        with app.app_context():
            item = db.session.get(EquipmentItem, item_id)
            assert item.issued_to_id is None


class TestEventEquipmentPlan:
    def test_admin_can_add_plan_entry(self, app, admin_client):
        type_id = _make_type(app)
        _make_item(app, type_id, "Plan Item 1")
        _make_item(app, type_id, "Plan Item 2")
        event_id = _make_event_in_status(app)
        response = admin_client.post(
            f"/events/{event_id}/equipment/plan",
            data={"type_id": str(type_id), "quantity": "2"},
            follow_redirects=False,
        )
        assert response.status_code == 302

        with app.app_context():
            plan = db.session.get(EventEquipmentPlan, (event_id, type_id))
            assert plan is not None
            assert plan.quantity_required == 2

    def test_member_cannot_add_plan_entry(self, app, member_client):
        type_id = _make_type(app)
        event_id = _make_event_in_status(app)
        response = member_client.post(
            f"/events/{event_id}/equipment/plan",
            data={"type_id": str(type_id), "quantity": "1"},
        )
        assert response.status_code == 403


# ── Type create: validation edge cases ───────────────────────────────────────


class TestEquipmentTypeCreateExtended:
    def test_duplicate_name_flashes(self, app, admin_client):
        _make_type(app, "Duplicate Type")
        response = admin_client.post(
            "/equipment/types/create",
            data={"name": "Duplicate Type"},
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "existuje" in response.data.decode()


# ── Type edit: extended ───────────────────────────────────────────────────────


class TestEquipmentTypeEditExtended:
    def test_get_returns_200(self, app, admin_client):
        type_id = _make_type(app)
        response = admin_client.get(f"/equipment/types/{type_id}/edit")
        assert response.status_code == 200

    def test_404_for_missing_type(self, admin_client):
        response = admin_client.get("/equipment/types/999999/edit")
        assert response.status_code == 404

    def test_edit_duplicate_name_flashes(self, app, admin_client):
        type_id = _make_type(app, "Type A")
        _make_type(app, "Type B")
        with app.app_context():
            et = db.session.get(EquipmentType, type_id)
            version = et.version
        response = admin_client.post(
            f"/equipment/types/{type_id}/edit",
            data={"name": "Type B", "version": str(version)},
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "existuje" in response.data.decode()

    def test_edit_empty_name_flashes(self, app, admin_client):
        type_id = _make_type(app)
        with app.app_context():
            version = db.session.get(EquipmentType, type_id).version
        response = admin_client.post(
            f"/equipment/types/{type_id}/edit",
            data={"name": "", "version": str(version)},
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "povinný" in response.data.decode()

    def test_edit_member_forbidden(self, app, member_client):
        type_id = _make_type(app)
        response = member_client.post(f"/equipment/types/{type_id}/edit", data={})
        assert response.status_code == 403

    def test_null_description_renders_empty_not_none(self, app, admin_client):
        """Regression for #84: NULL description must render as '' not 'None'."""
        type_id = _make_type(app)  # description is NULL by default
        response = admin_client.get(f"/equipment/types/{type_id}/edit")
        body = response.data.decode()
        assert "None" not in body


# ── Type delete: extended ─────────────────────────────────────────────────────


class TestEquipmentTypeDeleteExtended:
    def test_delete_404_for_missing(self, admin_client):
        response = admin_client.post("/equipment/types/999999/delete")
        assert response.status_code == 404

    def test_delete_member_forbidden(self, app, member_client):
        type_id = _make_type(app)
        response = member_client.post(f"/equipment/types/{type_id}/delete")
        assert response.status_code == 403


# ── Items list: filters ───────────────────────────────────────────────────────


class TestEquipmentItemsList:
    def test_list_returns_200(self, admin_client):
        response = admin_client.get("/equipment/items/")
        assert response.status_code == 200

    def test_filter_by_type_returns_200(self, app, admin_client):
        type_id = _make_type(app)
        response = admin_client.get(f"/equipment/items/?type_id={type_id}")
        assert response.status_code == 200

    def test_filter_issued_yes(self, admin_client):
        response = admin_client.get("/equipment/items/?issued=yes")
        assert response.status_code == 200

    def test_filter_issued_no(self, admin_client):
        response = admin_client.get("/equipment/items/?issued=no")
        assert response.status_code == 200

    def test_list_member_forbidden(self, member_client):
        """Member has equipment.view, so 200 is correct — test that access works."""
        response = member_client.get("/equipment/items/")
        assert response.status_code == 200


# ── Item create: extended validation ─────────────────────────────────────────


class TestEquipmentItemCreateExtended:
    def test_get_returns_200(self, admin_client):
        response = admin_client.get("/equipment/items/create")
        assert response.status_code == 200

    def test_empty_name_flashes(self, app, admin_client):
        type_id = _make_type(app)
        response = admin_client.post(
            "/equipment/items/create",
            data={"name": "", "type_id": str(type_id)},
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "povinný" in response.data.decode()

    def test_missing_type_flashes(self, admin_client):
        response = admin_client.post(
            "/equipment/items/create",
            data={"name": "Item", "type_id": ""},
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "povinný" in response.data.decode() or "Typ" in response.data.decode()

    def test_invalid_type_flashes(self, admin_client):
        response = admin_client.post(
            "/equipment/items/create",
            data={"name": "Item", "type_id": "999999"},
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "Neplatný" in response.data.decode() or "typ" in response.data.decode()


# ── Item edit ─────────────────────────────────────────────────────────────────


class TestEquipmentItemEdit:
    def test_get_returns_200(self, app, admin_client):
        type_id = _make_type(app)
        item_id = _make_item(app, type_id)
        response = admin_client.get(f"/equipment/items/{item_id}/edit")
        assert response.status_code == 200

    def test_404_for_missing_item(self, admin_client):
        response = admin_client.get("/equipment/items/999999/edit")
        assert response.status_code == 404

    def test_stale_version_flashes(self, app, admin_client):
        type_id = _make_type(app)
        item_id = _make_item(app, type_id)
        response = admin_client.post(
            f"/equipment/items/{item_id}/edit",
            data={"name": "New", "type_id": str(type_id), "version": "999"},
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "mezitím" in response.data.decode()

    def test_empty_name_flashes(self, app, admin_client):
        type_id = _make_type(app)
        item_id = _make_item(app, type_id)
        with app.app_context():
            version = db.session.get(EquipmentItem, item_id).version
        response = admin_client.post(
            f"/equipment/items/{item_id}/edit",
            data={"name": "", "type_id": str(type_id), "version": str(version)},
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "povinný" in response.data.decode()

    def test_successful_edit_redirects(self, app, admin_client):
        type_id = _make_type(app)
        item_id = _make_item(app, type_id)
        with app.app_context():
            version = db.session.get(EquipmentItem, item_id).version
        response = admin_client.post(
            f"/equipment/items/{item_id}/edit",
            data={"name": "Renamed Item", "type_id": str(type_id), "version": str(version)},
            follow_redirects=False,
        )
        assert response.status_code == 302
        with app.app_context():
            assert db.session.get(EquipmentItem, item_id).name == "Renamed Item"

    def test_member_forbidden(self, app, member_client):
        type_id = _make_type(app)
        item_id = _make_item(app, type_id)
        response = member_client.post(f"/equipment/items/{item_id}/edit", data={})
        assert response.status_code == 403

    def test_null_optional_fields_render_empty_not_none(self, app, admin_client):
        """Regression for #84: NULL optional fields must render as '' not 'None'."""
        type_id = _make_type(app)
        item_id = _make_item(app, type_id)  # serial_number/home_location/notes all NULL
        response = admin_client.get(f"/equipment/items/{item_id}/edit")
        body = response.data.decode()
        assert "None" not in body


# ── Item delete: extended ─────────────────────────────────────────────────────


class TestEquipmentItemDeleteExtended:
    def test_delete_success(self, app, admin_client):
        type_id = _make_type(app)
        item_id = _make_item(app, type_id)
        response = admin_client.post(f"/equipment/items/{item_id}/delete", follow_redirects=False)
        assert response.status_code == 302
        with app.app_context():
            assert db.session.get(EquipmentItem, item_id) is None

    def test_delete_404_for_missing(self, admin_client):
        response = admin_client.post("/equipment/items/999999/delete")
        assert response.status_code == 404

    def test_delete_issued_item_flashes(self, app, admin_client):

        type_id = _make_type(app)
        item_id = _make_item(app, type_id)
        with app.app_context():
            admin = db.session.scalar(db.select(UserAccount).where(UserAccount.email == "admin@test.com"))
            item = db.session.get(EquipmentItem, item_id)
            item.issued_to_id = admin.id
            item.issued_at = datetime.now(timezone.utc)
            db.session.commit()
        response = admin_client.post(f"/equipment/items/{item_id}/delete", follow_redirects=True)
        assert response.status_code == 200
        assert "vydána" in response.data.decode() or "nelze" in response.data.decode()

    def test_delete_member_forbidden(self, app, member_client):
        type_id = _make_type(app)
        item_id = _make_item(app, type_id)
        response = member_client.post(f"/equipment/items/{item_id}/delete")
        assert response.status_code == 403

    def test_delete_blocked_when_future_event_would_be_short(self, app, admin_client):
        """Deleting the only item of a type must be blocked if a future event plans that type."""

        type_id = _make_type(app, "Del Guard Type")
        item_id = _make_item(app, type_id, "Del Guard Item")
        event_id = _make_event_in_status(app, EventStatus.PUBLISHED)
        future_start = datetime.now(timezone.utc) + timedelta(days=3)
        future_end = future_start + timedelta(hours=8)
        with app.app_context():
            db.session.add(
                EventEquipmentPlan(
                    event_id=event_id,
                    equipment_type_id=type_id,
                    quantity_required=1,
                )
            )
            event = db.session.get(Event, event_id)
            event.start_datetime = future_start
            event.end_datetime = future_end
            db.session.commit()

        resp = admin_client.post(f"/equipment/items/{item_id}/delete", follow_redirects=True)
        assert resp.status_code == 200
        assert "Nelze smazat" in resp.data.decode()
        with app.app_context():
            assert db.session.get(EquipmentItem, item_id) is not None


# ── Item issue/return: extended ───────────────────────────────────────────────


class TestEquipmentItemIssueExtended:
    def test_already_issued_flashes(self, app, admin_client):

        type_id = _make_type(app)
        item_id = _make_item(app, type_id)
        with app.app_context():
            admin = db.session.scalar(db.select(UserAccount).where(UserAccount.email == "admin@test.com"))
            item = db.session.get(EquipmentItem, item_id)
            item.issued_to_id = admin.id
            item.issued_at = datetime.now(timezone.utc)
            db.session.commit()
            user_id = str(admin.id)
        response = admin_client.post(
            f"/equipment/items/{item_id}/issue",
            data={"user_id": user_id},
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "vydána" in response.data.decode() or "již" in response.data.decode()

    def test_no_user_id_flashes(self, app, admin_client):
        type_id = _make_type(app)
        item_id = _make_item(app, type_id)
        response = admin_client.post(f"/equipment/items/{item_id}/issue", data={}, follow_redirects=True)
        assert response.status_code == 200
        assert "povinný" in response.data.decode() or "uživatel" in response.data.decode().lower()

    def test_user_not_found_flashes(self, app, admin_client):
        type_id = _make_type(app)
        item_id = _make_item(app, type_id)
        response = admin_client.post(
            f"/equipment/items/{item_id}/issue",
            data={"user_id": "00000000-0000-0000-0000-000000000000"},
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "nalezen" in response.data.decode() or "uživatel" in response.data.decode().lower()


class TestEquipmentItemReturnExtended:
    def test_not_issued_flashes(self, app, admin_client):
        type_id = _make_type(app)
        item_id = _make_item(app, type_id)
        response = admin_client.post(f"/equipment/items/{item_id}/return", follow_redirects=True)
        assert response.status_code == 200
        assert "vydána" in response.data.decode() or "není" in response.data.decode()


# ── Availability ──────────────────────────────────────────────────────────────


class TestEquipmentItemAvailabilityModel:
    def test_is_available_default(self, app):
        type_id = _make_type(app)
        item_id = _make_item(app, type_id)
        with app.app_context():
            item = db.session.get(EquipmentItem, item_id)
            assert item.is_available is True
            assert item.unavailability_since is None

    def test_is_available_false_when_unavailable(self, app):
        type_id = _make_type(app)
        item_id = _make_item(app, type_id)
        with app.app_context():
            item = db.session.get(EquipmentItem, item_id)
            item.unavailability_since = datetime.now(timezone.utc) - timedelta(minutes=1)
            item.unavailability_reason = "Čeká na opravu"
            db.session.commit()
        with app.app_context():
            item = db.session.get(EquipmentItem, item_id)
            assert item.is_available is False


class TestEquipmentItemAvailabilityEdit:
    def test_set_unavailable_via_edit(self, app, admin_client):
        type_id = _make_type(app)
        item_id = _make_item(app, type_id, name="AED Test")
        with app.app_context():
            item = db.session.get(EquipmentItem, item_id)
            version = item.version

        response = admin_client.post(
            f"/equipment/items/{item_id}/edit",
            data={
                "name": "AED Test",
                "type_id": type_id,
                "version": version,
                "unavailability_reason": "Baterie potřebuje výměnu",
                "unavailability_since": "2030-01-01T10:00",
            },
            follow_redirects=True,
        )
        assert response.status_code == 200
        with app.app_context():
            item = db.session.get(EquipmentItem, item_id)
            assert item.unavailability_since is not None
            assert item.unavailability_reason == "Baterie potřebuje výměnu"

    def test_set_available_clears_reason(self, app, admin_client):
        type_id = _make_type(app)
        item_id = _make_item(app, type_id, name="AED Clr")
        with app.app_context():
            item = db.session.get(EquipmentItem, item_id)
            item.unavailability_since = datetime.now(timezone.utc) - timedelta(minutes=1)
            item.unavailability_reason = "Stará závada"
            db.session.commit()
            version = item.version

        admin_client.post(
            f"/equipment/items/{item_id}/edit",
            data={
                "name": "AED Clr",
                "type_id": type_id,
                "version": version,
            },
            follow_redirects=True,
        )
        with app.app_context():
            item = db.session.get(EquipmentItem, item_id)
            assert item.is_available is True
            assert item.unavailability_reason is None
            assert item.unavailability_since is None


class TestFutureMaintenanceCancellation:
    """Cancelling a future (scheduled) maintenance window via mark-available route."""

    def test_mark_available_clears_future_window(self, app, admin_client):
        """item_mark_available must clear a future window even though is_available=True."""
        type_id = _make_type(app, "Future Cancel Type")
        item_id = _make_item(app, type_id, "Future Cancel Item")
        future = datetime.now(timezone.utc) + timedelta(days=7)
        with app.app_context():
            item = db.session.get(EquipmentItem, item_id)
            item.unavailability_since = future
            item.unavailability_reason = "Plánovaný servis"
            db.session.commit()
            version = item.version
            # Confirm item is still available (future window)
            assert item.is_available is True

        resp = admin_client.post(
            f"/equipment/items/{item_id}/mark-available",
            data={"version": str(version)},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        body = resp.data.decode()
        assert "zrušen" in body
        with app.app_context():
            item = db.session.get(EquipmentItem, item_id)
            assert item.unavailability_since is None
            assert item.unavailability_reason is None
            assert item.is_available is True

    def test_mark_available_without_window_flashes_warning(self, app, admin_client):
        """Posting to mark-available when no maintenance window exists flashes a warning."""
        type_id = _make_type(app, "No Window Type")
        item_id = _make_item(app, type_id, "No Window Item")
        with app.app_context():
            version = db.session.get(EquipmentItem, item_id).version

        resp = admin_client.post(
            f"/equipment/items/{item_id}/mark-available",
            data={"version": str(version)},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert "žádný" in resp.data.decode()
        with app.app_context():
            # Item unchanged
            item = db.session.get(EquipmentItem, item_id)
            assert item.unavailability_since is None


# ── Type-level availability check ─────────────────────────────────────────────


class TestAvailableQuantityForType:
    """Unit tests for the available_quantity_for_type helper."""

    def _make_event_with_plan(self, app, type_id, qty, start, end, status=EventStatus.PUBLISHED):
        with app.app_context():
            me = MasterEvent(name=f"AQ ME {start}")
            db.session.add(me)
            db.session.flush()
            event = Event(
                name=f"AQ Event {start}",
                master_event_id=me.id,
                status=status,
                start_datetime=start,
                end_datetime=end,
            )
            db.session.add(event)
            db.session.flush()
            db.session.add(EventEquipmentPlan(event_id=event.id, equipment_type_id=type_id, quantity_required=qty))
            db.session.commit()
            return event.id

    def test_single_available_item_no_overlap(self, app):
        type_id = _make_type(app, "AQ Type A")
        _make_item(app, type_id, "AQ Item 1")
        start = datetime(2034, 1, 1, 10, tzinfo=timezone.utc)
        end = datetime(2034, 1, 1, 18, tzinfo=timezone.utc)
        with app.app_context():
            result = available_quantity_for_type(type_id, start, end)
        assert result == 1

    def test_overlapping_event_reduces_availability(self, app):
        type_id = _make_type(app, "AQ Type B")
        _make_item(app, type_id, "AQ Item B1")
        _make_item(app, type_id, "AQ Item B2")
        s = datetime(2034, 2, 1, 10, tzinfo=timezone.utc)
        e = datetime(2034, 2, 1, 18, tzinfo=timezone.utc)
        self._make_event_with_plan(app, type_id, 1, s, e)
        with app.app_context():
            result = available_quantity_for_type(type_id, s, e)
        assert result == 1  # 2 items - 1 committed

    def test_cancelled_event_not_counted(self, app):
        type_id = _make_type(app, "AQ Type C")
        _make_item(app, type_id, "AQ Item C")
        s = datetime(2034, 3, 1, 10, tzinfo=timezone.utc)
        e = datetime(2034, 3, 1, 18, tzinfo=timezone.utc)
        self._make_event_with_plan(app, type_id, 1, s, e, status=EventStatus.CANCELLED)
        with app.app_context():
            result = available_quantity_for_type(type_id, s, e)
        assert result == 1  # cancelled event doesn't consume the item

    def test_completed_event_not_counted(self, app):
        type_id = _make_type(app, "AQ Type D")
        _make_item(app, type_id, "AQ Item D")
        s = datetime(2034, 4, 1, 10, tzinfo=timezone.utc)
        e = datetime(2034, 4, 1, 18, tzinfo=timezone.utc)
        self._make_event_with_plan(app, type_id, 1, s, e, status=EventStatus.COMPLETED)
        with app.app_context():
            result = available_quantity_for_type(type_id, s, e)
        assert result == 1

    def test_unavailable_item_excluded_from_pool(self, app):
        type_id = _make_type(app, "AQ Type E")
        item_id = _make_item(app, type_id, "AQ Item E")
        with app.app_context():
            item = db.session.get(EquipmentItem, item_id)
            item.unavailability_since = datetime.now(timezone.utc) - timedelta(hours=1)
            db.session.commit()
        s = datetime(2034, 5, 1, 10, tzinfo=timezone.utc)
        e = datetime(2034, 5, 1, 18, tzinfo=timezone.utc)
        with app.app_context():
            result = available_quantity_for_type(type_id, s, e)
        assert result == 0

    def test_issued_item_included_in_pool(self, app, admin_client):
        type_id = _make_type(app, "AQ Type F")
        item_id = _make_item(app, type_id, "AQ Item F")
        with app.app_context():
            u = db.session.scalar(db.select(UserAccount).limit(1))
            item = db.session.get(EquipmentItem, item_id)
            item.issued_to_id = u.id
            db.session.commit()
        s = datetime(2034, 6, 1, 10, tzinfo=timezone.utc)
        e = datetime(2034, 6, 1, 18, tzinfo=timezone.utc)
        with app.app_context():
            result = available_quantity_for_type(type_id, s, e)
        assert result == 1  # issued items are still in the pool (person may bring it to the event)

    def test_exclude_event_id_frees_own_quantity(self, app):
        type_id = _make_type(app, "AQ Type G")
        _make_item(app, type_id, "AQ Item G")
        s = datetime(2034, 7, 1, 10, tzinfo=timezone.utc)
        e = datetime(2034, 7, 1, 18, tzinfo=timezone.utc)
        event_id = self._make_event_with_plan(app, type_id, 1, s, e)
        with app.app_context():
            without_exclude = available_quantity_for_type(type_id, s, e)
            with_exclude = available_quantity_for_type(type_id, s, e, exclude_event_id=event_id)
        assert without_exclude == 0
        assert with_exclude == 1


# ── Plan add rejects when stock is insufficient ───────────────────────────────


class TestEquipmentPlanAvailabilityEnforcement:
    """The plan-add route must reject quantities exceeding available stock."""

    def test_plan_add_rejected_when_no_stock(self, app, admin_client):
        type_id = _make_type(app, "Enf Type A")
        # No items of this type → available = 0
        event_id = _make_event_in_status(app, EventStatus.PUBLISHED)
        resp = admin_client.post(
            f"/events/{event_id}/equipment/plan",
            data={"type_id": str(type_id), "quantity": "1"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert "Nedostatek" in resp.data.decode() or "vybavení" in resp.data.decode()
        with app.app_context():
            plan = db.session.get(EventEquipmentPlan, (event_id, type_id))
            assert plan is None

    def test_plan_add_succeeds_when_stock_sufficient(self, app, admin_client):
        type_id = _make_type(app, "Enf Type B")
        _make_item(app, type_id, "Enf Item B")
        event_id = _make_event_in_status(app, EventStatus.PUBLISHED)
        resp = admin_client.post(
            f"/events/{event_id}/equipment/plan",
            data={"type_id": str(type_id), "quantity": "1"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        with app.app_context():
            plan = db.session.get(EventEquipmentPlan, (event_id, type_id))
            assert plan is not None
            assert plan.quantity_required == 1

    def test_plan_update_cannot_exceed_total_pool(self, app, admin_client):
        """Updating an existing plan row cannot request more items than exist."""
        type_id = _make_type(app, "Upd Type")
        _make_item(app, type_id, "Upd Item 1")  # only 1 item in pool
        event_id = _make_event_in_status(app, EventStatus.PUBLISHED)
        # First add: qty=1 succeeds (pool = 1, requesting 1)
        admin_client.post(
            f"/events/{event_id}/equipment/plan",
            data={"type_id": str(type_id), "quantity": "1"},
        )
        # Update attempt: qty=2 must be rejected (only 1 item exists)
        resp = admin_client.post(
            f"/events/{event_id}/equipment/plan",
            data={"type_id": str(type_id), "quantity": "2"},
            follow_redirects=True,
        )
        assert "Nedostatek" in resp.data.decode()
        with app.app_context():
            plan = db.session.get(EventEquipmentPlan, (event_id, type_id))
            assert plan.quantity_required == 1  # unchanged


# ── Unavailability warning for future events ──────────────────────────────────


class TestUnavailabilityFutureEventWarning:
    """Marking an item unavailable shows a warning if future events would be short."""

    def test_warning_shown_when_future_event_becomes_short(self, app, admin_client):

        type_id = _make_type(app, "Warn Type A")
        item_id = _make_item(app, type_id, "Warn Item A")
        # One item available; create a future event that needs 1
        future_start = datetime.now(timezone.utc) + timedelta(days=5)
        future_end = future_start + timedelta(hours=8)
        event_id = _make_event_in_status(app)
        with app.app_context():
            db.session.add(
                EventEquipmentPlan(
                    event_id=event_id,
                    equipment_type_id=type_id,
                    quantity_required=1,
                )
            )
            event = db.session.get(Event, event_id)
            event.start_datetime = future_start
            event.end_datetime = future_end
            db.session.commit()

        resp = admin_client.post(
            f"/equipment/items/{item_id}/edit",
            data={
                "name": "Warn Item A",
                "type_id": str(type_id),
                "version": "1",
                "unavailability_reason": "Oprava",
                "unavailability_since": "2026-01-01",
            },
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert "archivováno" not in resp.data.decode()
        assert "Upozornění" in resp.data.decode() or "nedostatek" in resp.data.decode()

    def test_no_warning_when_enough_stock_remains(self, app, admin_client):

        type_id = _make_type(app, "Warn Type B")
        item1_id = _make_item(app, type_id, "Warn Item B1")
        _make_item(app, type_id, "Warn Item B2")  # second item keeps pool at 1
        future_start = datetime.now(timezone.utc) + timedelta(days=5)
        future_end = future_start + timedelta(hours=8)
        event_id = _make_event_in_status(app)
        with app.app_context():
            db.session.add(
                EventEquipmentPlan(
                    event_id=event_id,
                    equipment_type_id=type_id,
                    quantity_required=1,
                )
            )
            event = db.session.get(Event, event_id)
            event.start_datetime = future_start
            event.end_datetime = future_end
            db.session.commit()

        resp = admin_client.post(
            f"/equipment/items/{item1_id}/edit",
            data={
                "name": "Warn Item B1",
                "type_id": str(type_id),
                "version": "1",
                "unavailability_reason": "Oprava",
                "unavailability_since": "2026-01-01",
            },
            follow_redirects=True,
        )
        # 2 items → marking 1 unavailable still leaves 1 available → no shortage
        assert resp.status_code == 200
        body = resp.data.decode()
        assert "Upozornění" not in body
        assert "nedostatek" not in body.lower()


# ── Plan-add conflict message includes conflicting event ──────────────────────


class TestPlanAddConflictMessage:
    """equipment_plan_add flash must link to the conflicting event."""

    def test_conflict_flash_contains_conflicting_event_name(self, app, admin_client):
        type_id = _make_type(app, "Conflict Msg Type")
        _make_item(app, type_id, "Conflict Msg Item")

        future_start = datetime.now(timezone.utc) + timedelta(days=3)
        future_end = future_start + timedelta(hours=8)

        # Event A occupies the only item in its window
        event_a_id = _make_event_in_status(app, EventStatus.PUBLISHED, name="Conflict Event A")
        with app.app_context():
            ev_a = db.session.get(Event, event_a_id)
            ev_a.name = "Blocking Event"
            ev_a.start_datetime = future_start
            ev_a.end_datetime = future_end
            db.session.add(EventEquipmentPlan(event_id=event_a_id, equipment_type_id=type_id, quantity_required=1))
            db.session.commit()

        # Event B overlaps — trying to add the same type should fail and name Event A
        event_b_id = _make_event_in_status(app, EventStatus.PUBLISHED, name="Conflict Event B")
        with app.app_context():
            ev_b = db.session.get(Event, event_b_id)
            ev_b.start_datetime = future_start
            ev_b.end_datetime = future_end
            db.session.commit()

        resp = admin_client.post(
            f"/events/{event_b_id}/equipment/plan",
            data={"type_id": str(type_id), "quantity": "1"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        body = resp.data.decode()
        assert "Nedostatek" in body
        assert "Blocking Event" in body
