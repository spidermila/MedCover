"""Tests for individual digest block collect() methods."""

from datetime import datetime, timedelta, timezone

import sqlalchemy as sa

from app.digest.blocks.audit_log import AuditLogBlock
from app.digest.blocks.backup_status import BackupStatusBlock
from app.digest.blocks.feedback_summary import FeedbackSummaryBlock
from app.digest.blocks.new_users import NewUsersBlock
from app.digest.blocks.upcoming_events import UpcomingEventsBlock
from app.digest.blocks.user_activity import UserActivityBlock
from app.extensions import db
from app.models.audit import AuditLogEntry
from app.models.event import Event, EventStatus
from app.models.master_event import MasterEvent
from app.models.role import Role
from app.models.settings import get_settings
from tests.conftest import _make_user


def _ensure_master_event() -> int:
    """Get or create a MasterEvent, return its ID."""
    me = db.session.scalars(sa.select(MasterEvent).limit(1)).first()
    if me:
        return me.id
    me = MasterEvent(name="Digest Test ME")
    db.session.add(me)
    db.session.flush()
    return me.id


def _make_event(name: str, hours_ahead: int = 24, status: EventStatus = EventStatus.PUBLISHED) -> Event:
    """Create a published event starting hours_ahead from now."""
    me_id = _ensure_master_event()
    start = datetime.now(timezone.utc) + timedelta(hours=hours_ahead)
    end = start + timedelta(hours=2)
    ev = Event(
        name=name,
        start_datetime=start,
        end_datetime=end,
        status=status,
        archived=False,
        master_event_id=me_id,
    )
    db.session.add(ev)
    return ev


class TestUpcomingEventsBlock:
    def test_collect_returns_upcoming_events(self, app):
        with app.app_context():
            _make_event("Akce za 2 dny", hours_ahead=48)
            _make_event("Akce za 10 dní", hours_ahead=240)  # outside 7-day window
            db.session.commit()

            block = UpcomingEventsBlock()
            result = block.collect(db.session, block.default_config)
            assert result["title"] == "Nadcházející akce"
            assert result["days_ahead"] == 7
            # Only the one within 7 days
            names = [r["event"].name for r in result["rows"]]
            assert "Akce za 2 dny" in names
            assert "Akce za 10 dní" not in names

    def test_collect_excludes_archived(self, app):
        with app.app_context():
            ev = _make_event("Archivovaná", hours_ahead=24)
            ev.archived = True
            db.session.commit()

            block = UpcomingEventsBlock()
            result = block.collect(db.session, block.default_config)
            names = [r["event"].name for r in result["rows"]]
            assert "Archivovaná" not in names

    def test_collect_unfilled_only(self, app):
        with app.app_context():
            _make_event("Bez pozic", hours_ahead=24)
            db.session.commit()

            block = UpcomingEventsBlock()
            config = {**block.default_config, "show_unfilled_only": True}
            result = block.collect(db.session, config)
            # Event has no spots, so unfilled_count == 0, should be excluded
            assert len(result["rows"]) == 0


class TestAuditLogBlock:
    def test_collect_returns_recent_entries(self, app):
        with app.app_context():
            entry = AuditLogEntry(
                actor_id=None,
                action_type="create",
                entity_type="Event",
                entity_id="test-id",
                summary="Test event created",
                timestamp=datetime.now(timezone.utc),
            )
            db.session.add(entry)
            db.session.commit()

            block = AuditLogBlock()
            result = block.collect(db.session, block.default_config)
            assert result["title"] == "Audit log"
            assert len(result["entries"]) >= 1
            assert result["entries"][0].summary == "Test event created"

    def test_collect_filters_by_entity_type(self, app):
        with app.app_context():
            db.session.add(
                AuditLogEntry(
                    actor_id=None,
                    action_type="create",
                    entity_type="Event",
                    entity_id="1",
                    summary="ev",
                    timestamp=datetime.now(timezone.utc),
                )
            )
            db.session.add(
                AuditLogEntry(
                    actor_id=None,
                    action_type="create",
                    entity_type="UserAccount",
                    entity_id="2",
                    summary="user",
                    timestamp=datetime.now(timezone.utc),
                )
            )
            db.session.commit()

            block = AuditLogBlock()
            config = {**block.default_config, "entity_types": ["Event"]}
            result = block.collect(db.session, config)
            assert all(e.entity_type == "Event" for e in result["entries"])

    def test_collect_filters_by_action_type(self, app):
        with app.app_context():
            db.session.add(
                AuditLogEntry(
                    actor_id=None,
                    action_type="delete",
                    entity_type="Event",
                    entity_id="1",
                    summary="deleted",
                    timestamp=datetime.now(timezone.utc),
                )
            )
            db.session.commit()

            block = AuditLogBlock()
            config = {**block.default_config, "action_types": ["delete"]}
            result = block.collect(db.session, config)
            assert all(e.action_type == "delete" for e in result["entries"])


class TestNewUsersBlock:
    def test_collect_returns_recent_users(self, app):
        with app.app_context():
            u = _make_user("new@test.cz", "Nový Uživatel", Role.MEMBER)
            u.created_at = datetime.now(timezone.utc) - timedelta(hours=1)
            db.session.commit()

            block = NewUsersBlock()
            result = block.collect(db.session, block.default_config)
            assert result["title"] == "Noví uživatelé"
            names = [user.name for user in result["users"]]
            assert "Nový Uživatel" in names

    def test_collect_pending_only(self, app):
        with app.app_context():
            u = _make_user("pending@test.cz", "Čekající", Role.MEMBER)
            u.is_active = False
            u.created_at = datetime.now(timezone.utc) - timedelta(hours=1)
            db.session.commit()

            block = NewUsersBlock()
            config = {**block.default_config, "show_pending_only": True}
            result = block.collect(db.session, config)
            names = [user.name for user in result["users"]]
            assert "Čekající" in names


class TestUserActivityBlock:
    def test_collect_returns_activity_counts(self, app):
        with app.app_context():
            u = _make_user("active@test.cz", "Aktivní", Role.ADMIN)
            for i in range(3):
                db.session.add(
                    AuditLogEntry(
                        actor_id=u.id,
                        action_type="edit",
                        entity_type="Event",
                        entity_id=str(i),
                        summary=f"edit {i}",
                        timestamp=datetime.now(timezone.utc),
                    )
                )
            db.session.commit()

            block = UserActivityBlock()
            result = block.collect(db.session, block.default_config)
            assert result["title"] == "Aktivita uživatelů"
            # Should have at least one entry for our user
            assert len(result["entries"]) >= 1
            entry = next((e for e in result["entries"] if e["name"] == "Aktivní"), None)
            assert entry is not None
            assert entry["count"] >= 3


class TestFeedbackSummaryBlock:
    def test_collect_empty(self, app):
        with app.app_context():
            block = FeedbackSummaryBlock()
            result = block.collect(db.session, block.default_config)
            assert result["title"] == "Zpětná vazba"
            assert result["items"] == []
            assert result["truncated"] is False


class TestBackupStatusBlock:
    def test_collect_returns_backup_info(self, app, tmp_path):
        with app.app_context():
            settings = get_settings()
            settings.backup_dir = str(tmp_path)
            db.session.commit()

            block = BackupStatusBlock()
            result = block.collect(db.session, block.default_config)
            assert result["title"] == "Stav zálohování"
            assert result["backup_count"] == 0
            assert result["total_size_bytes"] == 0
            assert result["last_backup_at"] is None
            assert result["last_backup_age_hours"] is None
