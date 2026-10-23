"""Allow opening an event to externals before the automatic ten-day window.

Revision ID: 5b9c1d2e3f40
Revises: 7d1e3f405162
"""
from alembic import op
import sqlalchemy as sa

revision = '5b9c1d2e3f40'
down_revision = '7d1e3f405162'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('event', sa.Column('open_to_externals', sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade():
    op.drop_column('event', 'open_to_externals', mssql_drop_default=True)
