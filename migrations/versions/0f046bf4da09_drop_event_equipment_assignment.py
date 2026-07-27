"""drop event_equipment_assignment table, add index on event_equipment_plan

Revision ID: 0f046bf4da09
Revises: 08afa74293a8
Create Date: 2026-07-25 00:00:00.000000

Equipment assignment model changed from item-level (specific item → event)
to type-level (equipment type + quantity → event).  The plan table was
already the canonical source; the assignment table is no longer needed.
"""
from alembic import op
import sqlalchemy as sa

revision = '0f046bf4da09'
down_revision = '08afa74293a8'
branch_labels = None
depends_on = None


def upgrade():
    op.drop_table('event_equipment_assignment')
    op.create_index(
        'ix_event_equipment_plan_type',
        'event_equipment_plan',
        ['equipment_type_id', 'event_id'],
    )


def downgrade():
    op.drop_index('ix_event_equipment_plan_type', table_name='event_equipment_plan')
    op.create_table(
        'event_equipment_assignment',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('event_id', sa.Integer(), nullable=False),
        sa.Column('equipment_item_id', sa.Integer(), nullable=False),
        sa.Column('assigned_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('returned_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['equipment_item_id'], ['equipment_item.id']),
        sa.ForeignKeyConstraint(['event_id'], ['event.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('event_id', 'equipment_item_id', name='uq_event_equipment_item'),
    )
