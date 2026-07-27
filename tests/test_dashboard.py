"""Tests for the main dashboard: event sort order (#113) and user-name links (#105)."""

from datetime import datetime, timedelta, timezone

from app.extensions import db
from app.models.assignment import Assignment
from app.models.equipment import EquipmentItem, EquipmentType, EventEquipmentPlan
from app.models.event import Event, EventSpot, EventStatus
from app.models.master_event import MasterEvent
from app.models.user import UserAccount


class TestDashboardEventSortOrder:
    """#113 — 'Moje akce' must appear sorted by start_datetime, not creation time."""

    def test_my_events_sorted_by_start_datetime(self, app, member_client):
        now = datetime.now(timezone.utc)

        with app.app_context():
            member = db.session.scalar(db.select(UserAccount).where(UserAccount.email == "member@test.com"))
            me = MasterEvent(name="Sort Test ME")
            db.session.add(me)
            db.session.flush()

            # Create events in REVERSE date order so creation time ≠ start time order
            ev_far = Event(
                name="Far Future Event",
                master_event_id=me.id,
                status=EventStatus.ASSIGNMENTS_OPEN,
                start_datetime=now + timedelta(days=20),
                end_datetime=now + timedelta(days=20, hours=4),
            )
            ev_mid = Event(
                name="Mid Future Event",
                master_event_id=me.id,
                status=EventStatus.ASSIGNMENTS_OPEN,
                start_datetime=now + timedelta(days=10),
                end_datetime=now + timedelta(days=10, hours=4),
            )
            ev_near = Event(
                name="Near Future Event",
                master_event_id=me.id,
                status=EventStatus.ASSIGNMENTS_OPEN,
                start_datetime=now + timedelta(days=2),
                end_datetime=now + timedelta(days=2, hours=4),
            )
            db.session.add_all([ev_far, ev_mid, ev_near])
            db.session.flush()

            # Assign member to each event via a spot
            for ev in (ev_far, ev_mid, ev_near):
                spot = EventSpot(event_id=ev.id)
                db.session.add(spot)
                db.session.flush()
                db.session.add(Assignment(spot_id=spot.id, user_id=member.id, assigned_by_id=member.id))
            db.session.commit()

        response = member_client.get("/dashboard")
        assert response.status_code == 200
        body = response.data.decode()

        pos_near = body.index("Near Future Event")
        pos_mid = body.index("Mid Future Event")
        pos_far = body.index("Far Future Event")

        # Near should appear before Mid, Mid before Far
        assert (
            pos_near < pos_mid < pos_far
        ), "Dashboard 'Moje akce' events must be ordered by start_datetime (nearest first)"


class TestDashboardPendingActivationLink:
    """#105 — Pending-activation user name must be a hyperlink to their profile."""

    def test_inactive_user_name_is_a_link(self, app, admin_client):
        with app.app_context():
            inactive = UserAccount(
                email="inactive_link@test.com",
                name="Pending Link User",
                is_active=False,
            )
            inactive.set_password("testpass123")
            db.session.add(inactive)
            db.session.commit()
            inactive_id = str(inactive.id)

        response = admin_client.get("/dashboard")
        assert response.status_code == 200
        body = response.data.decode()

        assert "Pending Link User" in body
        assert f"/users/{inactive_id}" in body
        # The name must be wrapped in an anchor tag, not plain text
        assert f'href="/users/{inactive_id}"' in body


class TestDashboardEquipmentShortage:
    """Equipment shortage section must appear for events competing for the same type."""

    def _make_overlapping_event(self, app, me_id: int, name: str) -> int:
        future = datetime.now(timezone.utc) + timedelta(days=5)
        with app.app_context():
            ev = Event(
                name=name,
                master_event_id=me_id,
                status=EventStatus.PUBLISHED,
                start_datetime=future,
                end_datetime=future + timedelta(hours=8),
            )
            db.session.add(ev)
            db.session.commit()
            return ev.id

    def test_single_event_shortage_shown(self, app, admin_client):
        """One item, one event needing two → shortage must appear on dashboard."""
        with app.app_context():
            me = MasterEvent(name="Shortage ME Single")
            db.session.add(me)
            db.session.flush()
            me_id = me.id
            et = EquipmentType(name="Shortage Type S")
            db.session.add(et)
            db.session.flush()
            db.session.add(EquipmentItem(name="Shortage Item S", type_id=et.id))
            db.session.commit()
            type_id = et.id

        event_id = self._make_overlapping_event(app, me_id, "Shortage Event S")
        with app.app_context():
            db.session.add(EventEquipmentPlan(
                event_id=event_id, equipment_type_id=type_id, quantity_required=2
            ))
            db.session.commit()

        resp = admin_client.get("/dashboard")
        assert resp.status_code == 200
        assert "Shortage Event S" in resp.data.decode()
        assert "Nedostatek vybavení" in resp.data.decode() or "Nedostatek" in resp.data.decode()

    def test_two_overlapping_events_both_flagged(self, app, admin_client):
        """Two overlapping events each needing the only item must both appear as short."""
        with app.app_context():
            me = MasterEvent(name="Shortage ME Two")
            db.session.add(me)
            db.session.flush()
            me_id = me.id
            et = EquipmentType(name="Shortage Type T")
            db.session.add(et)
            db.session.flush()
            db.session.add(EquipmentItem(name="Shortage Item T", type_id=et.id))
            db.session.commit()
            type_id = et.id

        event_a_id = self._make_overlapping_event(app, me_id, "Shortage Event A")
        event_b_id = self._make_overlapping_event(app, me_id, "Shortage Event B")
        with app.app_context():
            for eid in (event_a_id, event_b_id):
                db.session.add(EventEquipmentPlan(
                    event_id=eid, equipment_type_id=type_id, quantity_required=1
                ))
            db.session.commit()

        resp = admin_client.get("/dashboard")
        assert resp.status_code == 200
        body = resp.data.decode()
        # Both events compete for the one item — both must be shown as short
        assert "Shortage Event A" in body
        assert "Shortage Event B" in body
