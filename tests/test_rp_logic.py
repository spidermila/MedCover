"""Tests for RP (responsible person) auto-assign/clear logic and set_rp route."""
from __future__ import annotations

from datetime import datetime, timezone


from app.extensions import db
from app.models.assignment import Assignment
from app.models.event import Event, EventSpot, EventStatus
from app.models.master_event import MasterEvent
from app.models.qualification import Qualification
from app.models.role import Role
from app.models.user import UserAccount
from tests.conftest import _make_user, _login


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_rp_qual(app) -> int:
    with app.app_context():
        q = db.session.scalar(db.select(Qualification).where(Qualification.can_be_rp == True))  # noqa: E712
        if q:
            return q.id
        q = Qualification(name="TestZdravotnik", can_be_rp=True)
        db.session.add(q)
        db.session.commit()
        return q.id


def _make_non_rp_qual(app) -> int:
    with app.app_context():
        q = Qualification(name="TestRidic", can_be_rp=False)
        db.session.add(q)
        db.session.commit()
        return q.id


def _make_user_with_qual(app, email: str, qual_id: int) -> str:
    with app.app_context():
        qual = db.session.get(Qualification, qual_id)
        u = _make_user(email, "Test User", Role.MEMBER)
        u.qualifications = [qual]
        db.session.commit()
        return str(u.id)


def _make_open_event(app) -> tuple[int, int]:
    """Return (event_id, spot_id) for an ASSIGNMENTS_OPEN event."""
    with app.app_context():
        me = MasterEvent(name="RP Test ME")
        db.session.add(me)
        db.session.flush()
        event = Event(
            name="RP Test Event",
            master_event_id=me.id,
            status=EventStatus.ASSIGNMENTS_OPEN,
            start_datetime=datetime(2030, 6, 1, 10, 0, tzinfo=timezone.utc),
            end_datetime=datetime(2030, 6, 1, 18, 0, tzinfo=timezone.utc),
        )
        db.session.add(event)
        db.session.flush()
        spot = EventSpot(event_id=event.id)
        db.session.add(spot)
        db.session.commit()
        return event.id, spot.id


# ── is_rp_eligible ────────────────────────────────────────────────────────────

class TestIsRpEligible:
    def test_user_with_rp_qual_is_eligible(self, app):
        qual_id = _make_rp_qual(app)
        user_id = _make_user_with_qual(app, "rp_eligible@test.com", qual_id)
        with app.app_context():
            user = db.session.get(UserAccount, user_id)
            assert user.is_rp_eligible() is True

    def test_user_without_rp_qual_is_not_eligible(self, app):
        qual_id = _make_non_rp_qual(app)
        user_id = _make_user_with_qual(app, "not_rp@test.com", qual_id)
        with app.app_context():
            user = db.session.get(UserAccount, user_id)
            assert user.is_rp_eligible() is False

    def test_user_with_no_quals_is_not_eligible(self, app):
        with app.app_context():
            role = db.session.scalar(db.select(Role).where(Role.name == Role.MEMBER))
            u = UserAccount(email="noqual@test.com", name="No Qual", is_active=True)
            u.set_password("testpass123")
            u.roles = [role]
            db.session.add(u)
            db.session.commit()
            assert u.is_rp_eligible() is False


# ── Auto-assign RP on claim ───────────────────────────────────────────────────

class TestAutoAssignRpOnClaim:
    def test_first_eligible_claimant_becomes_rp(self, app):
        qual_id = _make_rp_qual(app)
        user_id = _make_user_with_qual(app, "claimer_rp@test.com", qual_id)
        event_id, spot_id = _make_open_event(app)

        client = app.test_client()
        _login(client, "claimer_rp@test.com")
        client.post(f"/assignments/claim/{spot_id}", follow_redirects=True)

        with app.app_context():
            event = db.session.get(Event, event_id)
            assert str(event.responsible_person_id) == user_id

    def test_non_eligible_claimant_does_not_become_rp(self, app):
        qual_id = _make_non_rp_qual(app)
        _make_user_with_qual(app, "claimer_nonrp@test.com", qual_id)
        event_id, spot_id = _make_open_event(app)

        client = app.test_client()
        _login(client, "claimer_nonrp@test.com")
        client.post(f"/assignments/claim/{spot_id}", follow_redirects=True)

        with app.app_context():
            event = db.session.get(Event, event_id)
            assert event.responsible_person_id is None

    def test_second_eligible_claimant_does_not_override_rp(self, app):
        """If event already has an RP, a second eligible joiner does not replace them."""
        qual_id = _make_rp_qual(app)
        user1_id = _make_user_with_qual(app, "claimer_rp1@test.com", qual_id)
        _make_user_with_qual(app, "claimer_rp2@test.com", qual_id)
        event_id, _ = _make_open_event(app)

        # Add second spot
        with app.app_context():
            spot2 = EventSpot(event_id=event_id)
            db.session.add(spot2)
            db.session.commit()
            spot2_id = spot2.id

        # First user claims spot 1 (already _make_open_event returns spot_id)
        _, spot1_id = event_id, _
        with app.app_context():
            spots = db.session.scalars(db.select(EventSpot).where(EventSpot.event_id == event_id)).all()
            spot1_id = spots[0].id
            spot2_id = spots[1].id

        client1 = app.test_client()
        _login(client1, "claimer_rp1@test.com")
        client1.post(f"/assignments/claim/{spot1_id}", follow_redirects=True)

        client2 = app.test_client()
        _login(client2, "claimer_rp2@test.com")
        client2.post(f"/assignments/claim/{spot2_id}", follow_redirects=True)

        with app.app_context():
            event = db.session.get(Event, event_id)
            assert str(event.responsible_person_id) == user1_id


# ── Auto-clear RP on release ──────────────────────────────────────────────────

class TestAutoClearRpOnRelease:
    def test_rp_cleared_when_rp_releases(self, app):
        qual_id = _make_rp_qual(app)
        user_id = _make_user_with_qual(app, "rp_release@test.com", qual_id)
        event_id, spot_id = _make_open_event(app)

        # Assign user and set them as RP
        with app.app_context():
            assignment = Assignment(spot_id=spot_id, user_id=user_id, assigned_by_id=user_id)
            db.session.add(assignment)
            event = db.session.get(Event, event_id)
            event.responsible_person_id = user_id
            db.session.commit()
            assignment_id = assignment.id

        client = app.test_client()
        _login(client, "rp_release@test.com")
        client.post(f"/assignments/release/{assignment_id}", follow_redirects=True)

        with app.app_context():
            event = db.session.get(Event, event_id)
            assert event.responsible_person_id is None

    def test_rp_not_cleared_when_non_rp_releases(self, app):
        """Releasing a non-RP user should not affect responsible_person_id."""
        rp_qual_id = _make_rp_qual(app)
        non_rp_qual_id = _make_non_rp_qual(app)
        rp_user_id = _make_user_with_qual(app, "rp_stays@test.com", rp_qual_id)
        _make_user_with_qual(app, "nonrp_leaves@test.com", non_rp_qual_id)
        event_id, spot_id = _make_open_event(app)

        with app.app_context():
            # Add second spot for non-rp user
            spot2 = EventSpot(event_id=event_id)
            db.session.add(spot2)
            db.session.flush()
            nonrp_user = db.session.scalar(
                db.select(UserAccount).where(UserAccount.email == "nonrp_leaves@test.com")
            )
            assignment2 = Assignment(spot_id=spot2.id, user_id=nonrp_user.id, assigned_by_id=nonrp_user.id)
            db.session.add(assignment2)
            event = db.session.get(Event, event_id)
            event.responsible_person_id = rp_user_id
            db.session.commit()
            assignment2_id = assignment2.id

        client = app.test_client()
        _login(client, "nonrp_leaves@test.com")
        client.post(f"/assignments/release/{assignment2_id}", follow_redirects=True)

        with app.app_context():
            event = db.session.get(Event, event_id)
            assert str(event.responsible_person_id) == rp_user_id


# ── set_rp route ──────────────────────────────────────────────────────────────

class TestSetRpRoute:
    def _setup(self, app) -> tuple[int, str, str]:
        """Returns (event_id, rp_eligible_user_id, non_eligible_user_id)."""
        rp_qual_id = _make_rp_qual(app)
        non_rp_qual_id = _make_non_rp_qual(app)
        rp_user_id = _make_user_with_qual(app, "set_rp_eligible@test.com", rp_qual_id)
        non_rp_id = _make_user_with_qual(app, "set_rp_noneligible@test.com", non_rp_qual_id)
        event_id, spot_id = _make_open_event(app)
        # Assign rp user to spot
        with app.app_context():
            assignment = Assignment(spot_id=spot_id, user_id=rp_user_id, assigned_by_id=rp_user_id)
            db.session.add(assignment)
            db.session.commit()
        return event_id, rp_user_id, non_rp_id

    def test_member_cannot_set_rp(self, app, member_client):
        event_id, rp_user_id, _ = self._setup(app)
        response = member_client.post(
            f"/events/{event_id}/set_rp", data={"user_id": rp_user_id}
        )
        assert response.status_code == 403

    def test_admin_can_set_rp(self, app, admin_client):
        event_id, rp_user_id, _ = self._setup(app)
        response = admin_client.post(
            f"/events/{event_id}/set_rp",
            data={"user_id": rp_user_id},
            follow_redirects=False,
        )
        assert response.status_code == 302
        with app.app_context():
            event = db.session.get(Event, event_id)
            assert str(event.responsible_person_id) == rp_user_id

    def test_non_eligible_user_rejected(self, app, admin_client):
        event_id, _, non_rp_id = self._setup(app)
        response = admin_client.post(
            f"/events/{event_id}/set_rp",
            data={"user_id": non_rp_id},
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "kvalifikaci" in response.data.decode()

    def test_user_not_on_event_rejected(self, app, admin_client):
        qual_id = _make_rp_qual(app)
        outsider_id = _make_user_with_qual(app, "outsider_rp@test.com", qual_id)
        event_id, _, _ = self._setup(app)
        response = admin_client.post(
            f"/events/{event_id}/set_rp",
            data={"user_id": outsider_id},
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "pozici" in response.data.decode()

    def test_set_rp_404_for_missing_event(self, app, admin_client):
        qual_id = _make_rp_qual(app)
        user_id = _make_user_with_qual(app, "rp_404@test.com", qual_id)
        response = admin_client.post(
            "/events/999999/set_rp", data={"user_id": user_id}
        )
        assert response.status_code == 404


# ── Dashboard RP warning ──────────────────────────────────────────────────────

class TestDashboardRpWarning:
    def _make_event_soon_no_rp(self, app) -> int:
        from datetime import timedelta
        with app.app_context():
            me = MasterEvent(name="Dashboard RP ME")
            db.session.add(me)
            db.session.flush()
            now = datetime.now(timezone.utc)
            event = Event(
                name="Soon No RP",
                master_event_id=me.id,
                status=EventStatus.PUBLISHED,
                start_datetime=now + timedelta(days=3),
                end_datetime=now + timedelta(days=3, hours=8),
                responsible_person_id=None,
            )
            db.session.add(event)
            db.session.commit()
            return event.id

    def test_admin_sees_missing_rp_warning(self, app, admin_client):
        self._make_event_soon_no_rp(app)
        response = admin_client.get("/dashboard")
        assert response.status_code == 200
        assert "bez zodpovědné osoby" in response.data.decode()

    def test_member_does_not_see_rp_warning(self, app, member_client):
        self._make_event_soon_no_rp(app)
        response = member_client.get("/dashboard")
        assert response.status_code == 200
        assert "bez zodpovědné osoby" not in response.data.decode()
