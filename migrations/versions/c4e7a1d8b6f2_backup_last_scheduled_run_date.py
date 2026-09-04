"""add backup_last_scheduled_run_date to app_settings

Revision ID: c4e7a1d8b6f2
Revises: b2d5f9c8e7a3
Create Date: 2026-09-04 00:00:00.000000

The scheduled-backup dedupe key used to be "any backup file already exists
for today's local date", derived by scanning the backup directory. That
made an ad-hoc admin-triggered backup suppress the scheduled run for the
same day — coordinators asked for the opposite: the scheduled backup must
fire at the scheduled time regardless of any ad-hoc runs, while still
producing at most one *scheduled* backup per day.

Track the last-fired scheduled-run local date in AppSettings so the guard
is independent of filesystem contents. Nullable to keep the first tick
after upgrade unambiguous.
"""

from alembic import op
import sqlalchemy as sa


revision = "c4e7a1d8b6f2"
down_revision = "b2d5f9c8e7a3"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "app_settings",
        sa.Column("backup_last_scheduled_run_date", sa.Date(), nullable=True),
    )


def downgrade():
    op.drop_column("app_settings", "backup_last_scheduled_run_date")
