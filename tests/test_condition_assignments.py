from concurrent.futures import ThreadPoolExecutor, TimeoutError
from threading import Barrier
from threading import Event as ThreadEvent
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy import event as sa_event
from sqlalchemy import text

from app.extensions import db
from app.models.assignment import Assignment
from app.models.event import Event, EventQualificationRequirement, EventStatus, StaffingMode
from app.models.qualification import Qualification
from app.models.role import Role
from app.models.user import UserAccount
from app.routes import assignments, users
from app.routes.assignments import do_assign_event, do_unassign_user
from app.routes.users import _apply_qualification_update
from app.staffing import can_join_event, condition_join_error, evaluate_staffing
from tests.conftest import _login, _make_event_with_spot, _make_user


def _condition_event(app, maximum=2):
    event_id, _ = _make_event_with_spot(app)
    with app.app_context():
        event = db.session.get(Event, event_id)
        event.spots.clear()
        event.staffing_mode = StaffingMode.CONDITIONS
        event.minimum_participants, event.maximum_participants = 1, maximum
        db.session.commit()
    return event_id


def test_capacity_claim_release_rp_and_qualification_change(app, client):
    event_id = _condition_event(app)
    with app.app_context():
        first = _make_user("first@test.com", "First", Role.MEMBER)
        second = _make_user("second@test.com", "Second", Role.MEMBER)
        third = _make_user("third@test.com", "Third", Role.MEMBER)
        qualification = Qualification(name="RP", can_be_rp=True)
        first.qualifications = second.qualifications = [qualification]
        db.session.commit()
        first_id, second_id, third_id = first.id, second.id, third.id
    _login(client, "first@test.com")
    assert client.post(f"/assignments/event/{event_id}/claim").status_code == 302
    with app.app_context(), patch("app.routes.assignments.audit"):
        event = db.session.get(Event, event_id)
        assert event.responsible_person_id == first_id
        second = db.session.get(UserAccount, second_id)
        result = do_assign_event(event_id, second, second, self_claim=True)
        assert result.ok
        second_assignment_id = result.assignment.id
        assert event.status == EventStatus.ASSIGNMENTS_CLOSED
        third = db.session.get(UserAccount, third_id)
        assert not do_assign_event(event_id, third, third, self_claim=True).ok
        assert not do_assign_event(event_id, second, second, self_claim=True).ok
        assert len(event.assignments) == 2
        # Manual choice remains restricted to eligible attendees.
        db.session.rollback()
    _login(client, "first@test.com")
    with app.test_request_context():
        with patch("app.routes.users.audit"), patch("app.routes.assignments.audit"):
            first = db.session.get(UserAccount, first_id)
            assert _apply_qualification_update(first, [])
            db.session.commit()
            event = db.session.get(Event, event_id)
            assert event.responsible_person_id == second_id
            result = do_unassign_user(db.session.get(Assignment, second_assignment_id))
            assert result.ok
            assert event.responsible_person_id is None
            assert event.status == EventStatus.ASSIGNMENTS_OPEN
            # Missing qualifications/RP never prevent joining.
            third = db.session.get(UserAccount, third_id)
            assert do_assign_event(event_id, third, third, self_claim=True).ok


@pytest.mark.parametrize("reserved", [False, True])
def test_last_seat_is_serialized_on_sql_server(app, monkeypatch, reserved):
    event_id = _condition_event(app, maximum=2 if reserved else 1)
    with app.app_context():
        if reserved:
            event = db.session.get(Event, event_id)
            event.qualification_requirements = [
                EventQualificationRequirement(
                    qualification=Qualification(name="Reserved doctor", can_be_rp=True), minimum_count=1
                )
            ]
            db.session.commit()
        ids = [_make_user(f"race{i}@test.com", f"Race {i}", Role.MEMBER).id for i in range(2)]
        engine = db.engine
    barrier = Barrier(2)
    captured = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        captured.append(statement)

    monkeypatch.setattr("app.routes.assignments.audit", lambda *a, **kw: None)
    monkeypatch.setattr("app.routes.assignments.mailer.send_assignment_confirmed", lambda *a, **kw: None)

    def claim(user_id):
        with app.app_context():
            user = db.session.get(UserAccount, user_id)
            # Deliberately preload stale state before either contender writes.
            db.session.get(Event, event_id)
            barrier.wait(timeout=10)
            return do_assign_event(event_id, user, user, self_claim=True).ok

    sa_event.listen(engine, "before_cursor_execute", capture)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, ids))
    finally:
        sa_event.remove(engine, "before_cursor_execute", capture)
    assert sorted(results) == [False, True]
    assert any("UPDLOCK, HOLDLOCK, ROWLOCK" in statement for statement in captured)
    with app.app_context():
        assert len(db.session.get(Event, event_id).assignments) == 1


def test_reservation_claim_ui_manager_and_actual_multiple_qualifications(app, client, admin_client):
    event_id = _condition_event(app, maximum=3)
    with app.app_context():
        event = db.session.get(Event, event_id)
        event.minimum_participants = 2
        doctor = Qualification(name="Lékař", can_be_rp=True)
        driver = Qualification(name="Řidič")
        event.qualification_requirements = [
            EventQualificationRequirement(qualification=q, minimum_count=1) for q in (doctor, driver)
        ]
        first = _make_user("newbie1@test.com", "Newbie 1", Role.MEMBER)
        second = _make_user("newbie2@test.com", "Newbie 2", Role.MEMBER)
        qualified = _make_user("both@test.com", "Both", Role.MEMBER)
        qualified.qualifications = [doctor, driver]
        db.session.commit()
        second_id, qualified_id = second.id, qualified.id
        assert can_join_event(event, first)
    first_client = app.test_client()
    _login(first_client, "newbie1@test.com")
    first_client.post(f"/assignments/event/{event_id}/claim")
    _login(client, "newbie2@test.com")
    claim_url = f"/assignments/event/{event_id}/claim"
    assert claim_url not in client.get(f"/events/{event_id}").data.decode()
    for response in (
        client.post(claim_url, follow_redirects=True),
        admin_client.post(
            f"/assignments/event/{event_id}/assign", data={"user_id": str(second_id)}, follow_redirects=True
        ),
    ):
        html = response.data.decode()
        assert "Zbývající místa jsou vyhrazena" in html
        assert "Lékař: 1" in html and "Řidič: 1" in html
    with app.app_context():
        event = db.session.get(Event, event_id)
        assert len(event.assignments) == 1
        assert not can_join_event(event, db.session.get(UserAccount, second_id))
    admin_client.post(f"/assignments/event/{event_id}/assign", data={"user_id": str(qualified_id)})
    assert claim_url in client.get(f"/events/{event_id}").data.decode()
    client.post(claim_url)
    with app.app_context():
        event = db.session.get(Event, event_id)
        assert len(event.assignments) == 3
        assert evaluate_staffing(event).is_staffing_sufficient


@pytest.mark.parametrize("first_coverage", [1, 2])
def test_reservation_recovery_only_requires_total_deficit_improvement(app, first_coverage):
    event_id = _condition_event(app, maximum=2)
    with app.app_context(), patch("app.routes.assignments.audit"):
        event = db.session.get(Event, event_id)
        qualifications = [Qualification(name=f"Independent {i}", can_be_rp=i == 0) for i in range(4)]
        event.qualification_requirements = [
            EventQualificationRequirement(qualification=q, minimum_count=1) for q in qualifications
        ]
        first = _make_user("recovery-first@test.com", "First", Role.MEMBER)
        second = _make_user("recovery-second@test.com", "Second", Role.MEMBER)
        unhelpful = _make_user("recovery-no-help@test.com", "No help", Role.MEMBER)
        first.qualifications = qualifications[:first_coverage]
        second.qualifications = qualifications[first_coverage:]
        unhelpful.qualifications = qualifications[:first_coverage]
        db.session.commit()
        assert can_join_event(event, first)
        assert do_assign_event(event_id, first, first, self_claim=True).ok
        assert not can_join_event(event, unhelpful)
        assert not do_assign_event(event_id, unhelpful, unhelpful, self_claim=True).ok
        assert can_join_event(event, second)
        assert do_assign_event(event_id, second, second, self_claim=True).ok
        assert evaluate_staffing(event).is_staffing_sufficient
        unhelpful.qualifications = qualifications
        assert "kapacita" in condition_join_error(event, unhelpful)
        assert not do_assign_event(event_id, unhelpful, first).ok


@pytest.mark.parametrize("state", [EventStatus.DRAFT, EventStatus.COMPLETED, EventStatus.CANCELLED])
def test_condition_claim_rejects_closed_lifecycle(app, member_client, state):
    event_id = _condition_event(app)
    with app.app_context():
        event = db.session.get(Event, event_id)
        event.status = state
        db.session.commit()
    member_client.post(f"/assignments/event/{event_id}/claim")
    with app.app_context():
        assert not db.session.get(Event, event_id).assignments


def test_condition_permissions_conflict_warning_and_manual_rp(app, admin_client):
    event_id = _condition_event(app, maximum=3)
    _, legacy_spot = _make_event_with_spot(app, name="Overlapping legacy")
    with app.app_context():
        user = _make_user("qualified@test.com", "Qualified", Role.MEMBER)
        outside = _make_user("outside@test.com", "Outside", Role.MEMBER)
        qualification = Qualification(name="RP", can_be_rp=True)
        user.qualifications = outside.qualifications = [qualification]
        db.session.add(Assignment(spot_id=legacy_spot, user=user))
        db.session.commit()
        user_id, outside_id = user.id, outside.id
    response = admin_client.post(
        f"/assignments/event/{event_id}/assign", data={"user_id": str(user_id)}, follow_redirects=True
    )
    assert "překrývající se akci" in response.data.decode()
    admin_client.post(f"/events/{event_id}/set_rp", data={"user_id": str(outside_id)})
    with app.app_context():
        assert db.session.get(Event, event_id).responsible_person_id == user_id
    admin_client.post(f"/assignments/event/{event_id}/assign", data={"user_id": str(outside_id)})
    admin_client.post(f"/events/{event_id}/set_rp", data={"user_id": str(outside_id)})
    with app.app_context():
        event = db.session.get(Event, event_id)
        assert event.responsible_person_id == outside_id
        event.status = EventStatus.ASSIGNMENTS_CLOSED
        assignment_id = event.assignments[0].id
        db.session.commit()
    admin_client.post(f"/assignments/unassign/{assignment_id}")
    with app.app_context():
        assert db.session.get(Event, event_id).status == EventStatus.ASSIGNMENTS_CLOSED


def test_qualification_update_serializes_with_release_under_rcsi(app, monkeypatch):
    event_id = _condition_event(app)
    with app.app_context():
        first = _make_user("rcsi-first@test.com", "First RP", Role.MEMBER)
        second = _make_user("rcsi-second@test.com", "Second RP", Role.MEMBER)
        qualification = Qualification(name="RCSI RP", can_be_rp=True)
        first.qualifications = second.qualifications = [qualification]
        event = db.session.get(Event, event_id)
        event.assignments = [Assignment(user=first), Assignment(user=second)]
        event.responsible_person_id = first.id
        db.session.commit()
        first_id, second_assignment_id = first.id, event.assignments[1].id
        engine = db.engine
        database = engine.url.database
        assert database.startswith("medcover_test")
        master = create_engine(engine.url.set(database="master"), isolation_level="AUTOCOMMIT")
    paused, resume, release_started = ThreadEvent(), ThreadEvent(), ThreadEvent()

    original_refresh = users.refresh_responsible_person
    original_lock = assignments.lock_condition_event

    def pause_refresh(event):
        paused.set()
        assert resume.wait(timeout=15)
        original_refresh(event)

    def release_lock(event_id):
        release_started.set()
        return original_lock(event_id)

    monkeypatch.setattr(users, "refresh_responsible_person", pause_refresh)
    monkeypatch.setattr(assignments, "lock_condition_event", release_lock)
    monkeypatch.setattr(users, "audit", lambda *a, **kw: None)
    monkeypatch.setattr(assignments, "audit", lambda *a, **kw: None)
    monkeypatch.setattr(assignments.mailer, "send_assignment_released", lambda *a, **kw: None)

    def update():
        with app.app_context():
            user = db.session.get(UserAccount, first_id)
            db.session.get(Event, event_id)  # Force a preloaded identity-map snapshot.
            assert _apply_qualification_update(user, [])
            db.session.commit()

    def release():
        with app.app_context():
            assignment = db.session.get(Assignment, second_assignment_id)
            return do_unassign_user(assignment).ok

    engine.dispose()
    with master.connect() as connection:
        was_enabled = connection.scalar(
            text("SELECT is_read_committed_snapshot_on FROM sys.databases WHERE name=:name"), {"name": database}
        )
        connection.exec_driver_sql(
            f"ALTER DATABASE [{database}] SET READ_COMMITTED_SNAPSHOT ON WITH ROLLBACK IMMEDIATE"
        )
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            updating = pool.submit(update)
            try:
                assert paused.wait(timeout=15)
                releasing = pool.submit(release)
                assert release_started.wait(timeout=15)
                # The qualification writer holds the event until its RP update commits.
                with pytest.raises(TimeoutError):
                    releasing.result(timeout=0.5)
            finally:
                resume.set()
            updating.result(timeout=15)
            assert releasing.result(timeout=15)
        with app.app_context():
            event = db.session.get(Event, event_id)
            assert len(event.assignments) == 1
            assert event.responsible_person_id is None
    finally:
        engine.dispose()
        with master.connect() as connection:
            setting = "ON" if was_enabled else "OFF"
            connection.exec_driver_sql(
                f"ALTER DATABASE [{database}] SET READ_COMMITTED_SNAPSHOT {setting} WITH ROLLBACK IMMEDIATE"
            )
        master.dispose()


def test_release_rechecks_stale_assignment_after_event_lock(app, monkeypatch):
    event_id = _condition_event(app)
    with app.app_context():
        user = _make_user("stale-release@test.com", "Stale release", Role.MEMBER)
        assignment = Assignment(event_id=event_id, user_id=user.id)
        db.session.add(assignment)
        db.session.commit()
        assignment_id = assignment.id
        original_lock = assignments.lock_condition_event

        def concurrent_release_then_lock(event_id):
            with db.engine.begin() as connection:
                connection.execute(text("DELETE FROM assignment WHERE id=:id"), {"id": assignment_id})
            return original_lock(event_id)

        monkeypatch.setattr(assignments, "lock_condition_event", concurrent_release_then_lock)
        with (
            patch.object(assignments, "audit") as audit,
            patch.object(assignments.mailer, "send_assignment_released") as mail,
        ):
            result = do_unassign_user(assignment)
            assert not result.ok
            assert "již" in result.error
            audit.assert_not_called()
            mail.assert_not_called()
        assert not result.event.assignments
