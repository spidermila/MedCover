"""replace debriefing grade with event note status

Revision ID: c09d6edb1a24
Revises: 1d73232b0f18
Create Date: 2026-08-30 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "c09d6edb1a24"
down_revision = "1d73232b0f18"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("debriefing_record", schema=None) as batch_op:
        batch_op.alter_column("grade", new_column_name="event_note_status", existing_type=sa.Integer())
    op.execute(
        "UPDATE debriefing_record SET event_note_status = 0 "
        "WHERE feedback_event LIKE '%importovaný historický dozor%'"
    )


def downgrade():
    with op.batch_alter_table("debriefing_record", schema=None) as batch_op:
        batch_op.alter_column("event_note_status", new_column_name="grade", existing_type=sa.Integer())
