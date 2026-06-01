"""Tests for the iCal calendar feed (/calendar/<token>.ics)."""

from __future__ import annotations

from datetime import datetime, timezone

from app.extensions import db
from app.models.assignment import Assignment
from app.models.audit import AuditLogEntry
from app.models.event import Event, EventSpot, EventStatus
from app.models.master_event import MasterEvent
from app.models.user import UserAccount
from tests.conftest import _make_event_with_spot


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
        event_id, spot_id = _make_event_with_spot(app, status=EventStatus.ASSIGNMENTS_OPEN, name="Active Event")
        _assign_user(app, user_id, spot_id)

        resp = client.get(f"/calendar/{token}.ics")
        assert resp.status_code == 200
        assert b"Active Event" in resp.data

    def test_cancelled_event_excluded_from_feed(self, app, client):
        user_id, token = _make_member(app, "ical_cancelled@test.com")
        event_id, spot_id = _make_event_with_spot(app, status=EventStatus.CANCELLED, name="Cancelled Event")
        _assign_user(app, user_id, spot_id)

        resp = client.get(f"/calendar/{token}.ics")
        assert resp.status_code == 200
        assert b"Cancelled Event" not in resp.data

    def test_completed_event_excluded_from_feed(self, app, client):
        user_id, token = _make_member(app, "ical_completed@test.com")
        event_id, spot_id = _make_event_with_spot(app, status=EventStatus.COMPLETED, name="Completed Event")
        _assign_user(app, user_id, spot_id)

        resp = client.get(f"/calendar/{token}.ics")
        assert resp.status_code == 200
        assert b"Completed Event" not in resp.data

    def test_event_uid_is_stable(self, app, client):
        user_id, token = _make_member(app, "ical_uid@test.com")
        event_id, spot_id = _make_event_with_spot(app, status=EventStatus.ASSIGNMENTS_OPEN, name="UID Test Event")
        _assign_user(app, user_id, spot_id)

        resp = client.get(f"/calendar/{token}.ics")
        assert f"event-{event_id}@medcover".encode() in resp.data

    def test_location_included_when_set(self, app, client):
        user_id, token = _make_member(app, "ical_loc@test.com")
        event_id, spot_id = _make_event_with_spot(
            app, status=EventStatus.ASSIGNMENTS_OPEN, name="Location Event", address="Brno, náměstí Svobody"
        )
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
            user = db.session.scalar(db.select(UserAccount).where(UserAccount.email == "member@test.com"))
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
            user = db.session.scalar(db.select(UserAccount).where(UserAccount.email == "member@test.com"))
            assert user.ical_token != old_token
            assert user.ical_token is not None

    def test_old_token_returns_404_after_regenerate(self, app, member_client):
        with app.app_context():
            user = db.session.scalar(db.select(UserAccount).where(UserAccount.email == "member@test.com"))
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
            user = db.session.scalar(db.select(UserAccount).where(UserAccount.email == "member@test.com"))
            entry = db.session.scalar(
                db.select(AuditLogEntry).where(
                    AuditLogEntry.entity_type == "UserAccount",
                    AuditLogEntry.action_type == "edit",
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


def _make_member_with_all_token(app, email: str = "ical_all@test.com") -> tuple[int, str]:
    """Create an active member user with an ical_all_token; return (id, token)."""
    with app.app_context():
        from app.models.role import Role

        role = db.session.scalar(db.select(Role).where(Role.name == Role.MEMBER))
        user = UserAccount(email=email, name="iCal All Member", is_active=True)
        user.set_password("testpass123")
        user.roles = [role]
        db.session.add(user)
        db.session.commit()
        return user.id, user.ical_all_token


class TestICalFeedArchivedExclusion:
    """Personal feed should exclude archived events."""

    def test_archived_event_excluded_from_personal_feed(self, app, client):
        user_id, token = _make_member(app, "ical_archived@test.com")
        with app.app_context():
            me = MasterEvent(name="Archived ME")
            db.session.add(me)
            db.session.flush()

            from app.models.role import Role

            role = db.session.scalar(db.select(Role).where(Role.name == Role.ADMIN))
            creator = UserAccount(email="archived_creator@test.com", name="Creator", is_active=True)
            creator.set_password("x")
            creator.roles = [role]
            db.session.add(creator)
            db.session.flush()

            event = Event(
                name="Archived Active Event",
                master_event_id=me.id,
                status=EventStatus.ASSIGNMENTS_OPEN,
                archived=True,
                start_datetime=datetime(2030, 9, 1, 9, 0, tzinfo=timezone.utc),
                end_datetime=datetime(2030, 9, 1, 17, 0, tzinfo=timezone.utc),
                created_by_id=creator.id,
            )
            db.session.add(event)
            db.session.flush()
            spot = EventSpot(event_id=event.id)
            db.session.add(spot)
            db.session.flush()
            db.session.add(Assignment(user_id=user_id, spot_id=spot.id))
            db.session.commit()

        resp = client.get(f"/calendar/{token}.ics")
        assert resp.status_code == 200
        assert b"Archived Active Event" not in resp.data


class TestICalAllEventsFeed:
    """All-events feed tests."""

    def test_invalid_token_returns_404(self, client):
        resp = client.get("/calendar/all/deadbeef1234567890abcdef1234567890abcdef1234567890abcdef12345678.ics")
        assert resp.status_code == 404

    def test_valid_token_returns_ics(self, app, client):
        _, token = _make_member_with_all_token(app, "ical_all_basic@test.com")
        resp = client.get(f"/calendar/all/{token}.ics")
        assert resp.status_code == 200
        assert "text/calendar" in resp.content_type
        assert b"BEGIN:VCALENDAR" in resp.data

    def test_active_event_appears(self, app, client):
        _, token = _make_member_with_all_token(app, "ical_all_active@test.com")
        _make_event_with_spot(app, status=EventStatus.ASSIGNMENTS_OPEN, name="All Feed Active")

        resp = client.get(f"/calendar/all/{token}.ics")
        assert resp.status_code == 200
        assert b"All Feed Active" in resp.data

    def test_completed_event_appears(self, app, client):
        """Completed events should appear in the all-events feed."""
        _, token = _make_member_with_all_token(app, "ical_all_completed@test.com")
        _make_event_with_spot(app, status=EventStatus.COMPLETED, name="All Feed Completed")

        resp = client.get(f"/calendar/all/{token}.ics")
        assert resp.status_code == 200
        assert b"All Feed Completed" in resp.data

    def test_cancelled_event_excluded(self, app, client):
        _, token = _make_member_with_all_token(app, "ical_all_cancelled@test.com")
        _make_event_with_spot(app, status=EventStatus.CANCELLED, name="All Feed Cancelled")

        resp = client.get(f"/calendar/all/{token}.ics")
        assert resp.status_code == 200
        assert b"All Feed Cancelled" not in resp.data

    def test_archived_event_excluded(self, app, client):
        _, token = _make_member_with_all_token(app, "ical_all_archived@test.com")
        with app.app_context():
            me = MasterEvent(name="All Archived ME")
            db.session.add(me)
            db.session.flush()

            from app.models.role import Role

            role = db.session.scalar(db.select(Role).where(Role.name == Role.ADMIN))
            creator = UserAccount(email="all_archived_creator@test.com", name="Creator", is_active=True)
            creator.set_password("x")
            creator.roles = [role]
            db.session.add(creator)
            db.session.flush()

            event = Event(
                name="All Feed Archived",
                master_event_id=me.id,
                status=EventStatus.ASSIGNMENTS_OPEN,
                archived=True,
                start_datetime=datetime(2030, 10, 1, 9, 0, tzinfo=timezone.utc),
                end_datetime=datetime(2030, 10, 1, 17, 0, tzinfo=timezone.utc),
                created_by_id=creator.id,
            )
            db.session.add(event)
            db.session.commit()

        resp = client.get(f"/calendar/all/{token}.ics")
        assert resp.status_code == 200
        assert b"All Feed Archived" not in resp.data

    def test_description_contains_status_and_spots(self, app, client):
        _, token = _make_member_with_all_token(app, "ical_all_desc@test.com")
        _make_event_with_spot(app, status=EventStatus.ASSIGNMENTS_OPEN, name="Desc Test Event")

        resp = client.get(f"/calendar/all/{token}.ics")
        assert resp.status_code == 200
        data = resp.data.decode()
        assert "Stav:" in data
        assert "Pozice:" in data


class TestICalRegenerateAll:
    """Regenerate all-events token tests."""

    def test_regenerate_all_creates_new_token(self, app, member_client):
        with app.app_context():
            user = db.session.scalar(db.select(UserAccount).where(UserAccount.email == "member@test.com"))
            user.regenerate_ical_all_token()
            db.session.commit()
            old_token = user.ical_all_token

        resp = member_client.post(
            "/calendar/regenerate-all",
            data={"csrf_token": "ignored"},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert "/profile" in resp.headers["Location"]

        with app.app_context():
            user = db.session.scalar(db.select(UserAccount).where(UserAccount.email == "member@test.com"))
            assert user.ical_all_token != old_token
            assert user.ical_all_token is not None

    def test_regenerate_all_requires_login(self, client):
        resp = client.post(
            "/calendar/regenerate-all",
            data={"csrf_token": "x"},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert "login" in resp.headers["Location"]


# ── Archived user — feed access (issue #233) ──────────────────────────────────


class TestArchivedUserFeedAccess:
    """Archived users must not be able to access any iCal feed (issue #233)."""

    def test_archived_user_personal_feed_returns_404(self, app, client):
        """Archived user's personal iCal token should return 404."""
        _, token = _make_member(app, "ical_archived_user@test.com")
        with app.app_context():
            user = db.session.scalar(db.select(UserAccount).where(UserAccount.email == "ical_archived_user@test.com"))
            user.is_archived = True
            user.is_active = False
            db.session.commit()

        resp = client.get(f"/calendar/{token}.ics")
        assert resp.status_code == 404

    def test_archived_user_all_events_feed_returns_404(self, app, client):
        """Archived user's all-events iCal token should return 404."""
        _, token = _make_member_with_all_token(app, "ical_archived_user_all@test.com")
        with app.app_context():
            user = db.session.scalar(
                db.select(UserAccount).where(UserAccount.email == "ical_archived_user_all@test.com")
            )
            user.is_archived = True
            user.is_active = False
            db.session.commit()

        resp = client.get(f"/calendar/all/{token}.ics")
        assert resp.status_code == 404

    def test_archived_but_active_user_personal_feed_returns_404(self, app, client):
        """User archived without is_active=False (hypothetical) is still blocked."""
        _, token = _make_member(app, "ical_archived_active@test.com")
        with app.app_context():
            user = db.session.scalar(db.select(UserAccount).where(UserAccount.email == "ical_archived_active@test.com"))
            user.is_archived = True
            # Deliberately leave is_active=True to test the is_archived guard independently
            db.session.commit()

        resp = client.get(f"/calendar/{token}.ics")
        assert resp.status_code == 404
