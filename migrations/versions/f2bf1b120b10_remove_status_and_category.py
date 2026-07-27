"""remove equipment_item.status and equipment_type.category

Revision ID: f2bf1b120b10
Revises: e3fb60c87488
Create Date: 2026-07-26 00:00:00.000000

Availability is now derived purely from the maintenance window
(unavailability_since / unavailability_until), making the status flag
redundant.  All equipment types are shared, so the category column is
also removed.
"""
from alembic import op
import sqlalchemy as sa

revision = 'f2bf1b120b10'
down_revision = 'e3fb60c87488'
branch_labels = None
depends_on = None


def upgrade():
    op.drop_column('equipment_item', 'status')
    op.drop_column('equipment_type', 'category')


def downgrade():
    op.add_column(
        'equipment_item',
        sa.Column(
            'status',
            sa.String(length=50),
            nullable=False,
            server_default='available',
        ),
    )
    op.add_column(
        'equipment_type',
        sa.Column(
            'category',
            sa.String(length=50),
            nullable=False,
            server_default='shared',
        ),
    )
