"""add event_time_format to user_account

Revision ID: a7c3e9f1d2b4
Revises: d5e6f7a8b9c0
Create Date: 2026-09-23 00:00:00.000000

Per-user choice of how event times are displayed (1 = start + duration,
2 = start–end). Existing users keep the current start + duration format.
"""
from alembic import op
import sqlalchemy as sa

revision = 'a7c3e9f1d2b4'
down_revision = 'd5e6f7a8b9c0'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'user_account',
        sa.Column('event_time_format', sa.SmallInteger(), server_default='1', nullable=False),
    )


def downgrade():
    # MSSQL keeps the DEFAULT as a named constraint that blocks the drop.
    op.drop_column('user_account', 'event_time_format', mssql_drop_default=True)
