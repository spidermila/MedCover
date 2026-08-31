"""bump event.reminder_schedule default to 72h

Revision ID: d4b5c8a2f107
Revises: c3a1e7f4b2d9
Create Date: 2026-08-31 00:00:00.000000

The default unfilled-spot reminder was 24 h before start. Coordinators
asked for earlier warning so they still have time to react; the new
default is 72 h before start. Existing rows still carrying the old "24"
default (never user-set, since the field has no per-event UI) are
migrated to "72".
"""
from alembic import op

revision = 'd4b5c8a2f107'
down_revision = 'c3a1e7f4b2d9'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("UPDATE event SET reminder_schedule = '72' WHERE reminder_schedule = '24' OR reminder_schedule IS NULL")


def downgrade():
    op.execute("UPDATE event SET reminder_schedule = '24' WHERE reminder_schedule = '72'")
