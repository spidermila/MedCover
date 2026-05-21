"""Tests for the iCal calendar feed (/calendar/<token>.ics)."""
from __future__ import annotations

from datetime import datetime, timezone

from app.extensions import db
from app.models.assignment import Assignment
from app.models.audit import AuditLogEntry
from app.models.event import Event, EventSpot, EventStatus
from app.models.master_event import MasterEvent
from app.models.user import UserAccount


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_event(
    app,
    name: str = "Test Event",
    status: EventStatus = EventStatus.ASSIGNMENTS_OPEN,
    address: str = "Praha, Náměstí Míru",
) -> tuple[int, int]:
    """Create a MasterEvent + Event + EventSpot; return (event_id, spot_id)."""
    with app.app_context():
        from app.models.role import Role

        me = MasterEvent(name=f"ME for {name}")
        db.session.add(me)
        db.session.flush()

        role = db.session.scalar(db.select(Role).where(Role.name == Role.ADMIN))
        creator = UserAccount(email=f"creator_{name.lower().replace(' ', '_')}@test.com", name="Creator", is_active=True)
        creator.set_password("x")
        creator.roles = [role]
        db.session.add(creator)
        db.session.flush()

        event = Event(
            name=name,
            master_event_id=me.id,
            status=status,
            start_datetime=datetime(2030, 8, 1, 9, 0, tzinfo=timezone.utc),
            end_datetime=datetime(2030, 8, 1, 17, 0, tzinfo=timezone.utc),
            address=address,
            created_by_id=creator.id,
        )
        db.session.add(event)
        db.session.flush()

        spot = EventSpot(event_id=event.id, description="Zdravotník")
        db.session.add(spot)
        db.session.commit()
        return event.id, spot.id


def _assign_user(app, user_id, spot_id: int) -> None:
    with app.app_context():
        db.session.add(Assignment(user_id=user_id, spot_id=spot_id))
        db.session.commit()


def _make_member(app, email: str = "ical_member@test.com") -> object:
    """Create an active member user with an iCal token; return (id, token)."""
    with app.app_context():
        from app.models.role import Role
        role = db.session.scalar(db.select(Role).where(Role.name == Role.MEMBER))
        user = UserAccount(email=email, name="iCal Member", is_active=True)
        user.set_password("testpass123")
        user.roles = [role]
        user.regenerate_ical_token()
        db.session.add(user)
        db.session.commit()
        return user.id, user.ical_token


# ── tests ─────────────────────────────────────────────────────────────────────

class TestICalFeed:
    def test_invalid_token_returns_404(self, client):
        resp = client.get("/calendar/deadbeef1234567890abcdef1234567890abcdef1234567890abcdef12345678.ics")
        assert resp.status_code == 404

    def test_valid_token_returns_ics(self, app, client):
        user_id, token = _make_member(app)
        resp = client.get(f"/calendar/{token}.ics")
        assert resp.status_code == 200
        assert "text/calendar" in resp.content_type
        assert b"BEGIN:VCALENDAR" in resp.data

    def test_active_assignment_appears_in_feed(self, app, client):
        user_id, token = _make_member(app, "ical_active@test.com")
        event_id, spot_id = _make_event(app, "Active Event", EventStatus.ASSIGNMENTS_OPEN)
        _assign_user(app, user_id, spot_id)

        resp = client.get(f"/calendar/{token}.ics")
        assert resp.status_code == 200
        assert b"Active Event" in resp.data

    def test_cancelled_event_excluded_from_feed(self, app, client):
        user_id, token = _make_member(app, "ical_cancelled@test.com")
        event_id, spot_id = _make_event(app, "Cancelled Event", EventStatus.CANCELLED)
        _assign_user(app, user_id, spot_id)

        resp = client.get(f"/calendar/{token}.ics")
        assert resp.status_code == 200
        assert b"Cancelled Event" not in resp.data

    def test_completed_event_excluded_from_feed(self, app, client):
        user_id, token = _make_member(app, "ical_completed@test.com")
        event_id, spot_id = _make_event(app, "Completed Event", EventStatus.COMPLETED)
        _assign_user(app, user_id, spot_id)

        resp = client.get(f"/calendar/{token}.ics")
        assert resp.status_code == 200
        assert b"Completed Event" not in resp.data

    def test_event_uid_is_stable(self, app, client):
        user_id, token = _make_member(app, "ical_uid@test.com")
        event_id, spot_id = _make_event(app, "UID Test Event", EventStatus.ASSIGNMENTS_OPEN)
        _assign_user(app, user_id, spot_id)

        resp = client.get(f"/calendar/{token}.ics")
        assert f"event-{event_id}@medcover".encode() in resp.data

    def test_location_included_when_set(self, app, client):
        user_id, token = _make_member(app, "ical_loc@test.com")
        event_id, spot_id = _make_event(app, "Location Event", EventStatus.ASSIGNMENTS_OPEN, address="Brno, náměstí Svobody")
        _assign_user(app, user_id, spot_id)

        resp = client.get(f"/calendar/{token}.ics")
        assert b"Brno" in resp.data

    def test_feed_empty_for_user_with_no_assignments(self, app, client):
        _, token = _make_member(app, "ical_empty@test.com")
        resp = client.get(f"/calendar/{token}.ics")
        assert resp.status_code == 200
        assert b"VEVENT" not in resp.data


class TestICalRegenerate:
    def test_regenerate_creates_new_token(self, app, member_client):
        with app.app_context():
            user = db.session.scalar(
                db.select(UserAccount).where(UserAccount.email == "member@test.com")
            )
            old_token = user.ical_token or user.regenerate_ical_token()
            db.session.commit()
            old_token = user.ical_token

        resp = member_client.post(
            "/calendar/regenerate",
            data={"csrf_token": "ignored"},
            follow_redirects=False,
        )
        # Should redirect to profile page
        assert resp.status_code == 302
        assert "/profile" in resp.headers["Location"]

        with app.app_context():
            user = db.session.scalar(
                db.select(UserAccount).where(UserAccount.email == "member@test.com")
            )
            assert user.ical_token != old_token
            assert user.ical_token is not None

    def test_old_token_returns_404_after_regenerate(self, app, member_client):
        with app.app_context():
            user = db.session.scalar(
                db.select(UserAccount).where(UserAccount.email == "member@test.com")
            )
            if not user.ical_token:
                user.regenerate_ical_token()
                db.session.commit()
            old_token = user.ical_token

        member_client.post(
            "/calendar/regenerate",
            data={"csrf_token": "ignored"},
            follow_redirects=False,
        )

        resp = member_client.get(f"/calendar/{old_token}.ics")
        assert resp.status_code == 404

    def test_regenerate_writes_audit_log(self, app, member_client):
        member_client.post(
            "/calendar/regenerate",
            data={"csrf_token": "ignored"},
            follow_redirects=False,
        )
        with app.app_context():
            user = db.session.scalar(
                db.select(UserAccount).where(UserAccount.email == "member@test.com")
            )
            entry = db.session.scalar(
                db.select(AuditLogEntry).where(
                    AuditLogEntry.entity_type == "UserAccount",
                    AuditLogEntry.action_type == "update",
                    AuditLogEntry.entity_id == str(user.id),
                )
            )
            assert entry is not None
            assert "iCal" in entry.summary

    def test_regenerate_requires_login(self, client):
        resp = client.post(
            "/calendar/regenerate",
            data={"csrf_token": "x"},
            follow_redirects=False,
        )
        # Unauthenticated → redirect to login
        assert resp.status_code == 302
        assert "login" in resp.headers["Location"]

    def test_profile_shows_ical_url(self, app, member_client):
        resp = member_client.get("/users/profile", follow_redirects=True)
        assert resp.status_code == 200
        assert b"ical" in resp.data.lower()
        assert b"calendar" in resp.data.lower()
