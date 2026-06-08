"""Tests for the reports blueprint."""

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

from app.extensions import db
from app.models.assignment import Assignment, DebriefingRecord
from app.models.event import Event, EventSpot, EventStatus
from app.models.master_event import MasterEvent
from app.models.role import Role
from app.models.user import UserAccount
from app.routes.reports import _compute_user_stats
from tests.conftest import _login, _make_user


def _make_me(name: str = "Testovací nadřazená akce") -> MasterEvent:
    me = MasterEvent(name=name)
    db.session.add(me)
    db.session.commit()
    return me


def _make_event(
    me: MasterEvent,
    name: str = "Testovací akce",
    status: EventStatus = EventStatus.COMPLETED,
    start: datetime | None = None,
    end: datetime | None = None,
) -> Event:
    now = datetime.now(timezone.utc)
    ev = Event(
        name=name,
        master_event_id=me.id,
        status=status,
        start_datetime=start or now - timedelta(hours=4),
        end_datetime=end or now - timedelta(hours=2),
    )
    db.session.add(ev)
    db.session.commit()
    return ev


def _make_spot(event: Event) -> EventSpot:
    spot = EventSpot(event_id=event.id)
    db.session.add(spot)
    db.session.commit()
    return spot


def _make_assignment(spot: EventSpot, user: UserAccount, admin: UserAccount) -> Assignment:
    asgn = Assignment(spot_id=spot.id, user_id=user.id, assigned_by_id=admin.id)
    db.session.add(asgn)
    db.session.commit()
    return asgn


def _make_debriefing(asgn: Assignment, actual_hours: float = 2.0, patients: int = 3) -> DebriefingRecord:
    """Create a DebriefingRecord and set actual times/patients on the event."""
    # Set actual times on the event so actual_hours property works
    spot = db.session.get(EventSpot, asgn.spot_id)
    assert spot is not None
    event = db.session.get(Event, spot.event_id)
    assert event is not None
    event.actual_start_datetime = event.start_datetime
    event.actual_end_datetime = event.start_datetime + timedelta(hours=actual_hours)
    event.post_event_count = patients
    dr = DebriefingRecord(
        assignment_id=asgn.id,
        submitted_by_id=asgn.user_id,
        grade=3,
    )
    db.session.add(dr)
    db.session.commit()
    return dr


# ── Index ─────────────────────────────────────────────────────────────────────


class TestReportsIndex:
    def test_redirect_when_not_logged_in(self, client):
        resp = client.get("/reports/", follow_redirects=False)
        assert resp.status_code == 302

    def test_admin_can_access_index(self, app, client):
        with app.app_context():
            _make_user("admin_rep@test.com", "Admin Rep", Role.ADMIN)
        _login(client, "admin_rep@test.com")
        resp = client.get("/reports/")
        assert resp.status_code == 200
        assert "Přehledy".encode() in resp.data

    def test_member_can_access_index(self, app, client):
        """Members have report.view permission."""
        with app.app_context():
            _make_user("member_rep@test.com", "Member Rep", Role.MEMBER)
        _login(client, "member_rep@test.com")
        resp = client.get("/reports/")
        assert resp.status_code == 200

    def test_unauthenticated_cannot_access_index(self, client):
        resp = client.get("/reports/", follow_redirects=False)
        assert resp.status_code == 302


# ── Per-user report ───────────────────────────────────────────────────────────


class TestUserReport:
    def test_member_can_access_own_report(self, app, client):
        with app.app_context():
            user = _make_user("member_own@test.com", "Own Member", Role.MEMBER)
            user_id = user.id
        _login(client, "member_own@test.com")
        resp = client.get(f"/reports/user/{user_id}")
        assert resp.status_code == 200

    def test_user_without_permission_cannot_access_other_user_report(self, app, client):
        """A user with no roles (no report.view) cannot view another user's report."""
        with app.app_context():
            # Create user with no roles (no report.view)
            norole = UserAccount(email="norole@test.com", name="No Role", is_active=True)
            norole.set_password("testpass123")
            db.session.add(norole)
            other = _make_user("other_norole@test.com", "Other User", Role.MEMBER)
            other_id = other.id
            db.session.commit()
        _login(client, "norole@test.com")
        resp = client.get(f"/reports/user/{other_id}")
        assert resp.status_code == 403

    def test_user_without_permission_can_access_own_report(self, app, client):
        """A user with no roles can still view their own report."""
        with app.app_context():
            norole = UserAccount(email="norole_own@test.com", name="No Role Own", is_active=True)
            norole.set_password("testpass123")
            db.session.add(norole)
            db.session.commit()
            norole_id = norole.id
        _login(client, "norole_own@test.com")
        resp = client.get(f"/reports/user/{norole_id}")
        assert resp.status_code == 200

    def test_admin_can_access_any_user_report(self, app, client):
        with app.app_context():
            _make_user("admin_ur@test.com", "Admin UR", Role.ADMIN)
            member = _make_user("member_c@test.com", "Member C", Role.MEMBER)
            member_id = member.id
        _login(client, "admin_ur@test.com")
        resp = client.get(f"/reports/user/{member_id}")
        assert resp.status_code == 200

    def test_coordinator_can_access_other_user_report(self, app, client):
        with app.app_context():
            _make_user("coord_ur@test.com", "Coord UR", Role.COORDINATOR)
            member = _make_user("member_d@test.com", "Member D", Role.MEMBER)
            member_id = member.id
        _login(client, "coord_ur@test.com")
        resp = client.get(f"/reports/user/{member_id}")
        assert resp.status_code == 200

    def test_404_for_nonexistent_user(self, app, client):
        with app.app_context():
            _make_user("admin_404@test.com", "Admin 404", Role.ADMIN)
        _login(client, "admin_404@test.com")
        resp = client.get(f"/reports/user/{uuid.uuid4()}")
        assert resp.status_code == 404

    def test_user_report_shows_correct_event_count(self, app, client):
        with app.app_context():
            admin = _make_user("admin_cnt@test.com", "Admin Cnt", Role.ADMIN)
            member = _make_user("member_cnt@test.com", "Member Cnt", Role.MEMBER)
            member_id = member.id

            me = _make_me("ME Count")
            ev1 = _make_event(me, "Akce 1", EventStatus.COMPLETED)
            ev2 = _make_event(me, "Akce 2", EventStatus.COMPLETED)
            # one event not assigned to member
            _make_event(me, "Akce 3", EventStatus.COMPLETED)

            spot1 = _make_spot(ev1)
            spot2 = _make_spot(ev2)
            _make_assignment(spot1, member, admin)
            _make_assignment(spot2, member, admin)

        _login(client, "admin_cnt@test.com")
        resp = client.get(f"/reports/user/{member_id}")
        assert resp.status_code == 200
        # member has 2 assignments
        assert b"Akce 1" in resp.data
        assert b"Akce 2" in resp.data
        assert b"Akce 3" not in resp.data

    def test_user_report_shows_debriefing_data(self, app, client):
        with app.app_context():
            admin = _make_user("admin_deb@test.com", "Admin Deb", Role.ADMIN)
            member = _make_user("member_deb@test.com", "Member Deb", Role.MEMBER)
            member_id = member.id

            me = _make_me("ME Deb")
            ev = _make_event(me, "Debriefing Akce", EventStatus.COMPLETED)
            spot = _make_spot(ev)
            asgn = _make_assignment(spot, member, admin)
            _make_debriefing(asgn, actual_hours=5.5, patients=7)

        _login(client, "admin_deb@test.com")
        resp = client.get(f"/reports/user/{member_id}")
        assert resp.status_code == 200
        assert b"5,5" in resp.data
        assert b"7" in resp.data

    def test_user_report_sum_row_planned_hours_is_dash(self, app, client):
        """Sum row for completed events must show — in the planned hours column (issue #108)."""
        with app.app_context():
            admin = _make_user("admin_sum@test.com", "Admin Sum", Role.ADMIN)
            member = _make_user("member_sum@test.com", "Member Sum", Role.MEMBER)
            member_id = member.id

            me = _make_me("ME Sum")
            ev = _make_event(me, "Completed Akce", EventStatus.COMPLETED)
            spot = _make_spot(ev)
            _make_assignment(spot, member, admin)

        _login(client, "admin_sum@test.com")
        resp = client.get(f"/reports/user/{member_id}")
        assert resp.status_code == 200
        # The tfoot planned-hours cell must be a dash, not a number
        assert b"Celkem (dokon\xc4\x8den\xc3\xa9 akce)" in resp.data
        html = resp.data.decode("utf-8")
        # Locate the tfoot section and confirm it contains the em-dash
        tfoot_start = html.find("<tfoot")
        assert tfoot_start != -1
        tfoot_html = html[tfoot_start:]
        assert "—" in tfoot_html


class TestUserReportDateFilter:
    """Date range filter on the per-user report."""

    def _setup(self, app, email_suffix: str) -> tuple[str, str]:
        """Create admin + member users and return (admin_email, member_id)."""
        with app.app_context():
            _make_user(f"admin_dr_{email_suffix}@test.com", "Admin DR", Role.ADMIN)
            member = _make_user(f"member_dr_{email_suffix}@test.com", "Member DR", Role.MEMBER)
            return f"admin_dr_{email_suffix}@test.com", str(member.id)

    def test_from_date_excludes_earlier_events(self, app, client):
        admin_email, member_id = self._setup(app, "from")
        with app.app_context():
            admin = db.session.scalar(db.select(UserAccount).where(UserAccount.email == admin_email))
            member = db.session.get(UserAccount, member_id)
            me = _make_me("DR From ME")
            early = _make_event(
                me,
                "Early Event",
                start=datetime(2025, 1, 10, 10, 0, tzinfo=timezone.utc),
                end=datetime(2025, 1, 10, 18, 0, tzinfo=timezone.utc),
            )
            later = _make_event(
                me,
                "Later Event",
                start=datetime(2025, 3, 10, 10, 0, tzinfo=timezone.utc),
                end=datetime(2025, 3, 10, 18, 0, tzinfo=timezone.utc),
            )
            _make_assignment(_make_spot(early), member, admin)
            _make_assignment(_make_spot(later), member, admin)

        _login(client, admin_email)
        resp = client.get(f"/reports/user/{member_id}?from_date=2025-02-01")
        assert resp.status_code == 200
        assert b"Early Event" not in resp.data
        assert b"Later Event" in resp.data

    def test_to_date_excludes_later_events(self, app, client):
        admin_email, member_id = self._setup(app, "to")
        with app.app_context():
            admin = db.session.scalar(db.select(UserAccount).where(UserAccount.email == admin_email))
            member = db.session.get(UserAccount, member_id)
            me = _make_me("DR To ME")
            early = _make_event(
                me,
                "Early Event",
                start=datetime(2025, 1, 10, 10, 0, tzinfo=timezone.utc),
                end=datetime(2025, 1, 10, 18, 0, tzinfo=timezone.utc),
            )
            later = _make_event(
                me,
                "Later Event",
                start=datetime(2025, 3, 10, 10, 0, tzinfo=timezone.utc),
                end=datetime(2025, 3, 10, 18, 0, tzinfo=timezone.utc),
            )
            _make_assignment(_make_spot(early), member, admin)
            _make_assignment(_make_spot(later), member, admin)

        _login(client, admin_email)
        resp = client.get(f"/reports/user/{member_id}?to_date=2025-02-01")
        assert resp.status_code == 200
        assert b"Early Event" in resp.data
        assert b"Later Event" not in resp.data

    def test_both_dates_show_only_events_in_range(self, app, client):
        admin_email, member_id = self._setup(app, "both")
        with app.app_context():
            admin = db.session.scalar(db.select(UserAccount).where(UserAccount.email == admin_email))
            member = db.session.get(UserAccount, member_id)
            me = _make_me("DR Both ME")
            before = _make_event(
                me,
                "Before Range",
                start=datetime(2025, 1, 5, 10, 0, tzinfo=timezone.utc),
                end=datetime(2025, 1, 5, 18, 0, tzinfo=timezone.utc),
            )
            inside = _make_event(
                me,
                "Inside Range",
                start=datetime(2025, 2, 15, 10, 0, tzinfo=timezone.utc),
                end=datetime(2025, 2, 15, 18, 0, tzinfo=timezone.utc),
            )
            after = _make_event(
                me,
                "After Range",
                start=datetime(2025, 4, 1, 10, 0, tzinfo=timezone.utc),
                end=datetime(2025, 4, 1, 18, 0, tzinfo=timezone.utc),
            )
            for ev in [before, inside, after]:
                _make_assignment(_make_spot(ev), member, admin)

        _login(client, admin_email)
        resp = client.get(f"/reports/user/{member_id}?from_date=2025-02-01&to_date=2025-03-01")
        assert resp.status_code == 200
        assert b"Before Range" not in resp.data
        assert b"Inside Range" in resp.data
        assert b"After Range" not in resp.data

    def test_to_date_is_inclusive_of_end_day(self, app, client):
        """An event starting on to_date itself must be included (+1 day boundary)."""
        admin_email, member_id = self._setup(app, "inc")
        with app.app_context():
            admin = db.session.scalar(db.select(UserAccount).where(UserAccount.email == admin_email))
            member = db.session.get(UserAccount, member_id)
            me = _make_me("DR Inc ME")
            on_boundary = _make_event(
                me,
                "Boundary Event",
                start=datetime(2025, 3, 31, 10, 0, tzinfo=timezone.utc),
                end=datetime(2025, 3, 31, 18, 0, tzinfo=timezone.utc),
            )
            _make_assignment(_make_spot(on_boundary), member, admin)

        _login(client, admin_email)
        resp = client.get(f"/reports/user/{member_id}?from_date=2025-01-01&to_date=2025-03-31")
        assert resp.status_code == 200
        assert b"Boundary Event" in resp.data

    def test_invalid_date_shows_error(self, app, client):
        admin_email, member_id = self._setup(app, "err")
        _login(client, admin_email)
        resp = client.get(f"/reports/user/{member_id}?from_date=not-a-date")
        assert resp.status_code == 200
        assert "Neplatný formát data".encode() in resp.data


# ── Per-ME report ─────────────────────────────────────────────────────────────


class TestMEReport:
    def test_admin_can_access_me_report(self, app, client):
        with app.app_context():
            _make_user("admin_me@test.com", "Admin ME", Role.ADMIN)
            me = _make_me("ME Test Report")
            me_id = me.id
        _login(client, "admin_me@test.com")
        resp = client.get(f"/reports/master-event/{me_id}")
        assert resp.status_code == 200
        assert b"ME Test Report" in resp.data

    def test_member_can_access_me_report(self, app, client):
        with app.app_context():
            _make_user("member_me@test.com", "Member ME", Role.MEMBER)
            me = _make_me("ME Member Report")
            me_id = me.id
        _login(client, "member_me@test.com")
        resp = client.get(f"/reports/master-event/{me_id}")
        assert resp.status_code == 200

    def test_unauthenticated_cannot_access_me_report(self, app, client):
        with app.app_context():
            me = _make_me("ME Viewer")
            me_id = me.id
        resp = client.get(f"/reports/master-event/{me_id}", follow_redirects=False)
        assert resp.status_code == 302

    def test_me_report_404_for_nonexistent(self, app, client):
        with app.app_context():
            _make_user("admin_me404@test.com", "Admin ME 404", Role.ADMIN)
        _login(client, "admin_me404@test.com")
        resp = client.get("/reports/master-event/99999")
        assert resp.status_code == 404

    def test_me_report_shows_events(self, app, client):
        with app.app_context():
            admin = _make_user("admin_mev@test.com", "Admin MEV", Role.ADMIN)
            member = _make_user("member_mev@test.com", "Member MEV", Role.MEMBER)
            me = _make_me("ME With Events")
            me_id = me.id
            ev = _make_event(me, "Event V1", EventStatus.COMPLETED)
            spot = _make_spot(ev)
            asgn = _make_assignment(spot, member, admin)
            _make_debriefing(asgn, actual_hours=3.0, patients=2)
        _login(client, "admin_mev@test.com")
        resp = client.get(f"/reports/master-event/{me_id}")
        assert resp.status_code == 200
        assert b"Event V1" in resp.data
        assert b"3,0" in resp.data


# ── Date-range report ─────────────────────────────────────────────────────────


class TestDateRangeReport:
    def test_get_shows_form(self, app, client):
        with app.app_context():
            _make_user("admin_dr@test.com", "Admin DR", Role.ADMIN)
        _login(client, "admin_dr@test.com")
        resp = client.get("/reports/date-range")
        assert resp.status_code == 200
        assert b"from_date" in resp.data
        assert b"to_date" in resp.data

    def test_get_with_params_shows_results(self, app, client):
        with app.app_context():
            _make_user("admin_drp@test.com", "Admin DRP", Role.ADMIN)
            me = _make_me("ME DR Params")
            now = datetime.now(timezone.utc)
            _make_event(
                me,
                "DR Akce",
                EventStatus.COMPLETED,
                start=now - timedelta(days=1),
                end=now - timedelta(days=1) + timedelta(hours=2),
            )
        _login(client, "admin_drp@test.com")
        from_d = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
        to_d = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        resp = client.get(f"/reports/date-range?from_date={from_d}&to_date={to_d}")
        assert resp.status_code == 200
        assert b"DR Akce" in resp.data

    def test_date_range_only_returns_events_in_range(self, app, client):
        with app.app_context():
            _make_user("admin_drr@test.com", "Admin DRR", Role.ADMIN)
            me = _make_me("ME DR Range")
            now = datetime.now(timezone.utc)
            # inside range
            _make_event(
                me,
                "In Range",
                EventStatus.COMPLETED,
                start=now - timedelta(days=3),
                end=now - timedelta(days=3) + timedelta(hours=2),
            )
            # outside range
            _make_event(
                me,
                "Out of Range",
                EventStatus.COMPLETED,
                start=now - timedelta(days=30),
                end=now - timedelta(days=30) + timedelta(hours=2),
            )
        _login(client, "admin_drr@test.com")
        from_d = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
        to_d = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        resp = client.get(f"/reports/date-range?from_date={from_d}&to_date={to_d}")
        assert resp.status_code == 200
        assert b"In Range" in resp.data
        assert b"Out of Range" not in resp.data

    def test_date_range_empty_result_for_no_events(self, app, client):
        with app.app_context():
            _make_user("admin_dre@test.com", "Admin DRE", Role.ADMIN)
        _login(client, "admin_dre@test.com")
        resp = client.get("/reports/date-range?from_date=2000-01-01&to_date=2000-01-31")
        assert resp.status_code == 200
        # Should not error, may show empty message
        assert b"date-range" in resp.data or b"from_date" in resp.data

    def test_unauthenticated_cannot_access_date_range(self, client):
        resp = client.get("/reports/date-range", follow_redirects=False)
        assert resp.status_code == 302


# ── UserStats / _compute_user_stats unit tests ────────────────────────────────


class TestComputeUserStats:
    """Unit tests for the _compute_user_stats helper."""

    def _make_fake_event(
        self,
        status: EventStatus,
        start: datetime,
        end: datetime,
        paid: bool = True,
        actual_start: datetime | None = None,
        actual_end: datetime | None = None,
    ):
        """Return a simple namespace that mimics the Event properties used by _compute_user_stats."""

        actual_hours: Decimal | None = None
        if actual_start and actual_end:
            delta = actual_end - actual_start
            actual_hours = Decimal(str(round(delta.total_seconds() / 3600, 1)))

        scheduled_delta = end - start
        scheduled_hours = Decimal(str(round(scheduled_delta.total_seconds() / 3600, 1)))

        return SimpleNamespace(
            status=status,
            start_datetime=start,
            end_datetime=end,
            paid=paid,
            actual_hours=actual_hours,
            scheduled_hours=scheduled_hours,
        )

    def test_completed_shift_counts_served(self):

        now = datetime.now(timezone.utc)
        ev = self._make_fake_event(
            EventStatus.COMPLETED,
            now - timedelta(hours=4),
            now - timedelta(hours=2),
        )
        stats = _compute_user_stats([(None, ev)], now)  # type: ignore[arg-type]
        assert stats.shifts_served == 1
        assert stats.shifts_planned == 0

    def test_future_event_counts_planned(self):

        now = datetime.now(timezone.utc)
        ev = self._make_fake_event(
            EventStatus.ASSIGNMENTS_OPEN,
            now + timedelta(days=1),
            now + timedelta(days=1, hours=3),
        )
        stats = _compute_user_stats([(None, ev)], now)  # type: ignore[arg-type]
        assert stats.shifts_planned == 1
        assert stats.shifts_served == 0

    def test_cancelled_event_excluded(self):

        now = datetime.now(timezone.utc)
        ev = self._make_fake_event(
            EventStatus.CANCELLED,
            now - timedelta(hours=4),
            now - timedelta(hours=2),
        )
        stats = _compute_user_stats([(None, ev)], now)  # type: ignore[arg-type]
        assert stats.shifts_served == 0
        assert stats.shifts_planned == 0
        assert stats.hours_total == 0

    def test_actual_hours_used_when_available(self):

        now = datetime.now(timezone.utc)
        ev = self._make_fake_event(
            EventStatus.COMPLETED,
            now - timedelta(hours=8),
            now - timedelta(hours=4),
            actual_start=now - timedelta(hours=7),
            actual_end=now - timedelta(hours=5),  # 2 actual hours
        )
        stats = _compute_user_stats([(None, ev)], now)  # type: ignore[arg-type]
        assert stats.hours_served == Decimal("2")

    def test_planned_hours_fallback_for_completed_without_actuals(self):

        now = datetime.now(timezone.utc)
        ev = self._make_fake_event(
            EventStatus.COMPLETED,
            now - timedelta(hours=4),
            now - timedelta(hours=2),  # 2 planned hours, no actuals
        )
        stats = _compute_user_stats([(None, ev)], now)  # type: ignore[arg-type]
        assert stats.hours_served == Decimal("2")

    def test_unpaid_event_counts_hours_free(self):

        now = datetime.now(timezone.utc)
        ev = self._make_fake_event(
            EventStatus.COMPLETED,
            now - timedelta(hours=4),
            now - timedelta(hours=2),
            paid=False,
        )
        stats = _compute_user_stats([(None, ev)], now)  # type: ignore[arg-type]
        assert stats.hours_free == Decimal("2")

    def test_paid_event_does_not_count_hours_free(self):

        now = datetime.now(timezone.utc)
        ev = self._make_fake_event(
            EventStatus.COMPLETED,
            now - timedelta(hours=4),
            now - timedelta(hours=2),
            paid=True,
        )
        stats = _compute_user_stats([(None, ev)], now)  # type: ignore[arg-type]
        assert stats.hours_free == 0

    def test_last_and_next_shift_dates(self):
        """_compute_user_stats sets last_shift but not next_shift (handled by _resolve_next_shifts)."""

        now = datetime.now(timezone.utc)
        past_ev = self._make_fake_event(
            EventStatus.COMPLETED,
            now - timedelta(days=5),
            now - timedelta(days=5) + timedelta(hours=2),
        )
        future_ev = self._make_fake_event(
            EventStatus.PUBLISHED,
            now + timedelta(days=3),
            now + timedelta(days=3, hours=2),
        )
        stats = _compute_user_stats([(None, past_ev), (None, future_ev)], now)  # type: ignore[arg-type]
        assert stats.last_shift is not None
        assert stats.last_shift < now
        # next_shift is resolved globally by _resolve_next_shifts, not here
        assert stats.next_shift is None

    def test_shifts_and_hours_totals(self):

        now = datetime.now(timezone.utc)
        ev1 = self._make_fake_event(
            EventStatus.COMPLETED,
            now - timedelta(hours=4),
            now - timedelta(hours=2),
        )
        ev2 = self._make_fake_event(
            EventStatus.PUBLISHED,
            now + timedelta(hours=2),
            now + timedelta(hours=4),
        )
        stats = _compute_user_stats([(None, ev1), (None, ev2)], now)  # type: ignore[arg-type]
        assert stats.shifts_total == 2
        assert stats.hours_total == Decimal("4")


# ── Own report shortcut ────────────────────────────────────────────────────────


class TestOwnReportShortcut:
    def test_own_report_redirects_to_user_report(self, app, client):
        with app.app_context():
            user = _make_user("member_own@test.com", "Member Own", Role.MEMBER)
            user_id = user.id
        _login(client, "member_own@test.com")
        resp = client.get("/reports/user", follow_redirects=False)
        assert resp.status_code == 302
        assert str(user_id) in resp.headers["Location"]

    def test_own_report_unauthenticated_redirects_to_login(self, client):
        resp = client.get("/reports/user", follow_redirects=False)
        assert resp.status_code == 302
        assert "login" in resp.headers["Location"].lower()


# ── Per-user stats in ME report ───────────────────────────────────────────────


class TestMEReportUserStats:
    def test_me_report_shows_participant_stats(self, app, client):
        with app.app_context():
            admin = _make_user("admin_mestat@test.com", "Admin MEStat", Role.ADMIN)
            member = _make_user("member_mestat@test.com", "Member MEStat", Role.MEMBER)
            me = _make_me("ME Stats Test")
            me_id = me.id
            ev = _make_event(me, "Stat Akce", EventStatus.COMPLETED)
            spot = _make_spot(ev)
            _make_assignment(spot, member, admin)
        _login(client, "admin_mestat@test.com")
        resp = client.get(f"/reports/master-event/{me_id}")
        assert resp.status_code == 200
        assert "Statistiky účastníků".encode() in resp.data
        assert b"Member MEStat" in resp.data


# ── Per-user stats in date-range report ──────────────────────────────────────


class TestDateRangeUserStats:
    def test_date_range_shows_participant_stats(self, app, client):
        with app.app_context():
            admin = _make_user("admin_drstat@test.com", "Admin DRStat", Role.ADMIN)
            member = _make_user("member_drstat@test.com", "Member DRStat", Role.MEMBER)
            me = _make_me("ME DR Stats")
            now = datetime.now(timezone.utc)
            ev = _make_event(
                me,
                "DR Stat Akce",
                EventStatus.COMPLETED,
                start=now - timedelta(days=2),
                end=now - timedelta(days=2) + timedelta(hours=3),
            )
            spot = _make_spot(ev)
            _make_assignment(spot, member, admin)
        _login(client, "admin_drstat@test.com")
        from_d = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
        to_d = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        resp = client.get(f"/reports/date-range?from_date={from_d}&to_date={to_d}")
        assert resp.status_code == 200
        assert "Statistiky účastníků".encode() in resp.data
        assert b"Member DRStat" in resp.data
