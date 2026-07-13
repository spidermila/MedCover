"""Tests for Master Event CRUD: list, create, detail, edit, archive."""

from datetime import datetime, timedelta, timezone

from app.extensions import db
from app.models.assignment import Assignment
from app.models.audit import AuditLogEntry
from app.models.event import Event, EventSpot, EventStatus
from app.models.master_event import MasterEvent
from app.models.role import Role
from app.models.user import UserAccount
from tests.conftest import _make_user


def _make_me(name: str = "Test ME", **kwargs) -> MasterEvent:
    """Create and persist a MasterEvent in the current context."""
    me = MasterEvent(name=name, **kwargs)
    db.session.add(me)
    db.session.commit()
    return me


# ── List ──────────────────────────────────────────────────────────────────────


class TestMasterEventList:
    def test_list_requires_login(self, client):
        response = client.get("/master-events/", follow_redirects=False)
        assert response.status_code == 302
        assert "login" in response.headers["Location"]

    def test_member_can_view_list(self, member_client):
        response = member_client.get("/master-events/")
        assert response.status_code == 200

    def test_admin_can_view_list(self, admin_client):
        response = admin_client.get("/master-events/")
        assert response.status_code == 200

    def test_list_shows_active_master_events(self, app, admin_client):
        with app.app_context():
            _make_me("Viditelná ME")
        response = admin_client.get("/master-events/")
        assert "Viditelná ME".encode() in response.data

    def test_list_hides_archived_by_default(self, app, admin_client):
        with app.app_context():
            _make_me("Archivovaná ME", archived=True)
        response = admin_client.get("/master-events/")
        assert "Archivovaná ME".encode() not in response.data

    def test_list_shows_archived_when_requested(self, app, admin_client):
        with app.app_context():
            _make_me("Archivovaná ME", archived=True)
        response = admin_client.get("/master-events/?archived=1")
        assert "Archivovaná ME".encode() in response.data


# ── Create ────────────────────────────────────────────────────────────────────


class TestMasterEventCreate:
    def test_create_page_loads_for_admin(self, admin_client):
        response = admin_client.get("/master-events/create")
        assert response.status_code == 200

    def test_create_page_forbidden_for_member(self, member_client):
        response = member_client.get("/master-events/create")
        assert response.status_code == 403

    def test_admin_can_create_master_event(self, app, admin_client):
        response = admin_client.post(
            "/master-events/create",
            data={"name": "Nová ME", "description": "Popis"},
            follow_redirects=False,
        )
        assert response.status_code == 302
        with app.app_context():
            me = db.session.scalar(db.select(MasterEvent).where(MasterEvent.name == "Nová ME"))
            assert me is not None
            assert me.description == "Popis"

    def test_create_missing_name_rejected(self, app, admin_client):
        response = admin_client.post(
            "/master-events/create",
            data={"name": ""},
            follow_redirects=True,
        )
        assert response.status_code == 200
        with app.app_context():
            # Only the general ME may exist (seeded); no new one
            count = db.session.scalar(
                db.select(db.func.count()).select_from(MasterEvent).where(MasterEvent.is_general == False)  # noqa: E712
            )
            assert count == 0

    def test_create_duplicate_name_rejected(self, app, admin_client):
        with app.app_context():
            _make_me("Duplicitní ME")
        response = admin_client.post(
            "/master-events/create",
            data={"name": "Duplicitní ME"},
            follow_redirects=True,
        )
        assert response.status_code == 200
        with app.app_context():
            count = db.session.scalar(
                db.select(db.func.count()).select_from(MasterEvent).where(MasterEvent.name == "Duplicitní ME")
            )
            assert count == 1  # No second one created

    def test_create_writes_audit_log(self, app, admin_client):
        admin_client.post(
            "/master-events/create",
            data={"name": "ME s auditom"},
            follow_redirects=True,
        )
        with app.app_context():
            entry = db.session.scalar(
                db.select(AuditLogEntry)
                .where(AuditLogEntry.entity_type == "MasterEvent")
                .where(AuditLogEntry.action_type == "create")
            )
            assert entry is not None
            assert "ME s auditom" in entry.summary


# ── Detail ────────────────────────────────────────────────────────────────────


class TestMasterEventDetail:
    def test_detail_requires_login(self, app, client):
        with app.app_context():
            me = _make_me("Detail ME")
            me_id = me.id
        response = client.get(f"/master-events/{me_id}", follow_redirects=False)
        assert response.status_code == 302

    def test_member_can_view_detail(self, app, member_client):
        with app.app_context():
            me = _make_me("Detail ME")
            me_id = me.id
        response = member_client.get(f"/master-events/{me_id}")
        assert response.status_code == 200

    def test_detail_shows_name(self, app, admin_client):
        with app.app_context():
            me = _make_me("Zobrazená ME")
            me_id = me.id
        response = admin_client.get(f"/master-events/{me_id}")
        assert "Zobrazená ME".encode() in response.data

    def test_detail_404_for_missing(self, admin_client):
        response = admin_client.get("/master-events/999999")
        assert response.status_code == 404


# ── Edit ──────────────────────────────────────────────────────────────────────


class TestMasterEventEdit:
    def test_edit_page_loads_for_admin(self, app, admin_client):
        with app.app_context():
            me = _make_me("Editovatelná ME")
            me_id = me.id
        response = admin_client.get(f"/master-events/{me_id}/edit")
        assert response.status_code == 200

    def test_edit_page_forbidden_for_member(self, app, member_client):
        with app.app_context():
            me = _make_me("Editovatelná ME")
            me_id = me.id
        response = member_client.get(f"/master-events/{me_id}/edit")
        assert response.status_code == 403

    def test_admin_can_edit_master_event(self, app, admin_client):
        with app.app_context():
            me = _make_me("Původní název")
            me_id = me.id
            version = me.version

        response = admin_client.post(
            f"/master-events/{me_id}/edit",
            data={"name": "Nový název", "version": str(version)},
            follow_redirects=False,
        )
        assert response.status_code == 302
        with app.app_context():
            updated = db.session.get(MasterEvent, me_id)
            assert updated.name == "Nový název"

    def test_edit_missing_name_rejected(self, app, admin_client):
        with app.app_context():
            me = _make_me("Původní název")
            me_id = me.id
            version = me.version

        admin_client.post(
            f"/master-events/{me_id}/edit",
            data={"name": "", "version": str(version)},
            follow_redirects=True,
        )
        with app.app_context():
            unchanged = db.session.get(MasterEvent, me_id)
            assert unchanged.name == "Původní název"

    def test_edit_stale_version_rejected(self, app, admin_client):
        with app.app_context():
            me = _make_me("Původní název")
            me_id = me.id

        response = admin_client.post(
            f"/master-events/{me_id}/edit",
            data={"name": "Nový název", "version": "999"},
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "mezitím změněn".encode() in response.data
        with app.app_context():
            unchanged = db.session.get(MasterEvent, me_id)
            assert unchanged.name == "Původní název"

    def test_edit_writes_audit_log(self, app, admin_client):
        with app.app_context():
            me = _make_me("Původní název")
            me_id = me.id
            version = me.version

        admin_client.post(
            f"/master-events/{me_id}/edit",
            data={"name": "Přejmenovaná ME", "version": str(version)},
            follow_redirects=True,
        )
        with app.app_context():
            entry = db.session.scalar(
                db.select(AuditLogEntry)
                .where(AuditLogEntry.entity_type == "MasterEvent")
                .where(AuditLogEntry.action_type == "edit")
                .where(AuditLogEntry.entity_id == str(me_id))
            )
            assert entry is not None

    def test_edit_404_for_missing(self, admin_client):
        response = admin_client.get("/master-events/999999/edit")
        assert response.status_code == 404


# ── Archive / Unarchive ───────────────────────────────────────────────────────


class TestMasterEventArchive:
    def test_admin_can_archive(self, app, admin_client):
        with app.app_context():
            me = _make_me("Archivovatelná ME")
            me_id = me.id

        response = admin_client.post(f"/master-events/{me_id}/archive", follow_redirects=False)
        assert response.status_code == 302
        with app.app_context():
            updated = db.session.get(MasterEvent, me_id)
            assert updated.archived is True

    def test_member_cannot_archive(self, app, member_client):
        with app.app_context():
            me = _make_me("Archivovatelná ME")
            me_id = me.id
        response = member_client.post(f"/master-events/{me_id}/archive")
        assert response.status_code == 403

    def test_cannot_archive_general_master_event(self, app, admin_client):
        with app.app_context():
            me = _make_me("Výchozí ME", is_general=True)
            me_id = me.id

        response = admin_client.post(f"/master-events/{me_id}/archive", follow_redirects=True)
        assert response.status_code == 200
        with app.app_context():
            unchanged = db.session.get(MasterEvent, me_id)
            assert unchanged.archived is False

    def test_admin_can_unarchive(self, app, admin_client):
        with app.app_context():
            me = _make_me("Archivovaná ME", archived=True)
            me_id = me.id

        response = admin_client.post(f"/master-events/{me_id}/unarchive", follow_redirects=False)
        assert response.status_code == 302
        with app.app_context():
            updated = db.session.get(MasterEvent, me_id)
            assert updated.archived is False

    def test_member_cannot_unarchive(self, app, member_client):
        with app.app_context():
            me = _make_me("Archivovaná ME", archived=True)
            me_id = me.id
        response = member_client.post(f"/master-events/{me_id}/unarchive")
        assert response.status_code == 403

    def test_archive_writes_audit_log(self, app, admin_client):
        with app.app_context():
            me = _make_me("Archivovatelná ME")
            me_id = me.id

        admin_client.post(f"/master-events/{me_id}/archive", follow_redirects=True)
        with app.app_context():
            entry = db.session.scalar(
                db.select(AuditLogEntry)
                .where(AuditLogEntry.entity_type == "MasterEvent")
                .where(AuditLogEntry.action_type == "archive")
                .where(AuditLogEntry.entity_id == str(me_id))
            )
            assert entry is not None


# ── Table Manager ─────────────────────────────────────────────────────────────


def _setup_table_manager(app):
    """Create ME with one event (ASSIGNMENTS_OPEN) and one spot. Returns (me_id, event_id, spot_id)."""
    with app.app_context():
        me = _make_me("Table Manager ME")
        event = Event(
            name="Akce TM",
            master_event_id=me.id,
            start_datetime=datetime(2030, 7, 1, 8, 0, tzinfo=timezone.utc),
            end_datetime=datetime(2030, 7, 1, 16, 0, tzinfo=timezone.utc),
            status=EventStatus.ASSIGNMENTS_OPEN,
        )
        db.session.add(event)
        db.session.flush()
        spot = EventSpot(event_id=event.id)
        db.session.add(spot)
        db.session.commit()
        return me.id, event.id, spot.id


class TestTableManager:
    def test_page_requires_login(self, app, client):
        me_id, _, _ = _setup_table_manager(app)
        response = client.get(f"/master-events/{me_id}/table", follow_redirects=False)
        assert response.status_code == 302
        assert "login" in response.headers["Location"]

    def test_admin_can_view_table(self, app, admin_client):
        me_id, _, _ = _setup_table_manager(app)
        response = admin_client.get(f"/master-events/{me_id}/table")
        assert response.status_code == 200
        assert "Tabulkový manažer".encode() in response.data

    def test_member_can_view_table(self, app, member_client):
        me_id, _, _ = _setup_table_manager(app)
        response = member_client.get(f"/master-events/{me_id}/table")
        assert response.status_code == 200

    def test_table_shows_event_name(self, app, admin_client):
        me_id, _, _ = _setup_table_manager(app)
        response = admin_client.get(f"/master-events/{me_id}/table")
        assert b"Akce TM" in response.data

    def test_404_for_missing_me(self, admin_client):
        response = admin_client.get("/master-events/99999/table")
        assert response.status_code == 404

    def test_coordinator_can_assign_spot(self, app, coordinator_client):
        me_id, event_id, spot_id = _setup_table_manager(app)
        with app.app_context():
            _make_user("tm_member@test.com", "TM Member", Role.MEMBER)
            u = db.session.scalar(db.select(UserAccount).where(UserAccount.email == "tm_member@test.com"))
            uid = str(u.id)

        response = coordinator_client.post(
            f"/master-events/{me_id}/table/assign/{spot_id}",
            data={"user_id": uid},
        )
        assert response.status_code == 200
        data = response.get_json()
        assert data["ok"] is True
        assert data["user_name"] == "TM Member"
        with app.app_context():
            assignment = db.session.scalar(db.select(Assignment).where(Assignment.spot_id == spot_id))
            assert assignment is not None

    def test_member_cannot_assign_spot(self, app, member_client):
        me_id, _, spot_id = _setup_table_manager(app)
        response = member_client.post(
            f"/master-events/{me_id}/table/assign/{spot_id}",
            data={"user_id": "any"},
        )
        assert response.status_code == 403

    def test_coordinator_can_unassign_spot(self, app, coordinator_client):
        me_id, event_id, spot_id = _setup_table_manager(app)
        with app.app_context():
            _make_user("tm_member2@test.com", "TM Member2", Role.MEMBER)
            u = db.session.scalar(db.select(UserAccount).where(UserAccount.email == "tm_member2@test.com"))
            assignment = Assignment(spot_id=spot_id, user_id=u.id)
            db.session.add(assignment)
            db.session.commit()
            assignment_id = assignment.id

        response = coordinator_client.post(
            f"/master-events/{me_id}/table/unassign/{assignment_id}",
        )
        assert response.status_code == 200
        data = response.get_json()
        assert data["ok"] is True
        with app.app_context():
            assert db.session.get(Assignment, assignment_id) is None

    def test_event_time_update(self, app, admin_client):
        me_id, event_id, _ = _setup_table_manager(app)
        response = admin_client.post(
            f"/master-events/{me_id}/table/event/{event_id}/update",
            data={"field": "start_datetime", "value": "2030-07-01T09:00"},
        )
        assert response.status_code == 200
        data = response.get_json()
        assert data["ok"] is True
        assert data["display"] == "09:00"

    def test_event_time_update_rejects_invalid_order(self, app, admin_client):
        me_id, event_id, _ = _setup_table_manager(app)
        # Event ends at 16:00 UTC = 18:00 CET; setting start to 19:00 CET should be rejected
        response = admin_client.post(
            f"/master-events/{me_id}/table/event/{event_id}/update",
            data={"field": "start_datetime", "value": "2030-07-01T19:00"},
        )
        assert response.status_code == 400
        data = response.get_json()
        assert data["ok"] is False

    def test_member_cannot_edit_event_time(self, app, member_client):
        me_id, event_id, _ = _setup_table_manager(app)
        response = member_client.post(
            f"/master-events/{me_id}/table/event/{event_id}/update",
            data={"field": "start_datetime", "value": "2030-07-01T09:00"},
        )
        assert response.status_code == 403

    def test_spot_count_increase(self, app, admin_client):
        me_id, event_id, spot_id = _setup_table_manager(app)
        response = admin_client.post(
            f"/master-events/{me_id}/table/spots/update",
            data={"event_id": event_id, "qual_ids_json": "[]", "new_count": "3"},
        )
        assert response.status_code == 200
        assert response.get_json()["ok"] is True
        with app.app_context():
            count = db.session.scalar(
                db.select(db.func.count()).select_from(EventSpot).where(EventSpot.event_id == event_id)
            )
            assert count == 3  # was 1, added 2

    def test_spot_count_decrease_unfilled(self, app, admin_client):
        me_id, event_id, _ = _setup_table_manager(app)
        # Add a 2nd spot first
        with app.app_context():
            db.session.add(EventSpot(event_id=event_id))
            db.session.commit()
        response = admin_client.post(
            f"/master-events/{me_id}/table/spots/update",
            data={"event_id": event_id, "qual_ids_json": "[]", "new_count": "1"},
        )
        assert response.status_code == 200
        assert response.get_json()["ok"] is True
        with app.app_context():
            count = db.session.scalar(
                db.select(db.func.count()).select_from(EventSpot).where(EventSpot.event_id == event_id)
            )
            assert count == 1

    def test_spot_count_decrease_blocks_if_filled(self, app, admin_client):
        me_id, event_id, spot_id = _setup_table_manager(app)
        with app.app_context():
            _make_user("tm_fill@test.com", "TM Fill", Role.MEMBER)
            u = db.session.scalar(db.select(UserAccount).where(UserAccount.email == "tm_fill@test.com"))
            db.session.add(Assignment(spot_id=spot_id, user_id=u.id))
            db.session.commit()
        response = admin_client.post(
            f"/master-events/{me_id}/table/spots/update",
            data={"event_id": event_id, "qual_ids_json": "[]", "new_count": "0"},
        )
        assert response.status_code == 409
        assert response.get_json()["ok"] is False

    def test_member_cannot_update_spot_count(self, app, member_client):
        me_id, event_id, _ = _setup_table_manager(app)
        response = member_client.post(
            f"/master-events/{me_id}/table/spots/update",
            data={"event_id": event_id, "qual_ids_json": "[]", "new_count": "2"},
        )
        assert response.status_code == 403

    # ── shift_hour ────────────────────────────────────────────────────────────

    def test_shift_hour_start_plus_one(self, app, admin_client):
        me_id, event_id, _ = _setup_table_manager(app)
        with app.app_context():
            orig_start = db.session.get(Event, event_id).start_datetime
        response = admin_client.post(
            f"/master-events/{me_id}/table/event/{event_id}/update",
            data={"field": "shift_hour", "value": "start:1"},
        )
        assert response.status_code == 200
        assert response.get_json()["ok"] is True
        with app.app_context():
            new_start = db.session.get(Event, event_id).start_datetime
            assert new_start == orig_start + timedelta(hours=1)

    def test_shift_hour_end_minus_one(self, app, admin_client):
        me_id, event_id, _ = _setup_table_manager(app)
        with app.app_context():
            orig_end = db.session.get(Event, event_id).end_datetime
        response = admin_client.post(
            f"/master-events/{me_id}/table/event/{event_id}/update",
            data={"field": "shift_hour", "value": "end:-1"},
        )
        assert response.status_code == 200
        assert response.get_json()["ok"] is True
        with app.app_context():
            new_end = db.session.get(Event, event_id).end_datetime
            assert new_end == orig_end + timedelta(hours=-1)

    def test_shift_hour_invalid_value(self, app, admin_client):
        me_id, event_id, _ = _setup_table_manager(app)
        response = admin_client.post(
            f"/master-events/{me_id}/table/event/{event_id}/update",
            data={"field": "shift_hour", "value": "start:bogus"},
        )
        assert response.status_code == 400

    def test_shift_hour_invalid_which(self, app, admin_client):
        me_id, event_id, _ = _setup_table_manager(app)
        response = admin_client.post(
            f"/master-events/{me_id}/table/event/{event_id}/update",
            data={"field": "shift_hour", "value": "middle:1"},
        )
        assert response.status_code == 400

    # ── advance_status ────────────────────────────────────────────────────────

    def _make_draft_event(self, app, me_id: int) -> int:
        with app.app_context():
            event = Event(
                name="Draft TM",
                master_event_id=me_id,
                start_datetime=datetime(2030, 8, 1, 8, 0, tzinfo=timezone.utc),
                end_datetime=datetime(2030, 8, 1, 16, 0, tzinfo=timezone.utc),
                status=EventStatus.DRAFT,
            )
            db.session.add(event)
            db.session.commit()
            return event.id

    def _make_published_event(self, app, me_id: int) -> int:
        with app.app_context():
            event = Event(
                name="Published TM",
                master_event_id=me_id,
                start_datetime=datetime(2030, 8, 2, 8, 0, tzinfo=timezone.utc),
                end_datetime=datetime(2030, 8, 2, 16, 0, tzinfo=timezone.utc),
                status=EventStatus.PUBLISHED,
            )
            db.session.add(event)
            db.session.commit()
            return event.id

    def test_advance_draft_to_published(self, app, admin_client):
        me_id, _, _ = _setup_table_manager(app)
        event_id = self._make_draft_event(app, me_id)
        response = admin_client.post(
            f"/master-events/{me_id}/table/event/{event_id}/update",
            data={"field": "advance_status"},
        )
        assert response.status_code == 200
        assert response.get_json()["ok"] is True
        with app.app_context():
            event = db.session.get(Event, event_id)
            assert event.status == EventStatus.PUBLISHED

    def test_advance_published_to_open(self, app, admin_client):
        me_id, _, _ = _setup_table_manager(app)
        event_id = self._make_published_event(app, me_id)
        response = admin_client.post(
            f"/master-events/{me_id}/table/event/{event_id}/update",
            data={"field": "advance_status"},
        )
        assert response.status_code == 200
        assert response.get_json()["ok"] is True
        with app.app_context():
            event = db.session.get(Event, event_id)
            assert event.status == EventStatus.ASSIGNMENTS_OPEN

    def test_advance_status_already_open_returns_400(self, app, admin_client):
        me_id, event_id, _ = _setup_table_manager(app)
        # event is ASSIGNMENTS_OPEN — not advanceable
        response = admin_client.post(
            f"/master-events/{me_id}/table/event/{event_id}/update",
            data={"field": "advance_status"},
        )
        assert response.status_code == 400
        assert response.get_json()["ok"] is False


class TestTableEventClone:
    """Table Manager clone should not copy responsible_person_id."""

    def test_clone_does_not_copy_rp(self, app, admin_client):

        with app.app_context():
            me = _make_me("Clone ME")
            db.session.add(me)
            db.session.flush()

            admin = db.session.scalar(db.select(UserAccount).where(UserAccount.email == "admin@test.com"))

            event = Event(
                name="Source Event",
                master_event_id=me.id,
                start_datetime=datetime(2035, 2, 1, 10, 0, tzinfo=timezone.utc),
                end_datetime=datetime(2035, 2, 1, 18, 0, tzinfo=timezone.utc),
                status=EventStatus.ASSIGNMENTS_OPEN,
                responsible_person_id=admin.id,
                created_by_id=admin.id,
            )
            db.session.add(event)
            db.session.flush()

            spot = EventSpot(event_id=event.id, description="Záchranář")
            db.session.add(spot)
            db.session.commit()

            me_id = me.id
            event_id = event.id

        resp = admin_client.post(f"/master-events/{me_id}/table/event/{event_id}/clone")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True

        with app.app_context():
            clone = db.session.get(Event, data["new_event_id"])
            assert clone is not None
            assert clone.responsible_person_id is None
            assert clone.name == "Source Event kopie"
            assert len(clone.spots) == 1


# ── Master Event Archive Cascade ─────────────────────────────────────────────


class TestMasterEventArchiveCascade:
    def _make_me_with_events(self, app, count: int = 2) -> tuple[int, list[int]]:
        """Create a ME with `count` PUBLISHED events. Returns (me_id, [event_ids])."""
        with app.app_context():
            me = MasterEvent(name=f"Cascade ME {count}")
            db.session.add(me)
            db.session.flush()
            event_ids = []
            for i in range(count):
                event = Event(
                    name=f"Cascade Event {i + 1}",
                    master_event_id=me.id,
                    status=EventStatus.PUBLISHED,
                    start_datetime=datetime(2030, 9, i + 1, 8, 0, tzinfo=timezone.utc),
                    end_datetime=datetime(2030, 9, i + 1, 16, 0, tzinfo=timezone.utc),
                )
                db.session.add(event)
                db.session.flush()
                event_ids.append(event.id)
            db.session.commit()
            return me.id, event_ids

    def test_archiving_me_trashes_all_events(self, app, admin_client):
        me_id, event_ids = self._make_me_with_events(app, count=2)
        admin_client.post(f"/master-events/{me_id}/archive", follow_redirects=False)
        with app.app_context():
            for eid in event_ids:
                event = db.session.get(Event, eid)
                assert event is not None
                assert event.archived is True, f"Event {eid} should be archived"

    def test_archiving_me_trashes_only_non_previously_trashed_events(self, app, admin_client):
        with app.app_context():
            me = MasterEvent(name="Partial Cascade ME")
            db.session.add(me)
            db.session.flush()
            # Event already archived before cascade runs
            ev_already_trashed = Event(
                name="Already Archived",
                master_event_id=me.id,
                status=EventStatus.PUBLISHED,
                archived=True,
                start_datetime=datetime(2030, 10, 1, 8, 0, tzinfo=timezone.utc),
                end_datetime=datetime(2030, 10, 1, 16, 0, tzinfo=timezone.utc),
            )
            db.session.add(ev_already_trashed)
            # Event not yet archived
            ev_active = Event(
                name="Active Event",
                master_event_id=me.id,
                status=EventStatus.PUBLISHED,
                archived=False,
                start_datetime=datetime(2030, 10, 2, 8, 0, tzinfo=timezone.utc),
                end_datetime=datetime(2030, 10, 2, 16, 0, tzinfo=timezone.utc),
            )
            db.session.add(ev_active)
            db.session.commit()
            me_id = me.id
            ev_already_id = ev_already_trashed.id
            ev_already_version = ev_already_trashed.version
            ev_active_id = ev_active.id

        admin_client.post(f"/master-events/{me_id}/archive", follow_redirects=False)
        with app.app_context():
            already = db.session.get(Event, ev_already_id)
            active = db.session.get(Event, ev_active_id)
            # Active event cascaded: archived and version bumped
            assert active.archived is True
            assert active.version == ev_already_version + 1
            # Pre-archived event: still archived but version untouched —
            # confirms the query filter (archived == false) truly skipped it
            assert already.archived is True
            assert already.version == ev_already_version

    def test_archiving_me_audit_log_contains_affected_event_ids(self, app, admin_client):
        me_id, event_ids = self._make_me_with_events(app, count=2)
        admin_client.post(f"/master-events/{me_id}/archive", follow_redirects=False)
        with app.app_context():
            me_entry = db.session.scalar(
                db.select(AuditLogEntry)
                .where(AuditLogEntry.entity_type == "MasterEvent")
                .where(AuditLogEntry.action_type == "archive")
                .where(AuditLogEntry.entity_id == str(me_id))
            )
            assert me_entry is not None
            assert me_entry.changes_json is not None
            affected = me_entry.changes_json.get("affected_event_ids", [])
            for eid in event_ids:
                assert eid in affected

    def test_archiving_me_with_no_events_succeeds(self, app, admin_client):
        with app.app_context():
            me = MasterEvent(name="Empty Cascade ME")
            db.session.add(me)
            db.session.commit()
            me_id = me.id
        response = admin_client.post(f"/master-events/{me_id}/archive", follow_redirects=False)
        assert response.status_code == 302
        with app.app_context():
            me = db.session.get(MasterEvent, me_id)
            assert me.archived is True

    def test_archiving_me_writes_per_event_audit_log_entries(self, app, admin_client):
        me_id, event_ids = self._make_me_with_events(app, count=2)
        admin_client.post(f"/master-events/{me_id}/archive", follow_redirects=False)
        with app.app_context():
            for eid in event_ids:
                entry = db.session.scalar(
                    db.select(AuditLogEntry)
                    .where(AuditLogEntry.entity_type == "Event")
                    .where(AuditLogEntry.action_type == "archive")
                    .where(AuditLogEntry.entity_id == str(eid))
                )
                assert entry is not None, f"Expected an AuditLog 'archive' entry for Event id={eid} but none was found"

    def test_unarchiving_me_does_not_restore_events(self, app, admin_client):
        me_id, event_ids = self._make_me_with_events(app, count=1)
        # Archive the ME (cascades to events)
        admin_client.post(f"/master-events/{me_id}/archive", follow_redirects=False)
        # Unarchive the ME
        admin_client.post(f"/master-events/{me_id}/unarchive", follow_redirects=False)
        with app.app_context():
            me = db.session.get(MasterEvent, me_id)
            assert me.archived is False
            # Events remain archived
            for eid in event_ids:
                event = db.session.get(Event, eid)
                assert event.archived is True, f"Event {eid} should remain archived"

    def test_unarchiving_me_hints_events_remain_archived(self, app, admin_client):
        me_id, _ = self._make_me_with_events(app, count=1)
        admin_client.post(f"/master-events/{me_id}/archive", follow_redirects=False)
        resp = admin_client.post(f"/master-events/{me_id}/unarchive", follow_redirects=True)
        assert resp.status_code == 200
        # The user is warned that the ME's events stay archived (no unarchive cascade)
        assert "zůstává archivováno" in resp.data.decode()

    def test_me_detail_excludes_archived_from_stats_and_shows_archived_count(self, app, admin_client):
        me_id, event_ids = self._make_me_with_events(app, count=2)
        with app.app_context():
            archived = db.session.get(Event, event_ids[0])
            archived.archived = True
            db.session.commit()

        resp = admin_client.get(f"/master-events/{me_id}")
        body = resp.data.decode()
        assert resp.status_code == 200
        # Archived event is still listed (viewable) and the archived count card appears
        assert "Cascade Event 1" in body
        assert "Archivováno" in body
        # "Celkem akcí" (total) counts only active events — 1 of the 2 is archived.
        assert '<div class="fs-3 fw-bold">1</div>' in body
