"""drop event_template.reminder_schedule

Revision ID: c3a1e7f4b2d9
Revises: c09d6edb1a24
Create Date: 2026-08-31 00:00:00.000000

The reminder_schedule column on event_template was never consumed:
creating an Event from a template did not copy it, and the scheduler
reads Event.reminder_schedule (which stays). The field only cluttered
the template form, so remove it.
"""
from alembic import op
import sqlalchemy as sa

revision = 'c3a1e7f4b2d9'
down_revision = 'c09d6edb1a24'
branch_labels = None
depends_on = None


def upgrade():
    op.drop_column('event_template', 'reminder_schedule')


def downgrade():
    op.add_column(
        'event_template',
        sa.Column('reminder_schedule', sa.String(length=255), nullable=True),
    )
