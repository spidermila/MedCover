"""add performance indexes on event, event_spot, assignment

Revision ID: b1c2d3e4f5a6
Revises: a2f3c4d5e6f7
Create Date: 2026-05-10 14:10:00.000000

Indexes added:
  - event(status)           – filtered in events list, dashboard candidates query
  - event(start_datetime)   – ORDER BY in events list and ME report
  - event(master_event_id)  – ME report WHERE clause
  - event(archived)         – nearly every event query filters on this
  - event_spot(event_id)    – selectin loads by SQLAlchemy for spots relationship
  - assignment(user_id)     – dashboard pending-debriefings filter

Also adds a composite covering index (archived, status, start_datetime) that
satisfies the events-list hot-path query in a single index-only scan.
"""
from alembic import op


# revision identifiers, used by Alembic.
revision = "b1c2d3e4f5a6"
down_revision = "a2f3c4d5e6f7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_event_status", "event", ["status"])
    op.create_index("ix_event_start_datetime", "event", ["start_datetime"])
    op.create_index("ix_event_master_event_id", "event", ["master_event_id"])
    op.create_index("ix_event_archived", "event", ["archived"])
    op.create_index(
        "ix_event_archived_status_start",
        "event",
        ["archived", "status", "start_datetime"],
    )
    op.create_index("ix_event_spot_event_id", "event_spot", ["event_id"])
    op.create_index("ix_assignment_user_id", "assignment", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_assignment_user_id", table_name="assignment")
    op.drop_index("ix_event_spot_event_id", table_name="event_spot")
    op.drop_index("ix_event_archived_status_start", table_name="event")
    op.drop_index("ix_event_archived", table_name="event")
    op.drop_index("ix_event_master_event_id", table_name="event")
    op.drop_index("ix_event_start_datetime", table_name="event")
    op.drop_index("ix_event_status", table_name="event")
