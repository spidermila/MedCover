import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models.assignment import Assignment, DebriefingRecord
from app.models.event import Event, EventSpot, StaffingMode
from app.models.role import Role
from tests.conftest import _make_event_with_spot, _make_user


def migration():
    path = Path("migrations/versions/9e8f7a6b5c4d_condition_staffing_foundation.py")
    spec = importlib.util.spec_from_file_location("conditions_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.op = Operations(MigrationContext.configure(db.session.connection()))
    return module


def test_assignment_constraints_and_condition_capacity(app):
    event_id, spot_id = _make_event_with_spot(app)
    with app.app_context():
        first = _make_user("first@test.com", "First", Role.MEMBER)
        second = _make_user("second@test.com", "Second", Role.MEMBER)
        event = db.session.get(Event, event_id)
        assert event.staffing_mode == StaffingMode.SPOTS
        a = Assignment(spot_id=spot_id, user_id=first.id)
        db.session.add(a)
        db.session.commit()
        assert a.event_id == event_id
        assert a.event == event
        db.session.add(Assignment(spot_id=spot_id, user_id=second.id))
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()
        db.session.delete(a)
        db.session.commit()
        event.staffing_mode = StaffingMode.CONDITIONS
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()
        event.staffing_mode = StaffingMode.CONDITIONS
        event.minimum_participants, event.maximum_participants = 1, 2
        db.session.add_all([Assignment(event_id=event_id, user_id=u.id) for u in (first, second)])
        db.session.commit()
        assert len(event.assignments) == 2
        assert all(a.spot_id is None for a in event.assignments)
        db.session.add(Assignment(event_id=event_id, user_id=first.id))
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()
        with pytest.raises(RuntimeError, match="Cannot downgrade"):
            migration().downgrade()


def test_migration_backfills_without_losing_debriefing(app):
    event_id, spot_id = _make_event_with_spot(app)
    with app.app_context():
        user = _make_user("legacy@test.com", "Legacy", Role.MEMBER)
        a = Assignment(spot_id=spot_id, user_id=user.id)
        db.session.add(a)
        db.session.flush()
        record = DebriefingRecord(assignment_id=a.id, submitted_by_id=user.id, event_note_status=1)
        db.session.add(record)
        db.session.commit()
        assignment_id, record_id = a.id, record.id
        # ORM-created FK names differ from migration-created names on MSSQL.
        fk = next(
            f for f in inspect(db.engine).get_foreign_keys("assignment") if f["constrained_columns"] == ["event_id"]
        )
        ops = migration().op
        ops.drop_constraint(fk["name"], "assignment", type_="foreignkey")
        ops.create_foreign_key("fk_assignment_event", "assignment", "event", ["event_id"], ["id"])
        migration().downgrade()
        duplicate_spot = db.session.scalar(
            text("INSERT INTO event_spot (event_id, is_optional, version) OUTPUT inserted.id VALUES (:event, 0, 1)"),
            {"event": event_id},
        )
        db.session.execute(
            text(
                "INSERT INTO assignment (spot_id, user_id, assigned_at, debriefing_email_sent) "
                "SELECT :spot, user_id, assigned_at, debriefing_email_sent FROM assignment WHERE id = :assignment"
            ),
            {"spot": duplicate_spot, "assignment": assignment_id},
        )
        with pytest.raises(RuntimeError, match="Duplicate event/user assignments"):
            migration().upgrade()
        db.session.execute(text("DELETE FROM assignment WHERE spot_id = :spot"), {"spot": duplicate_spot})
        db.session.execute(text("DELETE FROM event_spot WHERE id = :spot"), {"spot": duplicate_spot})
        db.session.execute(
            text(
                "INSERT INTO event_template (name, paid, event_type, created_at, updated_at, version) "
                "VALUES ('Old template', 0, 'MEDICAL_COVER', GETUTCDATE(), GETUTCDATE(), 1)"
            )
        )
        with pytest.raises(RuntimeError, match="Remove existing event templates"):
            migration().upgrade()
        db.session.execute(text("DELETE FROM event_template WHERE name = 'Old template'"))
        migration().upgrade()
        db.session.commit()
        db.session.expire_all()
        assert db.session.get(Assignment, assignment_id).event_id == event_id
        assert db.session.get(DebriefingRecord, record_id).assignment_id == assignment_id
        assert db.session.get(Event, event_id).staffing_mode == StaffingMode.SPOTS
        assert db.session.get(EventSpot, spot_id) is not None
