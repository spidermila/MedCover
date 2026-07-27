"""add unavailability_until to equipment_item

Revision ID: e3fb60c87488
Revises: 0f046bf4da09
Create Date: 2026-07-26 00:00:00.000000

Maintenance windows are now bounded: unavailability_until marks the end of
the maintenance period. NULL means indefinitely unavailable (e.g. written off).
"""
from alembic import op
import sqlalchemy as sa

revision = 'e3fb60c87488'
down_revision = '0f046bf4da09'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'equipment_item',
        sa.Column('unavailability_until', sa.DateTime(timezone=True), nullable=True),
    )


def downgrade():
    op.drop_column('equipment_item', 'unavailability_until')
