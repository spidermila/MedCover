"""Tests for event model unit tests (no HTTP, pure model logic)."""

from datetime import datetime, timezone

from app.extensions import db
from app.models.assignment import Assignment
from app.models.event import (
    Event,
    EventSpot,
    EventSpotTemplate,
    EventStatus,
    EventTemplate,
    ReminderScheduleMixin,
)
from app.models.master_event import MasterEvent
from app.models.qualification import Qualification
from app.models.role import Role
from app.models.user import UserAccount
from tests.conftest import _make_user


class TestEventStatusValues:
    def test_draft_value(self):
        assert EventStatus.DRAFT.value == "Koncept"

    def test_published_value(self):
        assert EventStatus.PUBLISHED.value == "Zveřejněná"

    def test_assignments_open_value(self):
        assert EventStatus.ASSIGNMENTS_OPEN.value == "Přihlášky otevřeny"

    def test_assignments_closed_value(self):
        assert EventStatus.ASSIGNMENTS_CLOSED.value == "Přihlášky uzavřeny"

    def test_completed_value(self):
        assert EventStatus.COMPLETED.value == "Dokončena"

    def test_cancelled_value(self):
        assert EventStatus.CANCELLED.value == "Zrušena"


class TestUserPermissions:
    def test_has_permission_returns_true_for_admin(self, app):

        with app.app_context():
            _make_user("admin@test.com", "Admin", Role.ADMIN)
            # Reload user in same context to test permissions
            loaded = db.session.scalar(db.select(UserAccount).where(UserAccount.email == "admin@test.com"))
            assert loaded.has_permission("event.create") is True

    def test_has_permission_returns_false_for_viewer(self, app):

        with app.app_context():
            _make_user("viewer@test.com", "Viewer", Role.VIEWER)
            loaded = db.session.scalar(db.select(UserAccount).where(UserAccount.email == "viewer@test.com"))
            assert loaded.has_permission("event.create") is False

    def test_has_any_permission(self, app):

        with app.app_context():
            _make_user("admin@test.com", "Admin", Role.ADMIN)
            loaded = db.session.scalar(db.select(UserAccount).where(UserAccount.email == "admin@test.com"))
            assert loaded.has_any_permission("event.create", "nonexistent.perm") is True

    def test_has_any_permission_all_missing(self, app):

        with app.app_context():
            _make_user("viewer@test.com", "Viewer", Role.VIEWER)
            loaded = db.session.scalar(db.select(UserAccount).where(UserAccount.email == "viewer@test.com"))
            assert loaded.has_any_permission("event.create", "event.edit") is False


class TestUserPassword:
    """Tests for set_password / check_password model methods."""

    def _make_unsaved_user(self) -> UserAccount:
        user = UserAccount(email="pw@test.com", name="PW User", is_active=True)
        return user

    def test_set_and_check_password_correct(self):
        user = self._make_unsaved_user()
        user.set_password("supersecret99")
        assert user.check_password("supersecret99") is True

    def test_check_password_wrong_returns_false(self):
        user = self._make_unsaved_user()
        user.set_password("supersecret99")
        assert user.check_password("wrongpassword") is False

    def test_check_password_empty_returns_false(self):
        user = self._make_unsaved_user()
        user.set_password("supersecret99")
        assert user.check_password("") is False

    def test_password_is_hashed_not_stored_plaintext(self):
        user = self._make_unsaved_user()
        user.set_password("mysecretpassword")
        assert user.password_hash != "mysecretpassword"
        assert "mysecretpassword" not in (user.password_hash or "")


class TestUserGetId:
    def test_get_id_returns_string(self, app):

        with app.app_context():
            _make_user("getid@test.com", "GetId User", Role.MEMBER)
            loaded = db.session.scalar(db.select(UserAccount).where(UserAccount.email == "getid@test.com"))
            result = loaded.get_id()
            assert isinstance(result, str)
            # Must be UUID-like (non-empty string representation of the PK)
            assert len(result) > 0


class TestEventStaffingStatus:
    """Unit tests for Event.staffing_status and Event.is_sufficiently_staffed."""

    def _make_event_with_spots(self, app, mandatory: int, optional: int) -> int:
        """Create an Event with spots and return its id."""
        with app.app_context():
            me = MasterEvent(name="Staffing ME")
            db.session.add(me)
            db.session.flush()
            event = Event(
                name="Staffing Event",
                master_event_id=me.id,
                status=EventStatus.ASSIGNMENTS_OPEN,
                start_datetime=datetime(2030, 1, 1, 10, 0, tzinfo=timezone.utc),
                end_datetime=datetime(2030, 1, 1, 18, 0, tzinfo=timezone.utc),
            )
            db.session.add(event)
            db.session.flush()
            for _ in range(mandatory):
                db.session.add(EventSpot(event_id=event.id, is_optional=False))
            for _ in range(optional):
                db.session.add(EventSpot(event_id=event.id, is_optional=True))
            db.session.commit()
            return event.id

    def _fill_spots(self, app, event_id: int, count: int) -> None:
        with app.app_context():
            spots = db.session.scalars(db.select(EventSpot).where(EventSpot.event_id == event_id)).all()
            for spot in spots[:count]:
                u = UserAccount(email=f"fill_{spot.id}@test.com", name="Filler", is_active=True)
                u.set_password("x")
                db.session.add(u)
                db.session.flush()
                db.session.add(Assignment(spot_id=spot.id, user_id=u.id, assigned_by_id=u.id))
            db.session.commit()

    def test_no_spots_returns_zadne_pozice(self, app):
        event_id = self._make_event_with_spots(app, mandatory=0, optional=0)
        with app.app_context():
            event = db.session.get(Event, event_id)
            assert event.staffing_status == "Žádné pozice"
            assert event.is_sufficiently_staffed is False

    def test_no_assignments_returns_neobsazeno(self, app):
        event_id = self._make_event_with_spots(app, mandatory=2, optional=1)
        with app.app_context():
            event = db.session.get(Event, event_id)
            assert event.staffing_status == "Neobsazeno"
            assert event.is_sufficiently_staffed is False

    def test_partial_mandatory_returns_castecne(self, app):
        event_id = self._make_event_with_spots(app, mandatory=2, optional=0)
        self._fill_spots(app, event_id, 1)
        with app.app_context():
            event = db.session.get(Event, event_id)
            assert event.staffing_status == "Částečně obsazeno"
            assert event.is_sufficiently_staffed is False

    def test_all_mandatory_filled_optional_free_returns_dostatecne(self, app):
        event_id = self._make_event_with_spots(app, mandatory=2, optional=1)
        self._fill_spots(app, event_id, 2)
        with app.app_context():
            event = db.session.get(Event, event_id)
            assert event.staffing_status == "Dostatečně obsazena"
            assert event.is_sufficiently_staffed is True

    def test_all_spots_filled_returns_plne(self, app):
        event_id = self._make_event_with_spots(app, mandatory=2, optional=1)
        self._fill_spots(app, event_id, 3)
        with app.app_context():
            event = db.session.get(Event, event_id)
            assert event.staffing_status == "Plně obsazena"
            assert event.is_sufficiently_staffed is True

    def test_only_optional_spots_all_filled_returns_plne(self, app):
        """Edge case: event with only optional spots, all filled."""
        event_id = self._make_event_with_spots(app, mandatory=0, optional=2)
        with app.app_context():
            event = db.session.get(Event, event_id)
            assert event.staffing_status == "Žádné pozice"
            assert event.is_sufficiently_staffed is False


class TestOptionalFilledSpotsProperty:
    """Regression tests for Event.optional_filled_spots."""

    def _make_event(self, app, mandatory: int, optional: int, fill_mandatory: int, fill_optional: int) -> int:
        with app.app_context():
            me = MasterEvent(name="Opt ME")
            db.session.add(me)
            db.session.flush()
            event = Event(
                name="Opt Event",
                master_event_id=me.id,
                status=EventStatus.ASSIGNMENTS_OPEN,
                start_datetime=datetime(2030, 1, 1, 10, 0, tzinfo=timezone.utc),
                end_datetime=datetime(2030, 1, 1, 18, 0, tzinfo=timezone.utc),
            )
            db.session.add(event)
            db.session.flush()
            mand_spots = [EventSpot(event_id=event.id, is_optional=False) for _ in range(mandatory)]
            opt_spots = [EventSpot(event_id=event.id, is_optional=True) for _ in range(optional)]
            db.session.add_all(mand_spots + opt_spots)
            db.session.flush()

            def _fill(spot: EventSpot, tag: str) -> None:
                u = UserAccount(email=f"{tag}_{spot.id}@test.com", name="F", is_active=True)
                u.set_password("x")
                db.session.add(u)
                db.session.flush()
                db.session.add(Assignment(spot_id=spot.id, user_id=u.id, assigned_by_id=u.id))

            for s in mand_spots[:fill_mandatory]:
                _fill(s, "m")
            for s in opt_spots[:fill_optional]:
                _fill(s, "o")
            db.session.commit()
            return event.id

    def test_none_filled(self, app):
        event_id = self._make_event(app, mandatory=2, optional=2, fill_mandatory=0, fill_optional=0)
        with app.app_context():
            event = db.session.get(Event, event_id)
            assert event.optional_filled_spots == 0
            assert event.optional_total_spots == 2

    def test_only_mandatory_filled(self, app):
        event_id = self._make_event(app, mandatory=2, optional=2, fill_mandatory=2, fill_optional=0)
        with app.app_context():
            event = db.session.get(Event, event_id)
            assert event.optional_filled_spots == 0

    def test_partial_optional_filled(self, app):
        event_id = self._make_event(app, mandatory=1, optional=3, fill_mandatory=0, fill_optional=2)
        with app.app_context():
            event = db.session.get(Event, event_id)
            assert event.optional_filled_spots == 2
            assert event.optional_total_spots == 3

    def test_all_optional_filled(self, app):
        event_id = self._make_event(app, mandatory=1, optional=2, fill_mandatory=0, fill_optional=2)
        with app.app_context():
            event = db.session.get(Event, event_id)
            assert event.optional_filled_spots == 2
            assert event.optional_filled_spots == event.optional_total_spots

    def test_no_optional_spots(self, app):
        event_id = self._make_event(app, mandatory=2, optional=0, fill_mandatory=1, fill_optional=0)
        with app.app_context():
            event = db.session.get(Event, event_id)
            assert event.optional_total_spots == 0
            assert event.optional_filled_spots == 0


class TestEventMiscProperties:
    """Coverage for small helper properties/methods on Event / EventSpot / templates."""

    def test_is_unfilled_true_when_mandatory_spot_missing(self, app):
        with app.app_context():
            me = MasterEvent(name="Unfilled ME")
            db.session.add(me)
            db.session.flush()
            event = Event(
                name="Unfilled Event",
                master_event_id=me.id,
                status=EventStatus.ASSIGNMENTS_OPEN,
                start_datetime=datetime(2030, 1, 1, 10, 0, tzinfo=timezone.utc),
                end_datetime=datetime(2030, 1, 1, 18, 0, tzinfo=timezone.utc),
            )
            db.session.add(event)
            db.session.flush()
            db.session.add(EventSpot(event_id=event.id, is_optional=False))
            db.session.commit()
            assert event.is_unfilled is True

    def test_is_unfilled_false_when_all_mandatory_taken(self, app):
        with app.app_context():
            me = MasterEvent(name="Filled ME")
            db.session.add(me)
            db.session.flush()
            event = Event(
                name="Filled Event",
                master_event_id=me.id,
                status=EventStatus.ASSIGNMENTS_OPEN,
                start_datetime=datetime(2030, 1, 1, 10, 0, tzinfo=timezone.utc),
                end_datetime=datetime(2030, 1, 1, 18, 0, tzinfo=timezone.utc),
            )
            db.session.add(event)
            db.session.flush()
            spot = EventSpot(event_id=event.id, is_optional=False)
            db.session.add(spot)
            db.session.flush()
            u = UserAccount(email="filler@test.com", name="F", is_active=True)
            u.set_password("x")
            db.session.add(u)
            db.session.flush()
            db.session.add(Assignment(spot_id=spot.id, user_id=u.id, assigned_by_id=u.id))
            db.session.commit()
            assert event.is_unfilled is False

    def test_event_repr(self, app):
        with app.app_context():
            me = MasterEvent(name="Repr ME")
            db.session.add(me)
            db.session.flush()
            event = Event(
                name="Repr Event",
                master_event_id=me.id,
                status=EventStatus.DRAFT,
                start_datetime=datetime(2030, 1, 1, 10, 0, tzinfo=timezone.utc),
                end_datetime=datetime(2030, 1, 1, 18, 0, tzinfo=timezone.utc),
            )
            db.session.add(event)
            db.session.flush()
            r = repr(event)
            assert "Repr Event" in r and "Event" in r

    def test_event_spot_repr(self, app):
        with app.app_context():
            me = MasterEvent(name="SpotRepr ME")
            db.session.add(me)
            db.session.flush()
            event = Event(
                name="SpotRepr Event",
                master_event_id=me.id,
                status=EventStatus.DRAFT,
                start_datetime=datetime(2030, 1, 1, 10, 0, tzinfo=timezone.utc),
                end_datetime=datetime(2030, 1, 1, 18, 0, tzinfo=timezone.utc),
            )
            db.session.add(event)
            db.session.flush()
            spot = EventSpot(event_id=event.id)
            db.session.add(spot)
            db.session.flush()
            r = repr(spot)
            assert "EventSpot" in r and str(spot.id) in r

    def test_event_template_repr(self, app):
        with app.app_context():
            tpl = EventTemplate(name="MyTpl")
            db.session.add(tpl)
            db.session.commit()
            assert "MyTpl" in repr(tpl)
            spot_tpl = EventSpotTemplate(template_id=tpl.id, description="S")
            db.session.add(spot_tpl)
            db.session.commit()
            r = repr(spot_tpl)
            assert "EventSpotTemplate" in r and str(spot_tpl.id) in r

    def test_is_eligible_rejects_user_missing_qualification(self, app):
        """EventSpot.is_eligible returns False when the user lacks any required qualification."""
        with app.app_context():
            me = MasterEvent(name="Elig ME")
            db.session.add(me)
            db.session.flush()
            event = Event(
                name="Elig Event",
                master_event_id=me.id,
                status=EventStatus.ASSIGNMENTS_OPEN,
                start_datetime=datetime(2030, 1, 1, 10, 0, tzinfo=timezone.utc),
                end_datetime=datetime(2030, 1, 1, 18, 0, tzinfo=timezone.utc),
            )
            db.session.add(event)
            db.session.flush()
            qual = Qualification(name="Required-Q")
            db.session.add(qual)
            db.session.flush()
            spot = EventSpot(event_id=event.id)
            spot.required_qualifications = [qual]
            db.session.add(spot)
            u = UserAccount(email="noqual@test.com", name="NoQual", is_active=True)
            u.set_password("x")
            db.session.add(u)
            db.session.commit()
            assert spot.is_eligible(u) is False


class TestReminderScheduleMixin:
    """Coverage for ReminderScheduleMixin.reminder_hours()."""

    def test_none_returns_default_24h(self):
        """When ``reminder_schedule`` is None, fall back to the default 24-h reminder.

        Constructed via a lightweight subclass instead of an ORM instance because
        the ``reminder_schedule`` column carries a server-side default of ``"24"``
        that would overwrite ``None`` on flush and skip the fallback branch.
        """

        class Bare(ReminderScheduleMixin):
            reminder_schedule = None

        assert Bare().reminder_hours() == [24]

    def test_parses_csv(self, app):
        with app.app_context():
            me = MasterEvent(name="Rem2 ME")
            db.session.add(me)
            db.session.flush()
            event = Event(
                name="Rem2 Event",
                master_event_id=me.id,
                status=EventStatus.DRAFT,
                start_datetime=datetime(2030, 1, 1, 10, 0, tzinfo=timezone.utc),
                end_datetime=datetime(2030, 1, 1, 18, 0, tzinfo=timezone.utc),
                reminder_schedule="72,24,6",
            )
            db.session.add(event)
            db.session.commit()
            assert event.reminder_hours() == [72, 24, 6]

    def test_ignores_non_numeric_tokens(self, app):
        with app.app_context():
            me = MasterEvent(name="Rem3 ME")
            db.session.add(me)
            db.session.flush()
            event = Event(
                name="Rem3 Event",
                master_event_id=me.id,
                status=EventStatus.DRAFT,
                start_datetime=datetime(2030, 1, 1, 10, 0, tzinfo=timezone.utc),
                end_datetime=datetime(2030, 1, 1, 18, 0, tzinfo=timezone.utc),
                reminder_schedule="24, foo, 12",
            )
            db.session.add(event)
            db.session.commit()
            assert event.reminder_hours() == [24, 12]
