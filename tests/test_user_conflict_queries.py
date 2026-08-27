"""Unit tests for user-assignment conflict query helpers."""

from datetime import datetime, timezone

from app.extensions import db
from app.models.assignment import Assignment
from app.models.event import Event, EventSpot, EventStatus
from app.models.master_event import MasterEvent
from app.models.role import Role
from app.models.user import UserAccount
from app.queries import (
    conflicting_events_for_users,
    user_conflicts_across_events,
    user_ids_with_conflicting_assignments,
)


def _make_user(email: str, name: str = "U") -> UserAccount:
    role = db.session.scalar(db.select(Role).where(Role.name == Role.MEMBER))
    u = UserAccount(email=email, name=name, is_active=True)
    u.set_password("testpass123")
    u.roles = [role]
    db.session.add(u)
    db.session.flush()
    return u


def _make_event(
    name: str,
    start: datetime,
    end: datetime,
    status: EventStatus = EventStatus.ASSIGNMENTS_OPEN,
    archived: bool = False,
) -> Event:
    me = MasterEvent(name=f"ME {name}")
    db.session.add(me)
    db.session.flush()
    e = Event(
        name=name,
        master_event_id=me.id,
        status=status,
        archived=archived,
        start_datetime=start,
        end_datetime=end,
    )
    db.session.add(e)
    db.session.flush()
    return e


def _assign(event: Event, user: UserAccount) -> None:
    spot = EventSpot(event_id=event.id)
    db.session.add(spot)
    db.session.flush()
    db.session.add(Assignment(spot_id=spot.id, user_id=user.id))
    db.session.flush()


class TestUserIdsWithConflictingAssignments:
    """Boundary and status behaviour of the raw conflict-set query."""

    def test_overlap_detected(self, app):
        with app.app_context():
            u = _make_user("uc_overlap@test.com")
            e1 = _make_event(
                "E1",
                datetime(2033, 6, 1, 10, tzinfo=timezone.utc),
                datetime(2033, 6, 1, 14, tzinfo=timezone.utc),
            )
            _assign(e1, u)
            db.session.commit()
            uid = u.id

            result = user_ids_with_conflicting_assignments(
                datetime(2033, 6, 1, 12, tzinfo=timezone.utc),
                datetime(2033, 6, 1, 16, tzinfo=timezone.utc),
            )
        assert uid in result

    def test_back_to_back_does_not_conflict(self, app):
        """Event ending exactly when another begins is not a conflict."""
        with app.app_context():
            u = _make_user("uc_backtoback@test.com")
            e1 = _make_event(
                "BB1",
                datetime(2033, 7, 1, 10, tzinfo=timezone.utc),
                datetime(2033, 7, 1, 14, tzinfo=timezone.utc),
            )
            _assign(e1, u)
            db.session.commit()
            uid = u.id

            result = user_ids_with_conflicting_assignments(
                datetime(2033, 7, 1, 14, tzinfo=timezone.utc),
                datetime(2033, 7, 1, 18, tzinfo=timezone.utc),
            )
        assert uid not in result

    def test_non_overlapping_ignored(self, app):
        with app.app_context():
            u = _make_user("uc_far@test.com")
            e1 = _make_event(
                "F1",
                datetime(2033, 8, 1, 10, tzinfo=timezone.utc),
                datetime(2033, 8, 1, 12, tzinfo=timezone.utc),
            )
            _assign(e1, u)
            db.session.commit()
            uid = u.id

            result = user_ids_with_conflicting_assignments(
                datetime(2033, 8, 2, 10, tzinfo=timezone.utc),
                datetime(2033, 8, 2, 12, tzinfo=timezone.utc),
            )
        assert uid not in result

    def test_cancelled_event_excluded(self, app):
        with app.app_context():
            u = _make_user("uc_cancel@test.com")
            e1 = _make_event(
                "C1",
                datetime(2033, 9, 1, 10, tzinfo=timezone.utc),
                datetime(2033, 9, 1, 14, tzinfo=timezone.utc),
                status=EventStatus.CANCELLED,
            )
            _assign(e1, u)
            db.session.commit()
            uid = u.id

            result = user_ids_with_conflicting_assignments(
                datetime(2033, 9, 1, 11, tzinfo=timezone.utc),
                datetime(2033, 9, 1, 13, tzinfo=timezone.utc),
            )
        assert uid not in result

    def test_completed_event_excluded(self, app):
        with app.app_context():
            u = _make_user("uc_done@test.com")
            e1 = _make_event(
                "D1",
                datetime(2033, 9, 2, 10, tzinfo=timezone.utc),
                datetime(2033, 9, 2, 14, tzinfo=timezone.utc),
                status=EventStatus.COMPLETED,
            )
            _assign(e1, u)
            db.session.commit()
            uid = u.id

            result = user_ids_with_conflicting_assignments(
                datetime(2033, 9, 2, 11, tzinfo=timezone.utc),
                datetime(2033, 9, 2, 13, tzinfo=timezone.utc),
            )
        assert uid not in result

    def test_draft_event_included(self, app):
        """Draft events still count — they may be published and become real conflicts."""
        with app.app_context():
            u = _make_user("uc_draft@test.com")
            e1 = _make_event(
                "DR1",
                datetime(2033, 9, 3, 10, tzinfo=timezone.utc),
                datetime(2033, 9, 3, 14, tzinfo=timezone.utc),
                status=EventStatus.DRAFT,
            )
            _assign(e1, u)
            db.session.commit()
            uid = u.id

            result = user_ids_with_conflicting_assignments(
                datetime(2033, 9, 3, 11, tzinfo=timezone.utc),
                datetime(2033, 9, 3, 13, tzinfo=timezone.utc),
            )
        assert uid in result

    def test_archived_event_excluded(self, app):
        with app.app_context():
            u = _make_user("uc_arch@test.com")
            e1 = _make_event(
                "AR1",
                datetime(2033, 9, 4, 10, tzinfo=timezone.utc),
                datetime(2033, 9, 4, 14, tzinfo=timezone.utc),
                archived=True,
            )
            _assign(e1, u)
            db.session.commit()
            uid = u.id

            result = user_ids_with_conflicting_assignments(
                datetime(2033, 9, 4, 11, tzinfo=timezone.utc),
                datetime(2033, 9, 4, 13, tzinfo=timezone.utc),
            )
        assert uid not in result

    def test_exclude_event_id_hides_own_event(self, app):
        with app.app_context():
            u = _make_user("uc_self@test.com")
            e1 = _make_event(
                "SELF",
                datetime(2033, 10, 1, 10, tzinfo=timezone.utc),
                datetime(2033, 10, 1, 14, tzinfo=timezone.utc),
            )
            _assign(e1, u)
            db.session.commit()

            eid = e1.id
            uid = u.id
            without_exclude = user_ids_with_conflicting_assignments(
                datetime(2033, 10, 1, 10, tzinfo=timezone.utc),
                datetime(2033, 10, 1, 14, tzinfo=timezone.utc),
            )
            with_exclude = user_ids_with_conflicting_assignments(
                datetime(2033, 10, 1, 10, tzinfo=timezone.utc),
                datetime(2033, 10, 1, 14, tzinfo=timezone.utc),
                exclude_event_id=eid,
            )
        assert uid in without_exclude
        assert uid not in with_exclude


class TestConflictingEventsForUsers:
    def test_returns_details_ordered_by_start(self, app):
        with app.app_context():
            u = _make_user("uc_det@test.com")
            e_late = _make_event(
                "Late",
                datetime(2033, 11, 1, 14, tzinfo=timezone.utc),
                datetime(2033, 11, 1, 18, tzinfo=timezone.utc),
            )
            e_early = _make_event(
                "Early",
                datetime(2033, 11, 1, 8, tzinfo=timezone.utc),
                datetime(2033, 11, 1, 12, tzinfo=timezone.utc),
            )
            _assign(e_late, u)
            _assign(e_early, u)
            db.session.commit()
            uid = u.id

            details = conflicting_events_for_users(
                [uid],
                datetime(2033, 11, 1, 6, tzinfo=timezone.utc),
                datetime(2033, 11, 1, 22, tzinfo=timezone.utc),
            )
        assert uid in details
        names = [c["name"] for c in details[uid]]
        assert names == ["Early", "Late"]

    def test_empty_user_ids_returns_empty(self, app):
        with app.app_context():
            result = conflicting_events_for_users(
                [],
                datetime(2033, 12, 1, tzinfo=timezone.utc),
                datetime(2033, 12, 2, tzinfo=timezone.utc),
            )
        assert result == {}

    def test_users_without_conflicts_omitted(self, app):
        with app.app_context():
            u = _make_user("uc_noconf@test.com")
            db.session.commit()
            uid = u.id

            result = conflicting_events_for_users(
                [uid],
                datetime(2033, 12, 5, tzinfo=timezone.utc),
                datetime(2033, 12, 6, tzinfo=timezone.utc),
            )
        assert uid not in result


class TestUserConflictsAcrossEvents:
    def test_batched_produces_per_event_conflict_map(self, app):
        """A single query should feed conflicts to multiple displayed events at once."""
        with app.app_context():
            u = _make_user("uc_bat@test.com")

            # Displayed events (two, overlapping neither each other nor the conflict yet)
            d1 = _make_event(
                "Display 1",
                datetime(2034, 1, 1, 10, tzinfo=timezone.utc),
                datetime(2034, 1, 1, 12, tzinfo=timezone.utc),
            )
            d2 = _make_event(
                "Display 2",
                datetime(2034, 1, 2, 10, tzinfo=timezone.utc),
                datetime(2034, 1, 2, 12, tzinfo=timezone.utc),
            )

            # External event overlapping d1 only
            ext = _make_event(
                "External conflict",
                datetime(2034, 1, 1, 11, tzinfo=timezone.utc),
                datetime(2034, 1, 1, 15, tzinfo=timezone.utc),
            )
            _assign(ext, u)
            db.session.commit()
            uid = u.id
            d1_id, d2_id = d1.id, d2.id

            result = user_conflicts_across_events([d1, d2])

        assert uid in result[d1_id]
        assert result[d1_id][uid][0]["name"] == "External conflict"
        assert uid not in result[d2_id]

    def test_displayed_event_not_self_conflict(self, app):
        """A user assigned to a displayed event should not show as conflicting for that event."""
        with app.app_context():
            u = _make_user("uc_self2@test.com")
            d1 = _make_event(
                "Self D",
                datetime(2034, 2, 1, 10, tzinfo=timezone.utc),
                datetime(2034, 2, 1, 14, tzinfo=timezone.utc),
            )
            _assign(d1, u)
            db.session.commit()
            uid = u.id
            d1_id = d1.id

            result = user_conflicts_across_events([d1])
        assert uid not in result[d1_id]

    def test_empty_events_returns_empty(self, app):
        with app.app_context():
            assert user_conflicts_across_events([]) == {}
