"""add backup_schedule_minute to app_settings

Revision ID: b2d5f9c8e7a3
Revises: a1c4e8b7d2f6
Create Date: 2026-09-04 00:00:00.000000

The scheduled-backup trigger used to be hour-only ("run daily at hour H").
Coordinators asked for finer control (e.g. 02:30 instead of just 02:00), so
a companion minute column is added. Together with the existing
``backup_schedule_hour`` this stores an HH:MM daily trigger time in the
app's configured timezone.

Existing rows default to minute 0, which preserves the previous behaviour
(hour H maps to HH:00).
"""

from alembic import op
import sqlalchemy as sa


revision = "b2d5f9c8e7a3"
down_revision = "a1c4e8b7d2f6"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "app_settings",
        sa.Column("backup_schedule_minute", sa.Integer(), server_default="0", nullable=False),
    )


def downgrade():
    # MSSQL attaches DEFAULTs as named constraints; the column can't be dropped
    # while a constraint depends on it, so drop the constraint by lookup first.
    op.execute(
        """
        DECLARE @cn sysname;
        SELECT @cn = dc.name
        FROM sys.default_constraints dc
        JOIN sys.columns c ON c.default_object_id = dc.object_id
        WHERE dc.parent_object_id = OBJECT_ID('app_settings')
          AND c.name = 'backup_schedule_minute';
        IF @cn IS NOT NULL EXEC('ALTER TABLE app_settings DROP CONSTRAINT ' + @cn);
        """
    )
    op.drop_column("app_settings", "backup_schedule_minute")
