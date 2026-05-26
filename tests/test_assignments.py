"""Tests for spot assignment: claim, release, permissions."""
from app.extensions import db
from app.models.event import Event, EventSpot, EventStatus
from app.models.assignment import Assignment
from app.models.master_event import MasterEvent
from app.models.role import Role
from app.models.user import UserAccount
from tests.conftest import _make_user


def _setup_open_event(app) -> tuple[int, int]:
    """Create a published, assignments-open event with one spot. Returns (event_id, spot_id)."""
    with app.app_context():
        me = MasterEvent(name="Test ME")
        db.session.add(me)
        db.session.flush()

        role = db.session.scalar(db.select(Role).where(Role.name == Role.ADMIN))
        creator = UserAccount(email="creator@test.com", name="Creator", is_active=True)
        creator.set_password("testpass123")
        creator.roles = [role]
        db.session.add(creator)
        db.session.flush()

        from datetime import datetime, timezone
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
        db.session.commit()

        return event.id, spot.id


class TestAssignmentClaim:
    def test_member_can_claim_open_spot(self, app, member_client):
        event_id, spot_id = _setup_open_event(app)
        response = member_client.post(f"/assignments/claim/{spot_id}", follow_redirects=False)
        assert response.status_code == 302
        with app.app_context():
            assignment = db.session.scalar(
                db.select(Assignment).where(Assignment.spot_id == spot_id)
            )
            assert assignment is not None
            # Verify the correct user is stored
            member = db.session.scalar(
                db.select(UserAccount).where(UserAccount.email == "member@test.com")
            )
            assert assignment.user_id == member.id

    def test_claim_already_taken_spot_is_rejected(self, app, member_client):
        event_id, spot_id = _setup_open_event(app)
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
        _, spot_id = _setup_open_event(app)
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

            from datetime import datetime, timezone
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
        event_id, spot_id = _setup_open_event(app)
        member_client.post(f"/assignments/claim/{spot_id}", follow_redirects=True)
        with app.app_context():
            assignment = db.session.scalar(
                db.select(Assignment).where(Assignment.spot_id == spot_id)
            )
            assignment_id = assignment.id

        response = member_client.post(
            f"/assignments/release/{assignment_id}", follow_redirects=False
        )
        assert response.status_code == 302
        with app.app_context():
            remaining = db.session.get(Assignment, assignment_id)
            assert remaining is None


class TestAdminAssignment:
    def test_admin_can_assign_other_user(self, app, admin_client):
        event_id, spot_id = _setup_open_event(app)
        with app.app_context():
            _make_user("target@test.com", "Target", Role.MEMBER)
            target = db.session.scalar(
                db.select(UserAccount).where(UserAccount.email == "target@test.com")
            )
            target_id = str(target.id)

        response = admin_client.post(
            f"/assignments/assign/{spot_id}",
            data={"user_id": target_id},
            follow_redirects=False,
        )
        assert response.status_code == 302
        with app.app_context():
            assignment = db.session.scalar(
                db.select(Assignment).where(Assignment.spot_id == spot_id)
            )
            assert assignment is not None

    def test_member_cannot_assign_others(self, app, member_client):
        event_id, spot_id = _setup_open_event(app)
        with app.app_context():
            _make_user("target@test.com", "Target", Role.MEMBER)
            target = db.session.scalar(
                db.select(UserAccount).where(UserAccount.email == "target@test.com")
            )
            target_id = str(target.id)

        response = member_client.post(
            f"/assignments/assign/{spot_id}",
            data={"user_id": target_id},
            follow_redirects=False,
        )
        assert response.status_code == 403

    def test_second_user_cannot_claim_taken_spot(self, app, member_client):
        """A different user should not be able to claim a spot already taken by another user."""
        event_id, spot_id = _setup_open_event(app)

        # First member claims the spot
        member_client.post(f"/assignments/claim/{spot_id}", follow_redirects=True)

        # Second member (fresh client — separate session) tries to claim the same spot
        with app.app_context():
            _make_user("member2@test.com", "Second Member", Role.MEMBER)

        from tests.conftest import _login
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
        event_id, spot_id = _setup_open_event(app)

        # First member claims the spot
        member_client.post(f"/assignments/claim/{spot_id}", follow_redirects=True)
        with app.app_context():
            assignment = db.session.scalar(
                db.select(Assignment).where(Assignment.spot_id == spot_id)
            )
            assignment_id = assignment.id

        # Second member (fresh client — separate session) tries to release it
        with app.app_context():
            _make_user("member2@test.com", "Second Member", Role.MEMBER)
        from tests.conftest import _login
        second_client = app.test_client()
        _login(second_client, "member2@test.com")
        response = second_client.post(f"/assignments/release/{assignment_id}", follow_redirects=False)

        assert response.status_code == 403
        with app.app_context():
            remaining = db.session.get(Assignment, assignment_id)
            assert remaining is not None  # Assignment must still exist


class TestAdminUnassign:
    def test_admin_can_unassign_user(self, app, admin_client):
        event_id, spot_id = _setup_open_event(app)
        # Create a member and have them claim the spot via a fresh client
        with app.app_context():
            _make_user("claimer@test.com", "Claimer", Role.MEMBER)
        from tests.conftest import _login
        claimer = app.test_client()
        _login(claimer, "claimer@test.com")
        claimer.post(f"/assignments/claim/{spot_id}", follow_redirects=True)

        with app.app_context():
            assignment = db.session.scalar(
                db.select(Assignment).where(Assignment.spot_id == spot_id)
            )
            assignment_id = assignment.id

        response = admin_client.post(
            f"/assignments/unassign/{assignment_id}", follow_redirects=False
        )
        assert response.status_code == 302
        with app.app_context():
            remaining = db.session.get(Assignment, assignment_id)
            assert remaining is None

    def test_member_cannot_unassign_others(self, app, admin_client):
        event_id, spot_id = _setup_open_event(app)
        # Admin assigns target@test.com
        with app.app_context():
            _make_user("target@test.com", "Target", Role.MEMBER)
            target = db.session.scalar(
                db.select(UserAccount).where(UserAccount.email == "target@test.com")
            )
            target_id = str(target.id)

        admin_client.post(
            f"/assignments/assign/{spot_id}",
            data={"user_id": target_id},
            follow_redirects=True,
        )
        with app.app_context():
            assignment = db.session.scalar(
                db.select(Assignment).where(Assignment.spot_id == spot_id)
            )
            assert assignment is not None
            assignment_id = assignment.id

        # A plain member (not target, not admin) tries to unassign via fresh client
        with app.app_context():
            _make_user("attacker@test.com", "Attacker", Role.MEMBER)
        from tests.conftest import _login
        attacker = app.test_client()
        _login(attacker, "attacker@test.com")
        response = attacker.post(f"/assignments/unassign/{assignment_id}")
        assert response.status_code == 403

    def test_unassign_nonexistent_returns_404(self, admin_client):
        response = admin_client.post("/assignments/unassign/999999")
        assert response.status_code == 404


# ── Claim edge cases ──────────────────────────────────────────────────────────

class TestClaimEdgeCases:
    def test_viewer_cannot_claim(self, app):
        """A Viewer user (no event.assign_own) gets 403."""
        from tests.conftest import _login
        event_id, spot_id = _setup_open_event(app)
        with app.app_context():
            _make_user("viewer@test.com", "Viewer", Role.VIEWER)
        c = app.test_client()
        _login(c, "viewer@test.com")
        response = c.post(f"/assignments/claim/{spot_id}")
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
            from datetime import datetime, timezone
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
            assert db.session.scalar(
                db.select(db.func.count()).select_from(Assignment).where(Assignment.spot_id == spot_id)
            ) == 0

    def test_claim_when_already_assigned_to_event_flashes(self, app, member_client):
        """User already has a spot on this event — second claim rejected."""
        event_id, spot_id = _setup_open_event(app)
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
        event_id, spot_id = _setup_open_event(app)
        member_client.post(f"/assignments/claim/{spot_id}", follow_redirects=True)
        with app.app_context():
            event = db.session.get(Event, event_id)
            event.status = EventStatus.COMPLETED
            db.session.commit()
            assignment = db.session.scalar(
                db.select(Assignment).where(Assignment.spot_id == spot_id)
            )
            assignment_id = assignment.id

        response = member_client.post(f"/assignments/release/{assignment_id}", follow_redirects=True)
        assert response.status_code == 200
        assert "dokončen" in response.data.decode() or "nelze" in response.data.decode()

    def test_release_reopens_assignments_closed_event(self, app, member_client):
        """Releasing a spot from a CLOSED event re-opens assignments."""
        event_id, spot_id = _setup_open_event(app)
        member_client.post(f"/assignments/claim/{spot_id}", follow_redirects=True)
        with app.app_context():
            event = db.session.get(Event, event_id)
            event.status = EventStatus.ASSIGNMENTS_CLOSED
            db.session.commit()
            assignment = db.session.scalar(
                db.select(Assignment).where(Assignment.spot_id == spot_id)
            )
            assignment_id = assignment.id

        member_client.post(f"/assignments/release/{assignment_id}", follow_redirects=True)
        with app.app_context():
            event = db.session.get(Event, event_id)
            assert event.status == EventStatus.ASSIGNMENTS_OPEN


# ── Assign-other edge cases ───────────────────────────────────────────────────

class TestAssignOtherEdgeCases:
    def test_assign_without_user_id_flashes(self, app, admin_client):
        _, spot_id = _setup_open_event(app)
        response = admin_client.post(
            f"/assignments/assign/{spot_id}", data={}, follow_redirects=True
        )
        assert response.status_code == 200
        assert "Vyberte" in response.data.decode() or "uživatele" in response.data.decode()

    def test_assign_inactive_user_flashes(self, app, admin_client):
        _, spot_id = _setup_open_event(app)
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
        response = admin_client.post(
            "/assignments/assign/999999", data={"user_id": target_id}
        )
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
            from datetime import datetime, timezone
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
        event_id, spot_id = _setup_open_event(app)
        with app.app_context():
            t1 = _make_user("ta@test.com", "TA", Role.MEMBER)
            t2 = _make_user("tb@test.com", "TB", Role.MEMBER)
            t1_id, t2_id = str(t1.id), str(t2.id)

        admin_client.post(f"/assignments/assign/{spot_id}", data={"user_id": t1_id}, follow_redirects=True)
        response = admin_client.post(
            f"/assignments/assign/{spot_id}", data={"user_id": t2_id}, follow_redirects=True
        )
        assert response.status_code == 200
        assert "obsazena" in response.data.decode()

    def test_assign_same_user_twice_flashes(self, app, admin_client):
        """Assigning the same user to a second spot on the same event should flash."""
        event_id, spot_id = _setup_open_event(app)
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
        event_id, spot_id = _setup_open_event(app)
        with app.app_context():
            target = _make_user("td@test.com", "TD", Role.MEMBER)
            target_id = str(target.id)
        admin_client.post(f"/assignments/assign/{spot_id}", data={"user_id": target_id}, follow_redirects=True)
        with app.app_context():
            db.session.get(Event, event_id).status = EventStatus.COMPLETED
            db.session.commit()
            assignment = db.session.scalar(
                db.select(Assignment).where(Assignment.spot_id == spot_id)
            )
            assignment_id = assignment.id

        response = admin_client.post(f"/assignments/unassign/{assignment_id}", follow_redirects=True)
        assert response.status_code == 200
        assert "dokončen" in response.data.decode() or "nelze" in response.data.decode()

    def test_unassign_reopens_assignments_closed_event(self, app, admin_client):
        """Unassigning from a CLOSED event should re-open assignments."""
        event_id, spot_id = _setup_open_event(app)
        with app.app_context():
            target = _make_user("te@test.com", "TE", Role.MEMBER)
            target_id = str(target.id)
        admin_client.post(f"/assignments/assign/{spot_id}", data={"user_id": target_id}, follow_redirects=True)
        with app.app_context():
            db.session.get(Event, event_id).status = EventStatus.ASSIGNMENTS_CLOSED
            db.session.commit()
            assignment_id = db.session.scalar(
                db.select(Assignment).where(Assignment.spot_id == spot_id)
            ).id

        admin_client.post(f"/assignments/unassign/{assignment_id}", follow_redirects=True)
        with app.app_context():
            assert db.session.get(Event, event_id).status == EventStatus.ASSIGNMENTS_OPEN
