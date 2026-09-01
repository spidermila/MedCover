"""Tests for spot assignment: claim, release, permissions."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Barrier

import pytest
from sqlalchemy import event as sa_event

from app.extensions import db
from app.models.assignment import Assignment
from app.models.equipment import EquipmentItem, EquipmentType
from app.models.event import Event, EventSpot, EventStatus
from app.models.master_event import MasterEvent
from app.models.qualification import Qualification
from app.models.role import Role
from app.models.user import UserAccount
from app.routes.assignments import do_assign_user
from tests.conftest import _login, _make_event_with_spot, _make_user


class TestAssignmentClaim:
    def test_member_can_claim_open_spot(self, app, member_client):
        event_id, spot_id = _make_event_with_spot(app)
        response = member_client.post(f"/assignments/claim/{spot_id}", follow_redirects=False)
        assert response.status_code == 302
        with app.app_context():
            assignment = db.session.scalar(db.select(Assignment).where(Assignment.spot_id == spot_id))
            assert assignment is not None
            # Verify the correct user is stored
            member = db.session.scalar(db.select(UserAccount).where(UserAccount.email == "member@test.com"))
            assert assignment.user_id == member.id

    def test_claim_already_taken_spot_is_rejected(self, app, member_client):
        event_id, spot_id = _make_event_with_spot(app)
        # First claim
        member_client.post(f"/assignments/claim/{spot_id}", follow_redirects=True)
        # Second claim (same user, same spot — spot is now taken)
        response = member_client.post(f"/assignments/claim/{spot_id}", follow_redirects=True)
        assert response.status_code == 200  # Back to detail, with flash
        with app.app_context():
            count = db.session.scalar(
                db.select(db.func.count()).select_from(Assignment).where(Assignment.spot_id == spot_id)
            )
            assert count == 1  # Only one assignment, not two

    def test_claim_requires_login(self, app, client):
        _, spot_id = _make_event_with_spot(app)
        response = client.post(f"/assignments/claim/{spot_id}", follow_redirects=False)
        assert response.status_code == 302
        assert "login" in response.headers["Location"]

    def test_cannot_claim_spot_in_draft_event(self, app, member_client):
        """Spot in a DRAFT event should not be claimable."""
        with app.app_context():
            me = MasterEvent(name="Test ME")
            db.session.add(me)
            db.session.flush()

            role = db.session.scalar(db.select(Role).where(Role.name == Role.ADMIN))
            creator = UserAccount(email="creator2@test.com", name="Creator2", is_active=True)
            creator.set_password("testpass123")
            creator.roles = [role]
            db.session.add(creator)
            db.session.flush()

            event = Event(
                name="Draft Event",
                master_event_id=me.id,
                start_datetime=datetime(2030, 6, 1, 10, 0, tzinfo=timezone.utc),
                end_datetime=datetime(2030, 6, 1, 18, 0, tzinfo=timezone.utc),
                status=EventStatus.DRAFT,
                created_by_id=creator.id,
            )
            db.session.add(event)
            db.session.flush()

            spot = EventSpot(event_id=event.id)
            db.session.add(spot)
            db.session.commit()
            spot_id = spot.id

        member_client.post(f"/assignments/claim/{spot_id}", follow_redirects=True)
        with app.app_context():
            count = db.session.scalar(
                db.select(db.func.count()).select_from(Assignment).where(Assignment.spot_id == spot_id)
            )
            assert count == 0


class TestAssignmentRelease:
    def test_member_can_release_own_assignment(self, app, member_client):
        event_id, spot_id = _make_event_with_spot(app)
        member_client.post(f"/assignments/claim/{spot_id}", follow_redirects=True)
        with app.app_context():
            assignment = db.session.scalar(db.select(Assignment).where(Assignment.spot_id == spot_id))
            assignment_id = assignment.id

        response = member_client.post(f"/assignments/release/{assignment_id}", follow_redirects=False)
        assert response.status_code == 302
        with app.app_context():
            remaining = db.session.get(Assignment, assignment_id)
            assert remaining is None


class TestAdminAssignment:
    def test_admin_can_assign_other_user(self, app, admin_client):
        event_id, spot_id = _make_event_with_spot(app)
        with app.app_context():
            _make_user("target@test.com", "Target", Role.MEMBER)
            target = db.session.scalar(db.select(UserAccount).where(UserAccount.email == "target@test.com"))
            target_id = str(target.id)

        response = admin_client.post(
            f"/assignments/assign/{spot_id}",
            data={"user_id": target_id},
            follow_redirects=False,
        )
        assert response.status_code == 302
        with app.app_context():
            assignment = db.session.scalar(db.select(Assignment).where(Assignment.spot_id == spot_id))
            assert assignment is not None

    def test_member_cannot_assign_others(self, app, member_client):
        event_id, spot_id = _make_event_with_spot(app)
        with app.app_context():
            _make_user("target@test.com", "Target", Role.MEMBER)
            target = db.session.scalar(db.select(UserAccount).where(UserAccount.email == "target@test.com"))
            target_id = str(target.id)

        response = member_client.post(
            f"/assignments/assign/{spot_id}",
            data={"user_id": target_id},
            follow_redirects=False,
        )
        assert response.status_code == 403

    def test_second_user_cannot_claim_taken_spot(self, app, member_client):
        """A different user should not be able to claim a spot already taken by another user."""
        event_id, spot_id = _make_event_with_spot(app)

        # First member claims the spot
        member_client.post(f"/assignments/claim/{spot_id}", follow_redirects=True)

        # Second member (fresh client — separate session) tries to claim the same spot
        with app.app_context():
            _make_user("member2@test.com", "Second Member", Role.MEMBER)

        second_client = app.test_client()
        _login(second_client, "member2@test.com")
        response = second_client.post(f"/assignments/claim/{spot_id}", follow_redirects=True)

        assert response.status_code == 200
        with app.app_context():
            count = db.session.scalar(
                db.select(db.func.count()).select_from(Assignment).where(Assignment.spot_id == spot_id)
            )
            assert count == 1  # Still only one assignment


class TestAssignmentReleaseOwnership:
    def test_member_cannot_release_others_assignment(self, app, member_client):
        """A member must not be able to release another user's assignment."""
        event_id, spot_id = _make_event_with_spot(app)

        # First member claims the spot
        member_client.post(f"/assignments/claim/{spot_id}", follow_redirects=True)
        with app.app_context():
            assignment = db.session.scalar(db.select(Assignment).where(Assignment.spot_id == spot_id))
            assignment_id = assignment.id

        # Second member (fresh client — separate session) tries to release it
        with app.app_context():
            _make_user("member2@test.com", "Second Member", Role.MEMBER)

        second_client = app.test_client()
        _login(second_client, "member2@test.com")
        response = second_client.post(f"/assignments/release/{assignment_id}", follow_redirects=False)

        assert response.status_code == 403
        with app.app_context():
            remaining = db.session.get(Assignment, assignment_id)
            assert remaining is not None  # Assignment must still exist


class TestAdminUnassign:
    def test_admin_can_unassign_user(self, app, admin_client):
        event_id, spot_id = _make_event_with_spot(app)
        # Create a member and have them claim the spot via a fresh client
        with app.app_context():
            _make_user("claimer@test.com", "Claimer", Role.MEMBER)

        claimer = app.test_client()
        _login(claimer, "claimer@test.com")
        claimer.post(f"/assignments/claim/{spot_id}", follow_redirects=True)

        with app.app_context():
            assignment = db.session.scalar(db.select(Assignment).where(Assignment.spot_id == spot_id))
            assignment_id = assignment.id

        response = admin_client.post(f"/assignments/unassign/{assignment_id}", follow_redirects=False)
        assert response.status_code == 302
        with app.app_context():
            remaining = db.session.get(Assignment, assignment_id)
            assert remaining is None

    def test_member_cannot_unassign_others(self, app, admin_client):
        event_id, spot_id = _make_event_with_spot(app)
        # Admin assigns target@test.com
        with app.app_context():
            _make_user("target@test.com", "Target", Role.MEMBER)
            target = db.session.scalar(db.select(UserAccount).where(UserAccount.email == "target@test.com"))
            target_id = str(target.id)

        admin_client.post(
            f"/assignments/assign/{spot_id}",
            data={"user_id": target_id},
            follow_redirects=True,
        )
        with app.app_context():
            assignment = db.session.scalar(db.select(Assignment).where(Assignment.spot_id == spot_id))
            assert assignment is not None
            assignment_id = assignment.id

        # A plain member (not target, not admin) tries to unassign via fresh client
        with app.app_context():
            _make_user("attacker@test.com", "Attacker", Role.MEMBER)

        attacker = app.test_client()
        _login(attacker, "attacker@test.com")
        response = attacker.post(f"/assignments/unassign/{assignment_id}")
        assert response.status_code == 403

    def test_unassign_nonexistent_returns_404(self, admin_client):
        response = admin_client.post("/assignments/unassign/999999")
        assert response.status_code == 404


# ── Claim edge cases ──────────────────────────────────────────────────────────


class TestClaimEdgeCases:
    def test_viewer_cannot_claim(self, app, viewer_client):
        """A Viewer user (no event.assign_own) gets 403."""

        _, spot_id = _make_event_with_spot(app)
        response = viewer_client.post(f"/assignments/claim/{spot_id}")
        assert response.status_code == 403

    def test_claim_nonexistent_spot_returns_404(self, app, member_client):
        response = member_client.post("/assignments/claim/999999")
        assert response.status_code == 404

    def test_claim_on_closed_event_flashes_warning(self, app, member_client):
        """Claiming a spot on a non-ASSIGNMENTS_OPEN event shows a flash message."""
        with app.app_context():
            me = MasterEvent(name="Closed ME")
            db.session.add(me)
            db.session.flush()
            role = db.session.scalar(db.select(Role).where(Role.name == Role.ADMIN))
            creator = UserAccount(email="closedcreator@test.com", name="C", is_active=True)
            creator.set_password("testpass123")
            creator.roles = [role]
            db.session.add(creator)
            db.session.flush()

            event = Event(
                name="Published Event",
                master_event_id=me.id,
                start_datetime=datetime(2030, 6, 1, 10, 0, tzinfo=timezone.utc),
                end_datetime=datetime(2030, 6, 1, 18, 0, tzinfo=timezone.utc),
                status=EventStatus.PUBLISHED,
                created_by_id=creator.id,
            )
            db.session.add(event)
            db.session.flush()
            spot = EventSpot(event_id=event.id)
            db.session.add(spot)
            db.session.commit()
            spot_id = spot.id

        response = member_client.post(f"/assignments/claim/{spot_id}", follow_redirects=True)
        assert response.status_code == 200
        assert "otevřeno" in response.data.decode() or "není" in response.data.decode()
        with app.app_context():
            assert (
                db.session.scalar(
                    db.select(db.func.count()).select_from(Assignment).where(Assignment.spot_id == spot_id)
                )
                == 0
            )

    def test_claim_when_already_assigned_to_event_flashes(self, app, member_client):
        """User already has a spot on this event — second claim rejected."""
        event_id, spot_id = _make_event_with_spot(app)
        # Add a second spot to the same event
        with app.app_context():
            spot2 = EventSpot(event_id=event_id)
            db.session.add(spot2)
            db.session.commit()
            spot2_id = spot2.id

        # Claim the first spot
        member_client.post(f"/assignments/claim/{spot_id}", follow_redirects=True)

        response = member_client.post(f"/assignments/claim/{spot2_id}", follow_redirects=True)
        assert response.status_code == 200
        assert "přihlášeni" in response.data.decode()


# ── Release edge cases ────────────────────────────────────────────────────────


class TestReleaseEdgeCases:
    def test_release_nonexistent_assignment_returns_404(self, app, member_client):
        response = member_client.post("/assignments/release/999999")
        assert response.status_code == 404

    def test_release_from_completed_event_flashes_warning(self, app, member_client):
        """Cannot release assignment from a COMPLETED event."""
        event_id, spot_id = _make_event_with_spot(app)
        member_client.post(f"/assignments/claim/{spot_id}", follow_redirects=True)
        with app.app_context():
            event = db.session.get(Event, event_id)
            event.status = EventStatus.COMPLETED
            db.session.commit()
            assignment = db.session.scalar(db.select(Assignment).where(Assignment.spot_id == spot_id))
            assignment_id = assignment.id

        response = member_client.post(f"/assignments/release/{assignment_id}", follow_redirects=True)
        assert response.status_code == 200
        assert "dokončen" in response.data.decode() or "nelze" in response.data.decode()

    def test_release_reopens_assignments_closed_event(self, app, member_client):
        """Releasing a spot from a CLOSED event re-opens assignments."""
        event_id, spot_id = _make_event_with_spot(app)
        member_client.post(f"/assignments/claim/{spot_id}", follow_redirects=True)
        with app.app_context():
            event = db.session.get(Event, event_id)
            event.status = EventStatus.ASSIGNMENTS_CLOSED
            db.session.commit()
            assignment = db.session.scalar(db.select(Assignment).where(Assignment.spot_id == spot_id))
            assignment_id = assignment.id

        member_client.post(f"/assignments/release/{assignment_id}", follow_redirects=True)
        with app.app_context():
            event = db.session.get(Event, event_id)
            assert event.status == EventStatus.ASSIGNMENTS_OPEN


# ── Auto-close transitions with optional spots ────────────────────────────────


def _make_event_with_mand_and_optional(app) -> tuple[int, int, int]:
    """Create an ASSIGNMENTS_OPEN event with one mandatory + one optional spot.

    Returns (event_id, mandatory_spot_id, optional_spot_id).
    """
    with app.app_context():
        me = MasterEvent(name="ME for mand+opt")
        db.session.add(me)
        db.session.flush()
        event = Event(
            name="Mand+Opt Event",
            master_event_id=me.id,
            status=EventStatus.ASSIGNMENTS_OPEN,
            start_datetime=datetime(2030, 6, 1, 10, 0, tzinfo=timezone.utc),
            end_datetime=datetime(2030, 6, 1, 18, 0, tzinfo=timezone.utc),
        )
        db.session.add(event)
        db.session.flush()
        mandatory = EventSpot(event_id=event.id, is_optional=False)
        optional = EventSpot(event_id=event.id, is_optional=True)
        db.session.add_all([mandatory, optional])
        db.session.commit()
        return event.id, mandatory.id, optional.id


class TestAutoCloseOptionalSpots:
    """Assignments must stay open while any spot — mandatory or optional — is free."""

    def test_claim_only_mandatory_keeps_open(self, app, member_client):
        """Filling the last mandatory spot must not close the event while an optional spot is free."""
        event_id, mand_spot_id, _opt_spot_id = _make_event_with_mand_and_optional(app)
        member_client.post(f"/assignments/claim/{mand_spot_id}", follow_redirects=True)
        with app.app_context():
            event = db.session.get(Event, event_id)
            assert event.status == EventStatus.ASSIGNMENTS_OPEN

    def test_optional_spot_claimable_after_mandatory_full(self, app, member_client):
        """After all mandatory are taken, a second member can still claim the optional spot."""
        event_id, mand_spot_id, opt_spot_id = _make_event_with_mand_and_optional(app)
        member_client.post(f"/assignments/claim/{mand_spot_id}", follow_redirects=True)

        with app.app_context():
            _make_user("opt_claimer@test.com", "Optional Claimer", Role.MEMBER)
        second = app.test_client()
        _login(second, "opt_claimer@test.com")
        second.post(f"/assignments/claim/{opt_spot_id}", follow_redirects=True)

        with app.app_context():
            assignment = db.session.scalar(db.select(Assignment).where(Assignment.spot_id == opt_spot_id))
            assert assignment is not None
            event = db.session.get(Event, event_id)
            assert event.status == EventStatus.ASSIGNMENTS_CLOSED

    def test_release_optional_reopens_event(self, app, member_client):
        """Releasing an optional spot from a fully-staffed event re-opens assignments."""
        event_id, mand_spot_id, opt_spot_id = _make_event_with_mand_and_optional(app)
        member_client.post(f"/assignments/claim/{mand_spot_id}", follow_redirects=True)
        with app.app_context():
            _make_user("opt3@test.com", "Opt 3", Role.MEMBER)
        second = app.test_client()
        _login(second, "opt3@test.com")
        second.post(f"/assignments/claim/{opt_spot_id}", follow_redirects=True)

        with app.app_context():
            event = db.session.get(Event, event_id)
            assert event.status == EventStatus.ASSIGNMENTS_CLOSED
            opt_assignment = db.session.scalar(db.select(Assignment).where(Assignment.spot_id == opt_spot_id))
            opt_assignment_id = opt_assignment.id

        second.post(f"/assignments/release/{opt_assignment_id}", follow_redirects=True)
        with app.app_context():
            event = db.session.get(Event, event_id)
            assert event.status == EventStatus.ASSIGNMENTS_OPEN

    def test_no_optional_spots_closes_when_mandatory_full(self, app, member_client):
        """Regression guard: events without optional spots still close on mandatory-full."""
        event_id, spot_id = _make_event_with_spot(app)
        member_client.post(f"/assignments/claim/{spot_id}", follow_redirects=True)
        with app.app_context():
            event = db.session.get(Event, event_id)
            assert event.status == EventStatus.ASSIGNMENTS_CLOSED

    def test_multi_optional_closes_only_when_all_filled(self, app):
        """Event with 2 mandatory + 2 optional stays OPEN until the very last spot fills."""
        with app.app_context():
            me = MasterEvent(name="Multi ME")
            db.session.add(me)
            db.session.flush()
            event = Event(
                name="Multi Event",
                master_event_id=me.id,
                status=EventStatus.ASSIGNMENTS_OPEN,
                start_datetime=datetime(2030, 6, 1, 10, 0, tzinfo=timezone.utc),
                end_datetime=datetime(2030, 6, 1, 18, 0, tzinfo=timezone.utc),
            )
            db.session.add(event)
            db.session.flush()
            mand1 = EventSpot(event_id=event.id, is_optional=False)
            mand2 = EventSpot(event_id=event.id, is_optional=False)
            opt1 = EventSpot(event_id=event.id, is_optional=True)
            opt2 = EventSpot(event_id=event.id, is_optional=True)
            db.session.add_all([mand1, mand2, opt1, opt2])
            db.session.commit()
            event_id = event.id
            spot_ids = [mand1.id, mand2.id, opt1.id, opt2.id]

        # Four separate members claim in order; event must stay OPEN until the last claim.
        for i, spot_id in enumerate(spot_ids):
            with app.app_context():
                _make_user(f"multi_{i}@test.com", f"Multi {i}", Role.MEMBER)
            c = app.test_client()
            _login(c, f"multi_{i}@test.com")
            c.post(f"/assignments/claim/{spot_id}", follow_redirects=True)
            with app.app_context():
                event = db.session.get(Event, event_id)
                expected = EventStatus.ASSIGNMENTS_CLOSED if i == 3 else EventStatus.ASSIGNMENTS_OPEN
                assert event.status == expected, f"after claim #{i + 1}: {event.status}"


# ── Assign-other edge cases ───────────────────────────────────────────────────


class TestAssignOtherEdgeCases:
    def test_assign_without_user_id_flashes(self, app, admin_client):
        _, spot_id = _make_event_with_spot(app)
        response = admin_client.post(f"/assignments/assign/{spot_id}", data={}, follow_redirects=True)
        assert response.status_code == 200
        assert "Vyberte" in response.data.decode() or "uživatele" in response.data.decode()

    def test_assign_inactive_user_flashes(self, app, admin_client):
        _, spot_id = _make_event_with_spot(app)
        with app.app_context():
            target = _make_user("inactive@test.com", "Inactive", Role.MEMBER)
            target.is_active = False
            db.session.commit()
            target_id = str(target.id)
        response = admin_client.post(
            f"/assignments/assign/{spot_id}",
            data={"user_id": target_id},
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "nenalezen" in response.data.decode() or "aktivní" in response.data.decode()

    def test_assign_to_nonexistent_spot_returns_404(self, app, admin_client):
        with app.app_context():
            target = _make_user("t@test.com", "Target", Role.MEMBER)
            target_id = str(target.id)
        response = admin_client.post("/assignments/assign/999999", data={"user_id": target_id})
        assert response.status_code == 404

    def test_assign_on_wrong_event_status_flashes(self, app, admin_client):
        """Assigning to a DRAFT event is not allowed."""
        with app.app_context():
            me = MasterEvent(name="Draft ME2")
            db.session.add(me)
            db.session.flush()
            role = db.session.scalar(db.select(Role).where(Role.name == Role.ADMIN))
            creator = UserAccount(email="creator3@test.com", name="C3", is_active=True)
            creator.set_password("testpass123")
            creator.roles = [role]
            db.session.add(creator)
            db.session.flush()

            event = Event(
                name="Draft Ev",
                master_event_id=me.id,
                start_datetime=datetime(2030, 6, 1, 10, 0, tzinfo=timezone.utc),
                end_datetime=datetime(2030, 6, 1, 18, 0, tzinfo=timezone.utc),
                status=EventStatus.DRAFT,
                created_by_id=creator.id,
            )
            db.session.add(event)
            db.session.flush()
            spot = EventSpot(event_id=event.id)
            db.session.add(spot)
            target = _make_user("t2@test.com", "T2", Role.MEMBER)
            db.session.commit()
            spot_id = spot.id
            target_id = str(target.id)

        response = admin_client.post(
            f"/assignments/assign/{spot_id}",
            data={"user_id": target_id},
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "možné" in response.data.decode() or "stav" in response.data.decode()

    def test_assign_taken_spot_flashes(self, app, admin_client):
        """Assigning to an already occupied spot should flash a warning."""
        event_id, spot_id = _make_event_with_spot(app)
        with app.app_context():
            t1 = _make_user("ta@test.com", "TA", Role.MEMBER)
            t2 = _make_user("tb@test.com", "TB", Role.MEMBER)
            t1_id, t2_id = str(t1.id), str(t2.id)

        admin_client.post(f"/assignments/assign/{spot_id}", data={"user_id": t1_id}, follow_redirects=True)
        response = admin_client.post(f"/assignments/assign/{spot_id}", data={"user_id": t2_id}, follow_redirects=True)
        assert response.status_code == 200
        assert "obsazena" in response.data.decode()

    def test_assign_same_user_twice_flashes(self, app, admin_client):
        """Assigning the same user to a second spot on the same event should flash."""
        event_id, spot_id = _make_event_with_spot(app)
        with app.app_context():
            target = _make_user("tc@test.com", "TC", Role.MEMBER)
            target_id = str(target.id)
            spot2 = EventSpot(event_id=event_id)
            db.session.add(spot2)
            db.session.commit()
            spot2_id = spot2.id

        admin_client.post(f"/assignments/assign/{spot_id}", data={"user_id": target_id}, follow_redirects=True)
        response = admin_client.post(
            f"/assignments/assign/{spot2_id}", data={"user_id": target_id}, follow_redirects=True
        )
        assert response.status_code == 200
        assert "již přihlášen" in response.data.decode()


# ── Unassign-other edge cases ─────────────────────────────────────────────────


class TestUnassignOtherEdgeCases:
    def test_unassign_completed_event_flashes(self, app, admin_client):
        """Cannot unassign from a COMPLETED event."""
        event_id, spot_id = _make_event_with_spot(app)
        with app.app_context():
            target = _make_user("td@test.com", "TD", Role.MEMBER)
            target_id = str(target.id)
        admin_client.post(f"/assignments/assign/{spot_id}", data={"user_id": target_id}, follow_redirects=True)
        with app.app_context():
            db.session.get(Event, event_id).status = EventStatus.COMPLETED
            db.session.commit()
            assignment = db.session.scalar(db.select(Assignment).where(Assignment.spot_id == spot_id))
            assignment_id = assignment.id

        response = admin_client.post(f"/assignments/unassign/{assignment_id}", follow_redirects=True)
        assert response.status_code == 200
        assert "dokončen" in response.data.decode() or "nelze" in response.data.decode()

    def test_unassign_reopens_assignments_closed_event(self, app, admin_client):
        """Unassigning from a CLOSED event should re-open assignments."""
        event_id, spot_id = _make_event_with_spot(app)
        with app.app_context():
            target = _make_user("te@test.com", "TE", Role.MEMBER)
            target_id = str(target.id)
        admin_client.post(f"/assignments/assign/{spot_id}", data={"user_id": target_id}, follow_redirects=True)
        with app.app_context():
            db.session.get(Event, event_id).status = EventStatus.ASSIGNMENTS_CLOSED
            db.session.commit()
            assignment_id = db.session.scalar(db.select(Assignment).where(Assignment.spot_id == spot_id)).id

        admin_client.post(f"/assignments/unassign/{assignment_id}", follow_redirects=True)
        with app.app_context():
            assert db.session.get(Event, event_id).status == EventStatus.ASSIGNMENTS_OPEN


# ── do_assign_user error branches ───────────────────────────────────────────────────────────────


class TestAssignErrorBranches:
    """Coverage for guard clauses inside do_assign_user / do_unassign_user."""

    def test_cannot_assign_to_archived_event(self, app, admin_client):
        event_id, spot_id = _make_event_with_spot(app)
        with app.app_context():
            db.session.get(Event, event_id).archived = True
            db.session.commit()
            target = _make_user("arch_target@test.com", "Arch", Role.MEMBER)
            target_id = str(target.id)

        resp = admin_client.post(
            f"/assignments/assign/{spot_id}",
            data={"user_id": target_id},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert "archivov" in resp.data.decode()
        with app.app_context():
            assert db.session.scalar(db.select(Assignment).where(Assignment.spot_id == spot_id)) is None

    def test_claim_missing_qualification_flashes(self, app):
        """Self-claim triggers eligibility check; user without the required qualification is rejected."""
        with app.app_context():
            me = MasterEvent(name="Qual ME")
            db.session.add(me)
            db.session.flush()
            event = Event(
                name="Qual Event",
                master_event_id=me.id,
                status=EventStatus.ASSIGNMENTS_OPEN,
                start_datetime=datetime(2030, 6, 1, 10, 0, tzinfo=timezone.utc),
                end_datetime=datetime(2030, 6, 1, 18, 0, tzinfo=timezone.utc),
            )
            db.session.add(event)
            db.session.flush()
            qual = Qualification(name="NeedsQual")
            db.session.add(qual)
            db.session.flush()
            spot = EventSpot(event_id=event.id)
            spot.required_qualifications = [qual]
            db.session.add(spot)
            _make_user("nonqual@test.com", "NoQual", Role.MEMBER)
            db.session.commit()
            spot_id = spot.id

        c = app.test_client()
        _login(c, "nonqual@test.com")
        resp = c.post(f"/assignments/claim/{spot_id}", follow_redirects=True)
        assert resp.status_code == 200
        assert "kvalifikaci" in resp.data.decode()
        with app.app_context():
            assert db.session.scalar(db.select(Assignment).where(Assignment.spot_id == spot_id)) is None

    def test_claim_rejects_overlap_with_draft_event(self, app, member_client):
        """The shared assignment service blocks overlaps, including draft events."""
        conflicting_event_id, conflicting_spot_id = _make_event_with_spot(
            app, EventStatus.DRAFT, name="Conflicting Draft"
        )
        _, spot_id = _make_event_with_spot(app, name="New Conflicting Event")
        with app.app_context():
            member = db.session.scalar(db.select(UserAccount).where(UserAccount.email == "member@test.com"))
            assert member is not None
            db.session.add(Assignment(spot_id=conflicting_spot_id, user_id=member.id, assigned_by_id=member.id))
            db.session.commit()

        response = member_client.post(f"/assignments/claim/{spot_id}", follow_redirects=True)

        assert "překrývající se akci" in response.data.decode()
        assert b"alert-danger" in response.data
        assert f'href="/events/{conflicting_event_id}"'.encode() in response.data
        with app.app_context():
            assert db.session.scalar(db.select(Assignment).where(Assignment.spot_id == spot_id)) is None

    def test_assign_other_with_unknown_user_id_flashes(self, app, admin_client):
        """Passing a user_id that doesn't exist yields 'Uživatel nenalezen'."""
        _, spot_id = _make_event_with_spot(app)
        # Use a well-formed but nonexistent UUID string.
        resp = admin_client.post(
            f"/assignments/assign/{spot_id}",
            data={"user_id": "00000000-0000-0000-0000-000000000000"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert "nenalezen" in resp.data.decode()

    def test_second_claim_returns_clean_error_when_spot_already_taken(self, app):
        """A second claimer on an already-taken spot gets the clean „obsazena“ warning.

        The ``spot.assignment is not None`` guard inside ``do_assign_user`` returns
        first, so the ``UNIQUE(spot_id)`` constraint never fires; this test proves
        the guard's message is what reaches the caller.
        """
        _event_id, spot_id = _make_event_with_spot(app)
        with app.app_context():
            u1 = _make_user("race1@test.com", "Race1", Role.MEMBER)
            u2 = _make_user("race2@test.com", "Race2", Role.MEMBER)
            u1_id, u2_id = u1.id, u2.id

        with app.app_context():
            u1 = db.session.get(UserAccount, u1_id)
            u2 = db.session.get(UserAccount, u2_id)
            # Insert the first assignment directly so the do_assign_user call
            # below re-reads a spot that already has a row — the UNIQUE(spot_id)
            # constraint on Assignment fires on commit.
            db.session.add(Assignment(spot_id=spot_id, user_id=u1.id, assigned_by_id=u1.id))
            db.session.commit()
            # Detach so do_assign_user re-selects
            db.session.expire_all()
            # do_assign_user's own "spot has assignment" guard fires first —
            # this test proves the guard emits the clean „obsazena“ message
            # rather than an IntegrityError.
            result = do_assign_user(spot_id, u2, u2)
            assert result.ok is False
            assert "obsazena" in result.error


@pytest.fixture
def _captured_sql(app):
    # Capture every raw SQL statement fired against the engine while the fixture
    # is active. Used to assert that critical pessimistic-lock queries carry the
    # T-SQL UPDLOCK table hint (SQLAlchemy's mssql dialect silently drops
    # .with_for_update(), so a hint is the only guarantee).
    captured: list[str] = []

    def _before(_conn, _cursor, statement, _parameters, _context, _executemany):
        captured.append(statement)

    with app.app_context():
        engine = db.engine
    sa_event.listen(engine, "before_cursor_execute", _before)
    try:
        yield captured
    finally:
        sa_event.remove(engine, "before_cursor_execute", _before)


class TestPessimisticLockHints:
    # Regression: bare .with_for_update() compiles to a plain SELECT on the
    # SQLAlchemy mssql dialect with no lock hint at all, silently disabling
    # every pessimistic lock in the codebase. Explicit T-SQL WITH (UPDLOCK...)
    # hints replace it; verify the hint reaches the wire.

    def test_spot_claim_emits_updlock_on_event_spot(self, app, member_client, _captured_sql):
        _event_id, spot_id = _make_event_with_spot(app)
        _captured_sql.clear()
        resp = member_client.post(f"/assignments/claim/{spot_id}", follow_redirects=True)
        assert resp.status_code == 200
        lock_selects = [
            s for s in _captured_sql if "event_spot" in s.lower() and "updlock" in s.lower() and "select" in s.lower()
        ]
        assert lock_selects, f"no UPDLOCK on event_spot in: {_captured_sql}"

    def test_overlapping_concurrent_claims_for_one_user_are_serialized(self, app, monkeypatch):
        _, first_spot_id = _make_event_with_spot(app, name="Overlap Race First")
        _, second_spot_id = _make_event_with_spot(app, name="Overlap Race Second")
        start = Barrier(2)
        monkeypatch.setattr("app.routes.assignments.audit", lambda *args, **kwargs: None)

        with app.app_context():
            user = _make_user("overlap_race@test.com", "Overlap Race", Role.MEMBER)
            user_id = user.id

        def claim(spot_id: int) -> bool:
            with app.app_context():
                user = db.session.get(UserAccount, user_id)
                assert user is not None
                start.wait()
                return do_assign_user(spot_id, user, user).ok

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(claim, (first_spot_id, second_spot_id)))

        assert results.count(True) == 1
        with app.app_context():
            assert (
                db.session.scalar(
                    db.select(db.func.count()).select_from(Assignment).where(Assignment.user_id == user_id)
                )
                == 1
            )

    def test_equipment_plan_add_emits_updlock_on_equipment_type(self, app, admin_client, _captured_sql):
        event_id, _ = _make_event_with_spot(app)
        with app.app_context():
            et = EquipmentType(name="Test batoh")
            db.session.add(et)
            db.session.flush()
            # A plan cannot be added when nothing exists in stock; give the type one item.
            db.session.add(EquipmentItem(name="Batoh #1", type_id=et.id))
            db.session.commit()
            type_id = et.id

        _captured_sql.clear()
        resp = admin_client.post(
            f"/events/{event_id}/equipment/plan",
            data={"type_id": str(type_id), "quantity": "1"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        lock_selects = [
            s
            for s in _captured_sql
            if "equipment_type" in s.lower() and "updlock" in s.lower() and "select" in s.lower()
        ]
        assert lock_selects, f"no UPDLOCK on equipment_type in: {_captured_sql}"
