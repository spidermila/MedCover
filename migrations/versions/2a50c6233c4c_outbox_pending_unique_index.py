"""filtered unique index on outbox_email pending rows

Revision ID: 2a50c6233c4c
Revises: b030510869ff
Create Date: 2026-07-27 00:00:00.000000

Enforces at most one pending outbox row per
(user_id, event_id, notification_type) — the invariant enqueue_deferred
already assumes. Pre-collapses any pre-existing duplicates so the
constraint can be built.
"""
from alembic import op
import sqlalchemy as sa


revision = '2a50c6233c4c'
down_revision = 'b030510869ff'
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        """
        WITH ranked AS (
            SELECT id,
                   ROW_NUMBER() OVER (
                       PARTITION BY user_id, event_id, notification_type
                       ORDER BY created_at DESC, id DESC
                   ) AS rn
            FROM outbox_email
            WHERE status = 'pending'
              AND user_id IS NOT NULL
              AND event_id IS NOT NULL
              AND notification_type IS NOT NULL
        )
        DELETE FROM outbox_email
        WHERE id IN (SELECT id FROM ranked WHERE rn > 1)
        """
    )
    op.create_index(
        "uq_outbox_pending_by_user_event_type",
        "outbox_email",
        ["user_id", "event_id", "notification_type"],
        unique=True,
        mssql_where=sa.text(
            "status = 'pending' "
            "AND user_id IS NOT NULL "
            "AND event_id IS NOT NULL "
            "AND notification_type IS NOT NULL"
        ),
    )


def downgrade():
    op.drop_index("uq_outbox_pending_by_user_event_type", table_name="outbox_email")
