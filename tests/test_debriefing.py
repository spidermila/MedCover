"""Tests for the debriefing blueprint — redesigned two-part form, final submission."""
from __future__ import annotations

from datetime import datetime
from datetime import timezone

from app.extensions import db
from app.models.assignment import Assignment
from app.models.assignment import DebriefingRecord
from app.models.audit import AuditLogEntry
from app.models.event import Event
from app.models.event import EventSpot
from app.models.event import EventStatus
from app.models.master_event import MasterEvent
from app.models.role import Role
from tests.conftest import _login
from tests.conftest import _make_user

_ASSIGNED_EMAIL = "assigned_member@test.com"
_DEBRIEF_MGR_EMAIL = "debrief_manager@test.com"


def _setup_completed_assignment(app, *, is_rp: bool = False) -> tuple[int, int, int]:
    """Create a completed event with one spot assigned to _ASSIGNED_EMAIL.

    If is_rp=True the assigned user is also set as the event's responsible_person.
    Returns (event_id, spot_id, assignment_id).
    """
    with app.app_context():
        me = MasterEvent(name="Test ME")
        db.session.add(me)
        db.session.flush()

        creator = _make_user("debrief_creator@test.com", "Creator", Role.ADMIN)
        assigned = _make_user(_ASSIGNED_EMAIL, "Assigned Member", Role.MEMBER)

        event = Event(
            name="Completed Event",
            master_event_id=me.id,
            start_datetime=datetime(2024, 1, 1, 10, 0, tzinfo=timezone.utc),
            end_datetime=datetime(2024, 1, 1, 18, 0, tzinfo=timezone.utc),
            status=EventStatus.COMPLETED,
            created_by_id=creator.id,
            responsible_person_id=assigned.id if is_rp else None,
        )
        db.session.add(event)
        db.session.flush()

        spot = EventSpot(event_id=event.id)
        db.session.add(spot)
        db.session.flush()

        assignment = Assignment(
            spot_id=spot.id,
            user_id=assigned.id,
            assigned_by_id=creator.id,
        )
        db.session.add(assignment)
        db.session.commit()

        return event.id, spot.id, assignment.id


def _debrief_manager_client(app):
    """Return a test client logged in as a Debriefing Manager."""
    with app.app_context():
        _make_user(_DEBRIEF_MGR_EMAIL, "Debrief Manager", Role.DEBRIEFING_MANAGER)
    c = app.test_client()
    _login(c, _DEBRIEF_MGR_EMAIL)
    return c


def _assigned_client(app):
    """Return a test client logged in as the assigned member."""
    c = app.test_client()
    _login(c, _ASSIGNED_EMAIL)
    return c


_VALID_FORM = {
    "grade": "2",
    "feedback_event": "Vše proběhlo hladce.",
    "feedback_customer": "Objednatel byl vstřícný.",
    "feedback_colleagues": "Tým fungoval skvěle.",
}


# ── Access control ────────────────────────────────────────────────────────────

class TestDebriefingAccess:
    def test_submit_requires_login(self, app, client):
        _, _, assignment_id = _setup_completed_assignment(app)
        resp = client.get(f"/debriefing/{assignment_id}", follow_redirects=False)
        assert resp.status_code == 302
        assert "login" in resp.headers["Location"]

    def test_assigned_user_can_view_form(self, app):
        _, _, assignment_id = _setup_completed_assignment(app)
        c = _assigned_client(app)
        resp = c.get(f"/debriefing/{assignment_id}")
        assert resp.status_code == 200

    def test_other_user_cannot_view_form(self, app, member_client):
        """member_client is member@test.com — not the assigned user."""
        _, _, assignment_id = _setup_completed_assignment(app)
        resp = member_client.get(f"/debriefing/{assignment_id}")
        assert resp.status_code == 403

    def test_admin_cannot_view_others_form(self, app, admin_client):
        _, _, assignment_id = _setup_completed_assignment(app)
        resp = admin_client.get(f"/debriefing/{assignment_id}")
        assert resp.status_code == 403

    def test_404_for_missing_assignment(self, admin_client):
        resp = admin_client.get("/debriefing/999999")
        assert resp.status_code == 404

    def test_form_redirects_when_event_not_completed(self, app):
        """Form must redirect with a warning when event is not Completed."""
        with app.app_context():
            me = MasterEvent(name="Active ME")
            db.session.add(me)
            db.session.flush()
            creator = _make_user("active_creator@test.com", "Creator", Role.ADMIN)
            assigned = _make_user("active_assigned@test.com", "Assigned", Role.MEMBER)
            event = Event(
                name="Open Event",
                master_event_id=me.id,
                start_datetime=datetime(2030, 6, 1, 10, 0, tzinfo=timezone.utc),
                end_datetime=datetime(2030, 6, 1, 18, 0, tzinfo=timezone.utc),
                status=EventStatus.ASSIGNMENTS_OPEN,
                created_by_id=creator.id,
            )
            db.session.add(event)
            db.session.flush()
            spot = EventSpot(event_id=event.id)
            db.session.add(spot)
            db.session.flush()
            assignment = Assignment(spot_id=spot.id, user_id=assigned.id, assigned_by_id=creator.id)
            db.session.add(assignment)
            db.session.commit()
            assignment_id = assignment.id

        c = app.test_client()
        _login(c, "active_assigned@test.com")
        resp = c.get(f"/debriefing/{assignment_id}", follow_redirects=True)
        assert resp.status_code == 200
        assert b"lze vyplnit" in resp.data


# ── Submit ────────────────────────────────────────────────────────────────────

class TestDebriefingSubmit:
    def test_valid_submission_creates_record(self, app):
        _, _, assignment_id = _setup_completed_assignment(app)
        c = _assigned_client(app)
        resp = c.post(f"/debriefing/{assignment_id}", data=_VALID_FORM, follow_redirects=True)
        assert resp.status_code == 200
        with app.app_context():
            record = db.session.scalar(
                db.select(DebriefingRecord).where(DebriefingRecord.assignment_id == assignment_id)
            )
            assert record is not None
            assert record.grade == 2
            assert record.feedback_event == "Vše proběhlo hladce."
            assert record.feedback_customer == "Objednatel byl vstřícný."
            assert record.feedback_colleagues == "Tým fungoval skvěle."

    def test_missing_grade_rejected(self, app):
        _, _, assignment_id = _setup_completed_assignment(app)
        c = _assigned_client(app)
        data = {**_VALID_FORM, "grade": ""}
        resp = c.post(f"/debriefing/{assignment_id}", data=data, follow_redirects=True)
        assert resp.status_code == 200
        assert "Hodnocení".encode() in resp.data
        with app.app_context():
            count = db.session.scalar(
                db.select(db.func.count()).select_from(DebriefingRecord)
                .where(DebriefingRecord.assignment_id == assignment_id)
            )
            assert count == 0

    def test_grade_out_of_range_rejected(self, app):
        _, _, assignment_id = _setup_completed_assignment(app)
        c = _assigned_client(app)
        for bad in ["0", "6", "abc"]:
            data = {**_VALID_FORM, "grade": bad}
            resp = c.post(f"/debriefing/{assignment_id}", data=data, follow_redirects=True)
            assert resp.status_code == 200
            with app.app_context():
                count = db.session.scalar(
                    db.select(db.func.count()).select_from(DebriefingRecord)
                    .where(DebriefingRecord.assignment_id == assignment_id)
                )
                assert count == 0

    def test_optional_fields_may_be_empty(self, app):
        _, _, assignment_id = _setup_completed_assignment(app)
        c = _assigned_client(app)
        resp = c.post(
            f"/debriefing/{assignment_id}",
            data={"grade": "3"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        with app.app_context():
            record = db.session.scalar(
                db.select(DebriefingRecord).where(DebriefingRecord.assignment_id == assignment_id)
            )
            assert record is not None
            assert record.grade == 3
            assert record.feedback_event is None
            assert record.feedback_customer is None
            assert record.feedback_colleagues is None

    def test_submission_is_final(self, app):
        """Submitting a second time should not create a second record or update."""
        _, _, assignment_id = _setup_completed_assignment(app)
        c = _assigned_client(app)
        c.post(f"/debriefing/{assignment_id}", data=_VALID_FORM, follow_redirects=True)
        # Second attempt — should show read-only view (submitted.html), not update
        resp = c.get(f"/debriefing/{assignment_id}")
        assert resp.status_code == 200
        assert "Debriefing byl odevzdán".encode() in resp.data
        # POST again should be refused (or show submitted page)
        resp2 = c.post(
            f"/debriefing/{assignment_id}",
            data={**_VALID_FORM, "grade": "5"},
            follow_redirects=True,
        )
        assert resp2.status_code == 200
        with app.app_context():
            record = db.session.scalar(
                db.select(DebriefingRecord).where(DebriefingRecord.assignment_id == assignment_id)
            )
            assert record.grade == 2  # still the original grade

    def test_submit_creates_audit_entry(self, app):
        _, _, assignment_id = _setup_completed_assignment(app)
        c = _assigned_client(app)
        c.post(f"/debriefing/{assignment_id}", data=_VALID_FORM, follow_redirects=True)
        with app.app_context():
            entry = db.session.scalar(
                db.select(AuditLogEntry)
                .where(AuditLogEntry.entity_type == "DebriefingRecord")
                .where(AuditLogEntry.action_type == "create")
            )
            assert entry is not None


# ── RP section ────────────────────────────────────────────────────────────────

class TestDebriefingRPSection:
    def test_rp_section_visible_for_responsible_person(self, app):
        _, _, assignment_id = _setup_completed_assignment(app, is_rp=True)
        c = _assigned_client(app)
        resp = c.get(f"/debriefing/{assignment_id}")
        assert resp.status_code == 200
        assert "Skutečný začátek".encode() in resp.data

    def test_rp_section_not_visible_for_non_rp(self, app):
        _, _, assignment_id = _setup_completed_assignment(app, is_rp=False)
        c = _assigned_client(app)
        resp = c.get(f"/debriefing/{assignment_id}")
        assert resp.status_code == 200
        assert "Skutečný začátek".encode() not in resp.data

    def test_rp_submission_updates_event_actuals(self, app):
        event_id, _, assignment_id = _setup_completed_assignment(app, is_rp=True)
        c = _assigned_client(app)
        form_data = {
            **_VALID_FORM,
            "actual_start_datetime": "2024-01-01T10:15",
            "actual_end_datetime": "2024-01-01T17:45",
            "post_event_count": "3",
        }
        resp = c.post(f"/debriefing/{assignment_id}", data=form_data, follow_redirects=True)
        assert resp.status_code == 200
        with app.app_context():
            event = db.session.get(Event, event_id)
            assert event.actual_start_datetime is not None
            assert event.actual_end_datetime is not None
            assert event.post_event_count == 3

    def test_rp_invalid_times_rejected(self, app):
        _, _, assignment_id = _setup_completed_assignment(app, is_rp=True)
        c = _assigned_client(app)
        form_data = {
            **_VALID_FORM,
            "actual_start_datetime": "not-a-date",
            "actual_end_datetime": "also-not",
            "post_event_count": "0",
        }
        resp = c.post(f"/debriefing/{assignment_id}", data=form_data, follow_redirects=True)
        assert resp.status_code == 200
        with app.app_context():
            count = db.session.scalar(
                db.select(db.func.count()).select_from(DebriefingRecord)
                .where(DebriefingRecord.assignment_id == assignment_id)
            )
            assert count == 0

    def test_rp_end_before_start_rejected(self, app):
        _, _, assignment_id = _setup_completed_assignment(app, is_rp=True)
        c = _assigned_client(app)
        form_data = {
            **_VALID_FORM,
            "actual_start_datetime": "2024-01-01T18:00",
            "actual_end_datetime": "2024-01-01T10:00",
            "post_event_count": "0",
        }
        resp = c.post(f"/debriefing/{assignment_id}", data=form_data, follow_redirects=True)
        assert resp.status_code == 200
        with app.app_context():
            count = db.session.scalar(
                db.select(db.func.count()).select_from(DebriefingRecord)
                .where(DebriefingRecord.assignment_id == assignment_id)
            )
            assert count == 0


# ── Manage page (Debriefing Manager only) ─────────────────────────────────────

class TestDebriefingManage:
    def test_manage_requires_login(self, client):
        resp = client.get("/debriefing/manage", follow_redirects=False)
        assert resp.status_code == 302

    def test_debriefing_manager_can_access_manage(self, app):
        _setup_completed_assignment(app)
        c = _debrief_manager_client(app)
        resp = c.get("/debriefing/manage")
        assert resp.status_code == 200

    def test_admin_cannot_access_manage(self, app, admin_client):
        resp = admin_client.get("/debriefing/manage")
        assert resp.status_code == 403

    def test_coordinator_cannot_access_manage(self, app, coordinator_client):
        resp = coordinator_client.get("/debriefing/manage")
        assert resp.status_code == 403

    def test_member_cannot_access_manage(self, app, member_client):
        resp = member_client.get("/debriefing/manage")
        assert resp.status_code == 403

    def test_event_overview_debriefing_manager(self, app):
        event_id, _, _ = _setup_completed_assignment(app)
        c = _debrief_manager_client(app)
        resp = c.get(f"/debriefing/event/{event_id}")
        assert resp.status_code == 200

    def test_admin_cannot_access_event_overview(self, app, admin_client):
        event_id, _, _ = _setup_completed_assignment(app)
        resp = admin_client.get(f"/debriefing/event/{event_id}")
        assert resp.status_code == 403

    def test_coordinator_cannot_access_event_overview(self, app, coordinator_client):
        event_id, _, _ = _setup_completed_assignment(app)
        resp = coordinator_client.get(f"/debriefing/event/{event_id}")
        assert resp.status_code == 403


class TestDebriefingEventTypes:
    """Test debriefing behaviour differs by event type."""

    def _setup_training_assignment(self, app, *, is_rp: bool = False):
        """Like _setup_completed_assignment but for a TRAINING event."""
        from app.models.event import EventType
        with app.app_context():
            me = MasterEvent(name="Test ME Training")
            db.session.add(me)
            db.session.flush()
            creator = _make_user("training_creator@test.com", "Creator T", Role.ADMIN)
            assigned = _make_user("training_assigned@test.com", "Assigned T", Role.MEMBER)
            event = Event(
                name="Training Event",
                master_event_id=me.id,
                start_datetime=datetime(2024, 3, 1, 9, 0, tzinfo=timezone.utc),
                end_datetime=datetime(2024, 3, 1, 15, 0, tzinfo=timezone.utc),
                status=EventStatus.COMPLETED,
                created_by_id=creator.id,
                responsible_person_id=assigned.id if is_rp else None,
                event_type=EventType.TRAINING,
            )
            db.session.add(event)
            db.session.flush()
            spot = EventSpot(event_id=event.id)
            db.session.add(spot)
            db.session.flush()
            assignment = Assignment(
                spot_id=spot.id, user_id=assigned.id, assigned_by_id=creator.id
            )
            db.session.add(assignment)
            db.session.commit()
            return event.id, spot.id, assignment.id

    def test_training_rp_can_submit_without_actuals(self, app):
        """Training RP section is optional — submission without times succeeds."""
        event_id, _, assignment_id = self._setup_training_assignment(app, is_rp=True)
        c = app.test_client()
        _login(c, "training_assigned@test.com")
        resp = c.post(
            f"/debriefing/{assignment_id}",
            data=_VALID_FORM,
            follow_redirects=True,
        )
        assert resp.status_code == 200
        with app.app_context():
            record = db.session.scalar(
                db.select(DebriefingRecord).where(
                    DebriefingRecord.assignment_id == assignment_id
                )
            )
            assert record is not None
            ev = db.session.get(Event, event_id)
            # Actuals not required — should still be None (not set)
            assert ev.actual_start_datetime is None

    def test_presentation_rp_section_not_shown(self, app):
        """PRESENTATION events: no RP section even for responsible person."""
        from app.models.event import EventType
        with app.app_context():
            me = MasterEvent(name="Pres ME")
            db.session.add(me)
            db.session.flush()
            creator = _make_user("pres_creator@test.com", "Creator P", Role.ADMIN)
            assigned = _make_user("pres_assigned@test.com", "Assigned P", Role.MEMBER)
            event = Event(
                name="Presentation Event",
                master_event_id=me.id,
                start_datetime=datetime(2024, 4, 1, 9, 0, tzinfo=timezone.utc),
                end_datetime=datetime(2024, 4, 1, 12, 0, tzinfo=timezone.utc),
                status=EventStatus.COMPLETED,
                created_by_id=creator.id,
                responsible_person_id=assigned.id,
                event_type=EventType.PRESENTATION,
            )
            db.session.add(event)
            db.session.flush()
            spot = EventSpot(event_id=event.id)
            db.session.add(spot)
            db.session.flush()
            assignment = Assignment(
                spot_id=spot.id, user_id=assigned.id, assigned_by_id=creator.id
            )
            db.session.add(assignment)
            db.session.commit()
            assignment_id = assignment.id

        c = app.test_client()
        _login(c, "pres_assigned@test.com")
        resp = c.get(f"/debriefing/{assignment_id}")
        assert resp.status_code == 200
        # RP section should not appear for PRESENTATION
        assert "Skutečný začátek".encode() not in resp.data
