"""Cross-session optimistic-lock tests for all versioned models.

Each test loads the same row in two independent sessions, commits the first,
then verifies the second raises StaleDataError.
"""

from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker
from sqlalchemy.orm.exc import StaleDataError

from app.extensions import db
from app.models.digest import DigestBlock, DigestSchedule
from app.models.equipment import EquipmentItem, EquipmentType
from app.models.event import Event, EventSpot, EventStatus, EventTemplate
from app.models.master_event import MasterEvent
from app.models.role import Role
from app.models.user import UserAccount


def _second_session(app):
    """Return a raw SQLAlchemy session independent of Flask-SQLAlchemy's scoped session."""
    Session = sessionmaker(bind=db.engine)
    return Session()


def _make_user(session) -> UserAccount:
    role = session.scalar(sa.select(Role).where(Role.name == Role.ADMIN))
    user = UserAccount(email="olock_test@test.com", name="OLock User", is_active=True)
    user.set_password("testpass123")
    user.roles = [role]
    session.add(user)
    session.flush()
    return user


def _make_master_event(session) -> MasterEvent:
    me = MasterEvent(name="OLock ME")
    session.add(me)
    session.flush()
    return me


def _make_event(session, me: MasterEvent, user: UserAccount) -> Event:
    now = datetime.now(timezone.utc)
    event = Event(
        name="OLock Event",
        master_event_id=me.id,
        start_datetime=now + timedelta(hours=1),
        end_datetime=now + timedelta(hours=2),
        status=EventStatus.DRAFT,
        created_by_id=user.id,
        responsible_person_id=user.id,
    )
    session.add(event)
    session.flush()
    return event


class TestVersionedModelsCrossSession:
    def test_user_account_stale_raises(self, app) -> None:
        with app.app_context():
            user = _make_user(db.session)
            db.session.commit()
            user_id = user.id

        with app.app_context():
            sess_a = _second_session(app)
            sess_b = _second_session(app)
            try:
                row_a = sess_a.get(UserAccount, user_id)
                row_b = sess_b.get(UserAccount, user_id)

                row_a.name = "Name from A"
                row_a.version += 1
                sess_a.commit()

                row_b.name = "Name from B"
                row_b.version += 1
                with pytest.raises(StaleDataError):
                    sess_b.commit()
            finally:
                sess_a.close()
                sess_b.close()

    def test_master_event_stale_raises(self, app) -> None:
        with app.app_context():
            me = _make_master_event(db.session)
            db.session.commit()
            me_id = me.id

        with app.app_context():
            sess_a = _second_session(app)
            sess_b = _second_session(app)
            try:
                row_a = sess_a.get(MasterEvent, me_id)
                row_b = sess_b.get(MasterEvent, me_id)

                row_a.name = "ME from A"
                row_a.version += 1
                sess_a.commit()

                row_b.name = "ME from B"
                row_b.version += 1
                with pytest.raises(StaleDataError):
                    sess_b.commit()
            finally:
                sess_a.close()
                sess_b.close()

    def test_event_template_stale_raises(self, app) -> None:
        with app.app_context():
            tmpl = EventTemplate(name="OLock Template")
            db.session.add(tmpl)
            db.session.commit()
            tmpl_id = tmpl.id

        with app.app_context():
            sess_a = _second_session(app)
            sess_b = _second_session(app)
            try:
                row_a = sess_a.get(EventTemplate, tmpl_id)
                row_b = sess_b.get(EventTemplate, tmpl_id)

                row_a.name = "Template from A"
                row_a.version += 1
                sess_a.commit()

                row_b.name = "Template from B"
                row_b.version += 1
                with pytest.raises(StaleDataError):
                    sess_b.commit()
            finally:
                sess_a.close()
                sess_b.close()

    def test_event_stale_raises(self, app) -> None:
        with app.app_context():
            user = _make_user(db.session)
            me = _make_master_event(db.session)
            event = _make_event(db.session, me, user)
            db.session.commit()
            event_id = event.id

        with app.app_context():
            sess_a = _second_session(app)
            sess_b = _second_session(app)
            try:
                row_a = sess_a.get(Event, event_id)
                row_b = sess_b.get(Event, event_id)

                row_a.name = "Event from A"
                row_a.version += 1
                sess_a.commit()

                row_b.name = "Event from B"
                row_b.version += 1
                with pytest.raises(StaleDataError):
                    sess_b.commit()
            finally:
                sess_a.close()
                sess_b.close()

    def test_event_spot_stale_raises(self, app) -> None:
        with app.app_context():
            user = _make_user(db.session)
            me = _make_master_event(db.session)
            event = _make_event(db.session, me, user)
            spot = EventSpot(event_id=event.id, description="OLock Spot")
            db.session.add(spot)
            db.session.commit()
            spot_id = spot.id

        with app.app_context():
            sess_a = _second_session(app)
            sess_b = _second_session(app)
            try:
                row_a = sess_a.get(EventSpot, spot_id)
                row_b = sess_b.get(EventSpot, spot_id)

                row_a.description = "Spot from A"
                row_a.version += 1
                sess_a.commit()

                row_b.description = "Spot from B"
                row_b.version += 1
                with pytest.raises(StaleDataError):
                    sess_b.commit()
            finally:
                sess_a.close()
                sess_b.close()

    def test_equipment_type_stale_raises(self, app) -> None:
        with app.app_context():
            et = EquipmentType(name="OLock EquipType")
            db.session.add(et)
            db.session.commit()
            et_id = et.id

        with app.app_context():
            sess_a = _second_session(app)
            sess_b = _second_session(app)
            try:
                row_a = sess_a.get(EquipmentType, et_id)
                row_b = sess_b.get(EquipmentType, et_id)

                row_a.name = "EquipType from A"
                row_a.version += 1
                sess_a.commit()

                row_b.name = "EquipType from B"
                row_b.version += 1
                with pytest.raises(StaleDataError):
                    sess_b.commit()
            finally:
                sess_a.close()
                sess_b.close()

    def test_equipment_item_stale_raises(self, app) -> None:
        with app.app_context():
            et = EquipmentType(name="OLock ItemType")
            db.session.add(et)
            db.session.flush()
            item = EquipmentItem(name="OLock Item", type_id=et.id)
            db.session.add(item)
            db.session.commit()
            item_id = item.id

        with app.app_context():
            sess_a = _second_session(app)
            sess_b = _second_session(app)
            try:
                row_a = sess_a.get(EquipmentItem, item_id)
                row_b = sess_b.get(EquipmentItem, item_id)

                row_a.name = "Item from A"
                row_a.version += 1
                sess_a.commit()

                row_b.name = "Item from B"
                row_b.version += 1
                with pytest.raises(StaleDataError):
                    sess_b.commit()
            finally:
                sess_a.close()
                sess_b.close()

    def test_digest_schedule_stale_raises(self, app) -> None:
        from app.models.digest import get_digest_schedule  # pylint: disable=import-outside-toplevel

        with app.app_context():
            schedule = get_digest_schedule()
            db.session.commit()
            schedule_id = schedule.id

        with app.app_context():
            sess_a = _second_session(app)
            sess_b = _second_session(app)
            try:
                row_a = sess_a.get(DigestSchedule, schedule_id)
                row_b = sess_b.get(DigestSchedule, schedule_id)

                row_a.frequency_hours = 12
                row_a.version += 1
                sess_a.commit()

                row_b.frequency_hours = 48
                row_b.version += 1
                with pytest.raises(StaleDataError):
                    sess_b.commit()
            finally:
                sess_a.close()
                sess_b.close()

    def test_digest_block_stale_raises(self, app) -> None:
        from app.models.digest import get_digest_schedule  # pylint: disable=import-outside-toplevel

        with app.app_context():
            schedule = get_digest_schedule()
            block = DigestBlock(
                digest_schedule_id=schedule.id,
                block_type="free_text",
                enabled=True,
                sort_order=1,
                config_json={"title": "OLock", "content": ""},
            )
            db.session.add(block)
            db.session.commit()
            block_id = block.id

        with app.app_context():
            sess_a = _second_session(app)
            sess_b = _second_session(app)
            try:
                row_a = sess_a.get(DigestBlock, block_id)
                row_b = sess_b.get(DigestBlock, block_id)

                row_a.enabled = False
                row_a.version += 1
                sess_a.commit()

                row_b.enabled = False
                row_b.version += 1
                with pytest.raises(StaleDataError):
                    sess_b.commit()
            finally:
                sess_a.close()
                sess_b.close()


class TestDoubleBumpGuard:
    def test_stale_committed_value_raises(self, app) -> None:
        """Binding a stale form version via set_committed_value causes ORM to reject on commit."""
        from sqlalchemy.orm.attributes import set_committed_value  # pylint: disable=import-outside-toplevel

        with app.app_context():
            et = EquipmentType(name="StaleForm Type")
            db.session.add(et)
            db.session.commit()
            et_id = et.id

        with app.app_context():
            sess = _second_session(app)
            try:
                row = sess.get(EquipmentType, et_id)
                # Simulate a form submission with an old version number (stale form)
                set_committed_value(row, "version", 0)
                row.name = "Stale edit"
                row.version = 1
                with pytest.raises(StaleDataError):
                    sess.commit()
            finally:
                sess.close()
