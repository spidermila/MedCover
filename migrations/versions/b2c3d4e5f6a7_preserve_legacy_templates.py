"""Allow legacy templates to remain as read-only references.

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
"""
from alembic import op
import sqlalchemy as sa

revision = "b2c3d4e5f6a7"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade():
    # Also repair databases that applied the original NOT NULL foundation.
    # Existing condition plans retain their values; no template data is converted.
    for column in ("minimum_participants", "maximum_participants"):
        op.alter_column("event_template", column, existing_type=sa.Integer(), nullable=True)


def downgrade():
    # The corrected foundation already permits NULL legacy capacities. Keeping
    # them is necessary to preserve legacy references when returning to it.
    pass
