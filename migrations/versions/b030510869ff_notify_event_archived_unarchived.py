"""notify_event_archived and notify_event_unarchived

Revision ID: b030510869ff
Revises: 853f463b9f87
Create Date: 2026-07-27 00:00:00.000000

Adds two NOT NULL boolean toggle columns to app_settings for the new
event_archived / event_unarchived notification categories (defaults
"true").
"""
from alembic import op
import sqlalchemy as sa


revision = 'b030510869ff'
down_revision = '853f463b9f87'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('app_settings', schema=None) as batch_op:
        batch_op.add_column(sa.Column('notify_event_archived', sa.Boolean(),
                                      server_default='true', nullable=False))
        batch_op.add_column(sa.Column('notify_event_unarchived', sa.Boolean(),
                                      server_default='true', nullable=False))


def downgrade():
    with op.batch_alter_table('app_settings', schema=None) as batch_op:
        batch_op.drop_column('notify_event_unarchived', mssql_drop_default=True)
        batch_op.drop_column('notify_event_archived', mssql_drop_default=True)
