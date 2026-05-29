"""Tests for equipment inventory CRUD and permissions."""
import json
from datetime import datetime
from datetime import timezone

from app.extensions import db
from app.models.equipment import EquipmentCategory
from app.models.equipment import EquipmentItem
from app.models.equipment import EquipmentItemStatus
from app.models.equipment import EquipmentType
from app.models.equipment import EventEquipmentAssignment
from app.models.event import Event
from app.models.event import EventStatus
from app.models.master_event import MasterEvent


def _make_type(app, name: str = "Test Type", category: EquipmentCategory = EquipmentCategory.SHARED) -> int:
    with app.app_context():
        et = EquipmentType(name=name, category=category)
        db.session.add(et)
        db.session.commit()
        return et.id


def _make_item(app, type_id: int, name: str = "Test Item") -> int:
    with app.app_context():
        item = EquipmentItem(name=name, type_id=type_id)
        db.session.add(item)
        db.session.commit()
        return item.id


def _make_event(app) -> int:
    with app.app_context():
        me = MasterEvent(name="Test ME")
        db.session.add(me)
        db.session.flush()
        event = Event(
            name="Test Event",
            master_event_id=me.id,
            status=EventStatus.DRAFT,
            start_datetime=__import__('datetime').datetime(2030, 6, 1, 10, 0, tzinfo=__import__('datetime').timezone.utc),
            end_datetime=__import__('datetime').datetime(2030, 6, 1, 18, 0, tzinfo=__import__('datetime').timezone.utc),
        )
        db.session.add(event)
        db.session.commit()
        return event.id


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
            data={"name": "Defibrilátor", "category": "shared", "description": ""},
            follow_redirects=False,
        )
        assert response.status_code == 302
        with app.app_context():
            et = db.session.scalar(db.select(EquipmentType).where(EquipmentType.name == "Defibrilátor"))
            assert et is not None
            assert et.category == EquipmentCategory.SHARED

    def test_create_type_missing_name(self, admin_client):
        response = admin_client.post(
            "/equipment/types/create",
            data={"name": "", "category": "shared"},
            follow_redirects=True,
        )
        assert response.status_code == 200

    def test_member_cannot_create_type(self, member_client):
        response = member_client.post(
            "/equipment/types/create",
            data={"name": "Test", "category": "shared"},
        )
        assert response.status_code == 403


class TestEquipmentTypeEdit:
    def test_admin_can_edit_type(self, app, admin_client):
        type_id = _make_type(app, "Old Name")
        response = admin_client.post(
            f"/equipment/types/{type_id}/edit",
            data={"name": "New Name", "category": "personal", "version": "1"},
            follow_redirects=False,
        )
        assert response.status_code == 302
        with app.app_context():
            et = db.session.get(EquipmentType, type_id)
            assert et.name == "New Name"
            assert et.category == EquipmentCategory.PERSONAL

    def test_optimistic_lock_conflict(self, app, admin_client):
        type_id = _make_type(app)
        response = admin_client.post(
            f"/equipment/types/{type_id}/edit",
            data={"name": "New Name", "category": "shared", "version": "99"},
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
        type_id = _make_type(app, category=EquipmentCategory.PERSONAL)
        item_id = _make_item(app, type_id)
        # get user id
        from app.models.user import UserAccount
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
        type_id = _make_type(app, category=EquipmentCategory.PERSONAL)
        item_id = _make_item(app, type_id)
        from app.models.user import UserAccount
        from datetime import datetime, timezone
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
        event_id = _make_event(app)
        response = admin_client.post(
            f"/events/{event_id}/equipment/plan",
            data={"type_id": str(type_id), "quantity": "2"},
            follow_redirects=False,
        )
        assert response.status_code == 302
        from app.models.equipment import EventEquipmentPlan
        with app.app_context():
            plan = db.session.get(EventEquipmentPlan, (event_id, type_id))
            assert plan is not None
            assert plan.quantity_required == 2

    def test_member_cannot_add_plan_entry(self, app, member_client):
        type_id = _make_type(app)
        event_id = _make_event(app)
        response = member_client.post(
            f"/events/{event_id}/equipment/plan",
            data={"type_id": str(type_id), "quantity": "1"},
        )
        assert response.status_code == 403


class TestEventEquipmentAssign:
    def test_admin_can_assign_item(self, app, admin_client):
        type_id = _make_type(app)
        item_id = _make_item(app, type_id)
        event_id = _make_event(app)
        response = admin_client.post(
            f"/events/{event_id}/equipment/assign",
            data={"item_id": str(item_id)},
            follow_redirects=False,
        )
        assert response.status_code == 302
        from app.models.equipment import EventEquipmentAssignment
        with app.app_context():
            ea = db.session.scalar(
                db.select(EventEquipmentAssignment).where(
                    EventEquipmentAssignment.event_id == event_id,
                    EventEquipmentAssignment.equipment_item_id == item_id,
                )
            )
            assert ea is not None

    def test_member_cannot_assign_item(self, app, member_client):
        type_id = _make_type(app)
        item_id = _make_item(app, type_id)
        event_id = _make_event(app)
        response = member_client.post(
            f"/events/{event_id}/equipment/assign",
            data={"item_id": str(item_id)},
        )
        assert response.status_code == 403


# ── Type create: validation edge cases ───────────────────────────────────────

class TestEquipmentTypeCreateExtended:
    def test_invalid_category_flashes(self, admin_client):
        response = admin_client.post(
            "/equipment/types/create",
            data={"name": "Valid Name", "category": "not_a_category"},
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "kategorie" in response.data.decode()

    def test_duplicate_name_flashes(self, app, admin_client):
        _make_type(app, "Duplicate Type")
        response = admin_client.post(
            "/equipment/types/create",
            data={"name": "Duplicate Type", "category": "shared"},
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

    def test_edit_invalid_category_flashes(self, app, admin_client):
        type_id = _make_type(app)
        with app.app_context():
            et = db.session.get(EquipmentType, type_id)
            version = et.version
        response = admin_client.post(
            f"/equipment/types/{type_id}/edit",
            data={"name": "Valid", "category": "bad_cat", "version": str(version)},
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "kategorie" in response.data.decode()

    def test_edit_duplicate_name_flashes(self, app, admin_client):
        type_id = _make_type(app, "Type A")
        _make_type(app, "Type B")
        with app.app_context():
            et = db.session.get(EquipmentType, type_id)
            version = et.version
        response = admin_client.post(
            f"/equipment/types/{type_id}/edit",
            data={"name": "Type B", "category": "shared", "version": str(version)},
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
            data={"name": "", "category": "shared", "version": str(version)},
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
        from app.models.user import UserAccount
        from datetime import datetime, timezone
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


# ── Item issue/return: extended ───────────────────────────────────────────────

class TestEquipmentItemIssueExtended:
    def test_already_issued_flashes(self, app, admin_client):
        from app.models.user import UserAccount
        from datetime import datetime, timezone
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
        response = admin_client.post(
            f"/equipment/items/{item_id}/issue", data={}, follow_redirects=True
        )
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
        response = admin_client.post(
            f"/equipment/items/{item_id}/return", follow_redirects=True
        )
        assert response.status_code == 200
        assert "vydána" in response.data.decode() or "není" in response.data.decode()


# ── Availability ──────────────────────────────────────────────────────────────

def _make_event_with_times(app, start: datetime, end: datetime, name: str = "Test Event") -> int:
    """Create a published event with the given time window."""
    with app.app_context():
        me = MasterEvent(name="Test ME avail")
        db.session.add(me)
        db.session.flush()
        event = Event(
            name=name,
            master_event_id=me.id,
            status=EventStatus.PUBLISHED,
            start_datetime=start,
            end_datetime=end,
        )
        db.session.add(event)
        db.session.commit()
        return event.id


def _assign_item_to_event(app, event_id: int, item_id: int) -> None:
    with app.app_context():
        assn = EventEquipmentAssignment(event_id=event_id, equipment_item_id=item_id)
        db.session.add(assn)
        db.session.commit()


class TestEquipmentItemAvailabilityModel:
    def test_is_available_default(self, app):
        type_id = _make_type(app)
        item_id = _make_item(app, type_id)
        with app.app_context():
            item = db.session.get(EquipmentItem, item_id)
            assert item.is_available is True
            assert item.status == EquipmentItemStatus.AVAILABLE

    def test_is_available_false_when_unavailable(self, app):
        type_id = _make_type(app)
        item_id = _make_item(app, type_id)
        with app.app_context():
            item = db.session.get(EquipmentItem, item_id)
            item.status = EquipmentItemStatus.UNAVAILABLE
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
                "status": "UNAVAILABLE",
                "unavailability_reason": "Baterie potřebuje výměnu",
                "unavailability_since": "2030-01-01T10:00",
            },
            follow_redirects=True,
        )
        assert response.status_code == 200
        with app.app_context():
            item = db.session.get(EquipmentItem, item_id)
            assert item.status == EquipmentItemStatus.UNAVAILABLE
            assert item.unavailability_reason == "Baterie potřebuje výměnu"

    def test_set_available_clears_reason(self, app, admin_client):
        type_id = _make_type(app)
        item_id = _make_item(app, type_id, name="AED Clr")
        with app.app_context():
            item = db.session.get(EquipmentItem, item_id)
            item.status = EquipmentItemStatus.UNAVAILABLE
            item.unavailability_reason = "Stará závada"
            db.session.commit()
            version = item.version

        admin_client.post(
            f"/equipment/items/{item_id}/edit",
            data={
                "name": "AED Clr",
                "type_id": type_id,
                "version": version,
                "status": "AVAILABLE",
            },
            follow_redirects=True,
        )
        with app.app_context():
            item = db.session.get(EquipmentItem, item_id)
            assert item.status == EquipmentItemStatus.AVAILABLE
            assert item.unavailability_reason is None


class TestEquipmentCheckEndpoint:
    def _post_check(self, client, payload: dict):
        return client.post(
            "/events/equipment-check",
            data=json.dumps(payload),
            content_type="application/json",
            headers={"X-CSRFToken": "ignored"},
        )

    def test_available_item_returns_ok(self, app, admin_client):
        type_id = _make_type(app, name="Typ check ok")
        item_id = _make_item(app, type_id, name="Item OK")
        response = admin_client.post(
            "/events/equipment-check",
            data=json.dumps({
                "item_ids": [item_id],
                "start_datetime": "2030-07-01T10:00:00",
                "end_datetime": "2030-07-01T18:00:00",
            }),
            content_type="application/json",
        )
        assert response.status_code == 200
        data = response.get_json()
        assert data["results"][0]["status"] == "ok"

    def test_unavailable_item_returns_unavailable(self, app, admin_client):
        type_id = _make_type(app, name="Typ unavail")
        item_id = _make_item(app, type_id, name="Item Unavail")
        with app.app_context():
            item = db.session.get(EquipmentItem, item_id)
            item.status = EquipmentItemStatus.UNAVAILABLE
            item.unavailability_reason = "Oprava"
            db.session.commit()

        response = admin_client.post(
            "/events/equipment-check",
            data=json.dumps({
                "item_ids": [item_id],
                "start_datetime": "2030-07-01T10:00:00",
                "end_datetime": "2030-07-01T18:00:00",
            }),
            content_type="application/json",
        )
        data = response.get_json()
        assert data["results"][0]["status"] == "unavailable"
        assert "Oprava" in data["results"][0]["reason"]

    def test_conflict_detected(self, app, admin_client):
        type_id = _make_type(app, name="Typ conflict")
        item_id = _make_item(app, type_id, name="Item Conflict")
        # Existing event: 10:00–18:00 on 2030-08-01
        existing_event_id = _make_event_with_times(
            app,
            datetime(2030, 8, 1, 10, 0, tzinfo=timezone.utc),
            datetime(2030, 8, 1, 18, 0, tzinfo=timezone.utc),
            name="Existing Event",
        )
        _assign_item_to_event(app, existing_event_id, item_id)

        # Check for overlapping window 12:00–16:00 same day
        response = admin_client.post(
            "/events/equipment-check",
            data=json.dumps({
                "item_ids": [item_id],
                "start_datetime": "2030-08-01T12:00:00",
                "end_datetime": "2030-08-01T16:00:00",
            }),
            content_type="application/json",
        )
        data = response.get_json()
        assert data["results"][0]["status"] == "conflict"
        assert data["results"][0]["conflicting_event"]["name"] == "Existing Event"

    def test_conflict_excluded_for_own_event(self, app, admin_client):
        """When editing an event, its own assignment should not be a conflict."""
        type_id = _make_type(app, name="Typ self excl")
        item_id = _make_item(app, type_id, name="Item Self")
        event_id = _make_event_with_times(
            app,
            datetime(2030, 9, 1, 10, 0, tzinfo=timezone.utc),
            datetime(2030, 9, 1, 18, 0, tzinfo=timezone.utc),
        )
        _assign_item_to_event(app, event_id, item_id)

        response = admin_client.post(
            "/events/equipment-check",
            data=json.dumps({
                "item_ids": [item_id],
                "start_datetime": "2030-09-01T10:00:00",
                "end_datetime": "2030-09-01T18:00:00",
                "exclude_event_id": event_id,
            }),
            content_type="application/json",
        )
        data = response.get_json()
        assert data["results"][0]["status"] == "ok"

    def test_no_conflict_for_non_overlapping(self, app, admin_client):
        type_id = _make_type(app, name="Typ no ovlp")
        item_id = _make_item(app, type_id, name="Item NoOvlp")
        existing_event_id = _make_event_with_times(
            app,
            datetime(2030, 10, 1, 10, 0, tzinfo=timezone.utc),
            datetime(2030, 10, 1, 14, 0, tzinfo=timezone.utc),
        )
        _assign_item_to_event(app, existing_event_id, item_id)

        # New event starts after existing ends — no overlap
        response = admin_client.post(
            "/events/equipment-check",
            data=json.dumps({
                "item_ids": [item_id],
                "start_datetime": "2030-10-01T15:00:00",
                "end_datetime": "2030-10-01T20:00:00",
            }),
            content_type="application/json",
        )
        data = response.get_json()
        assert data["results"][0]["status"] == "ok"

    def test_assign_unavailable_item_blocked(self, app, admin_client):
        """Assigning an unavailable item to an event should be blocked."""
        type_id = _make_type(app, name="Typ block assign")
        item_id = _make_item(app, type_id, name="Blocked Item")
        event_id = _make_event_with_times(
            app,
            datetime(2030, 11, 1, 10, 0, tzinfo=timezone.utc),
            datetime(2030, 11, 1, 18, 0, tzinfo=timezone.utc),
        )
        with app.app_context():
            item = db.session.get(EquipmentItem, item_id)
            item.status = EquipmentItemStatus.UNAVAILABLE
            item.unavailability_reason = "Poškozeno"
            db.session.commit()

        response = admin_client.post(
            f"/events/{event_id}/equipment/assign",
            data={"item_id": item_id},
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "nedostupná" in response.data.decode().lower()
        # Item must NOT be assigned
        with app.app_context():
            assn = db.session.scalar(
                db.select(EventEquipmentAssignment).where(
                    EventEquipmentAssignment.event_id == event_id,
                    EventEquipmentAssignment.equipment_item_id == item_id,
                )
            )
            assert assn is None


class TestEventCreateWithEquipment:
    """Equipment pre-assignment on the /events/create page."""

    def test_create_form_shows_equipment_section_for_admin(self, app, admin_client):
        type_id = _make_type(app, name="Typ formulář create")
        _make_item(app, type_id, name="Item formulář create")
        response = admin_client.get("/events/create")
        assert response.status_code == 200
        assert b"equipment_item_ids" in response.data

    def test_create_event_with_equipment_assigns_items(self, app, admin_client):
        type_id = _make_type(app, name="Typ pre-assign")
        item_id = _make_item(app, type_id, name="Item pre-assign")
        with app.app_context():
            me = MasterEvent(name="ME pre-assign")
            db.session.add(me)
            db.session.commit()
            me_id = me.id

        response = admin_client.post(
            "/events/create",
            data={
                "name": "Akce s vybavením",
                "event_type": "MEDICAL_COVER",
                "master_event_id": str(me_id),
                "start_datetime": "2035-07-01T10:00",
                "end_datetime": "2035-07-01T18:00",
                "spot_count": "0",
                "action": "create",
                "equipment_item_ids": str(item_id),
            },
            follow_redirects=False,
        )
        assert response.status_code == 302
        with app.app_context():
            event = db.session.scalar(db.select(Event).where(Event.name == "Akce s vybavením"))
            assert event is not None
            assn = db.session.scalar(
                db.select(EventEquipmentAssignment).where(
                    EventEquipmentAssignment.event_id == event.id,
                    EventEquipmentAssignment.equipment_item_id == item_id,
                )
            )
            assert assn is not None

    def test_create_event_skips_unavailable_equipment(self, app, admin_client):
        type_id = _make_type(app, name="Typ unavail create")
        item_id = _make_item(app, type_id, name="Item unavail create")
        with app.app_context():
            item = db.session.get(EquipmentItem, item_id)
            item.status = EquipmentItemStatus.UNAVAILABLE
            item.unavailability_reason = "V opravě"
            me = MasterEvent(name="ME unavail create")
            db.session.add(me)
            db.session.commit()
            me_id = me.id

        response = admin_client.post(
            "/events/create",
            data={
                "name": "Akce se zakázaným vybavením",
                "event_type": "MEDICAL_COVER",
                "master_event_id": str(me_id),
                "start_datetime": "2035-08-01T10:00",
                "end_datetime": "2035-08-01T18:00",
                "spot_count": "0",
                "action": "create",
                "equipment_item_ids": str(item_id),
            },
            follow_redirects=False,
        )
        assert response.status_code == 302
        with app.app_context():
            event = db.session.scalar(db.select(Event).where(Event.name == "Akce se zakázaným vybavením"))
            assert event is not None
            assn = db.session.scalar(
                db.select(EventEquipmentAssignment).where(
                    EventEquipmentAssignment.event_id == event.id,
                    EventEquipmentAssignment.equipment_item_id == item_id,
                )
            )
            assert assn is None


class TestEquipmentConflictExclusion:
    """Cancelled/archived events should not cause equipment conflicts."""

    def test_cancelled_event_does_not_cause_conflict(self, app, admin_client):
        from app.routes.events._helpers import equipment_warnings_for_event

        type_id = _make_type(app, "Conflict Type")
        item_id = _make_item(app, type_id, "Conflict Item")

        with app.app_context():
            me = MasterEvent(name="Conflict ME")
            db.session.add(me)
            db.session.flush()

            # Cancelled event with the item assigned (overlapping time)
            cancelled = Event(
                name="Cancelled Event",
                master_event_id=me.id,
                status=EventStatus.CANCELLED,
                archived=True,
                start_datetime=datetime(2031, 1, 1, 10, 0, tzinfo=timezone.utc),
                end_datetime=datetime(2031, 1, 1, 18, 0, tzinfo=timezone.utc),
            )
            db.session.add(cancelled)
            db.session.flush()
            db.session.add(EventEquipmentAssignment(event_id=cancelled.id, equipment_item_id=item_id))

            # Active event with the same item (overlapping time)
            active = Event(
                name="Active Event",
                master_event_id=me.id,
                status=EventStatus.PUBLISHED,
                start_datetime=datetime(2031, 1, 1, 9, 0, tzinfo=timezone.utc),
                end_datetime=datetime(2031, 1, 1, 20, 0, tzinfo=timezone.utc),
            )
            db.session.add(active)
            db.session.flush()
            db.session.add(EventEquipmentAssignment(event_id=active.id, equipment_item_id=item_id))
            db.session.commit()

            # Warnings for the active event should NOT include the cancelled one
            warnings = equipment_warnings_for_event(active)
            conflict_names = [w["conflicting_event"]["name"] for w in warnings if w["status"] == "conflict"]
            assert "Cancelled Event" not in conflict_names

    def test_archived_event_does_not_cause_conflict(self, app, admin_client):
        from app.routes.events._helpers import equipment_warnings_for_event

        type_id = _make_type(app, "Archived Type")
        item_id = _make_item(app, type_id, "Archived Item")

        with app.app_context():
            me = MasterEvent(name="Archived ME")
            db.session.add(me)
            db.session.flush()

            # Archived (but completed) event with the item
            archived = Event(
                name="Archived Event",
                master_event_id=me.id,
                status=EventStatus.COMPLETED,
                archived=True,
                start_datetime=datetime(2032, 3, 1, 10, 0, tzinfo=timezone.utc),
                end_datetime=datetime(2032, 3, 1, 18, 0, tzinfo=timezone.utc),
            )
            db.session.add(archived)
            db.session.flush()
            db.session.add(EventEquipmentAssignment(event_id=archived.id, equipment_item_id=item_id))

            # New event at the same time
            new_event = Event(
                name="New Event",
                master_event_id=me.id,
                status=EventStatus.DRAFT,
                start_datetime=datetime(2032, 3, 1, 9, 0, tzinfo=timezone.utc),
                end_datetime=datetime(2032, 3, 1, 20, 0, tzinfo=timezone.utc),
            )
            db.session.add(new_event)
            db.session.flush()
            db.session.add(EventEquipmentAssignment(event_id=new_event.id, equipment_item_id=item_id))
            db.session.commit()

            warnings = equipment_warnings_for_event(new_event)
            conflict_names = [w["conflicting_event"]["name"] for w in warnings if w["status"] == "conflict"]
            assert "Archived Event" not in conflict_names


class TestEquipmentCheckEndpointExclusion:
    """AJAX equipment-check endpoint should ignore cancelled/archived events."""

    def test_check_excludes_cancelled_event(self, app, admin_client):
        type_id = _make_type(app, "Check Cancel Type")
        item_id = _make_item(app, type_id, "Check Cancel Item")

        with app.app_context():
            me = MasterEvent(name="Check Cancel ME")
            db.session.add(me)
            db.session.flush()

            # Cancelled event with the item
            cancelled = Event(
                name="Cancelled Overlap",
                master_event_id=me.id,
                status=EventStatus.CANCELLED,
                start_datetime=datetime(2033, 5, 1, 10, 0, tzinfo=timezone.utc),
                end_datetime=datetime(2033, 5, 1, 18, 0, tzinfo=timezone.utc),
            )
            db.session.add(cancelled)
            db.session.flush()
            db.session.add(EventEquipmentAssignment(event_id=cancelled.id, equipment_item_id=item_id))
            db.session.commit()

        # Check availability for the same time window — should report no conflict
        resp = admin_client.post("/events/equipment-check", json={
            "item_ids": [item_id],
            "start_datetime": "2033-05-01T09:00:00+00:00",
            "end_datetime": "2033-05-01T20:00:00+00:00",
        })
        assert resp.status_code == 200
        data = resp.get_json()
        statuses = [r["status"] for r in data["results"]]
        assert "conflict" not in statuses

    def test_check_excludes_archived_event(self, app, admin_client):
        type_id = _make_type(app, "Check Archive Type")
        item_id = _make_item(app, type_id, "Check Archive Item")

        with app.app_context():
            me = MasterEvent(name="Check Archive ME")
            db.session.add(me)
            db.session.flush()

            archived = Event(
                name="Archived Overlap",
                master_event_id=me.id,
                status=EventStatus.COMPLETED,
                archived=True,
                start_datetime=datetime(2033, 6, 1, 10, 0, tzinfo=timezone.utc),
                end_datetime=datetime(2033, 6, 1, 18, 0, tzinfo=timezone.utc),
            )
            db.session.add(archived)
            db.session.flush()
            db.session.add(EventEquipmentAssignment(event_id=archived.id, equipment_item_id=item_id))
            db.session.commit()

        resp = admin_client.post("/events/equipment-check", json={
            "item_ids": [item_id],
            "start_datetime": "2033-06-01T09:00:00+00:00",
            "end_datetime": "2033-06-01T20:00:00+00:00",
        })
        assert resp.status_code == 200
        data = resp.get_json()
        statuses = [r["status"] for r in data["results"]]
        assert "conflict" not in statuses

    def test_check_still_reports_active_conflict(self, app, admin_client):
        """Sanity check: active overlapping event DOES produce a conflict."""
        type_id = _make_type(app, "Check Active Type")
        item_id = _make_item(app, type_id, "Check Active Item")

        with app.app_context():
            me = MasterEvent(name="Check Active ME")
            db.session.add(me)
            db.session.flush()

            active = Event(
                name="Active Overlap",
                master_event_id=me.id,
                status=EventStatus.PUBLISHED,
                start_datetime=datetime(2033, 7, 1, 10, 0, tzinfo=timezone.utc),
                end_datetime=datetime(2033, 7, 1, 18, 0, tzinfo=timezone.utc),
            )
            db.session.add(active)
            db.session.flush()
            db.session.add(EventEquipmentAssignment(event_id=active.id, equipment_item_id=item_id))
            db.session.commit()

        resp = admin_client.post("/events/equipment-check", json={
            "item_ids": [item_id],
            "start_datetime": "2033-07-01T09:00:00+00:00",
            "end_datetime": "2033-07-01T20:00:00+00:00",
        })
        assert resp.status_code == 200
        data = resp.get_json()
        statuses = [r["status"] for r in data["results"]]
        assert "conflict" in statuses
