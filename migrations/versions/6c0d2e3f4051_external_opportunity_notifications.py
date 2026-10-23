"""Add the external opportunity notification toggle.

Revision ID: 6c0d2e3f4051
Revises: 5b9c1d2e3f40
"""
from alembic import op
import sqlalchemy as sa

revision = '6c0d2e3f4051'
down_revision = '5b9c1d2e3f40'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('app_settings', sa.Column('notify_external_opportunities', sa.Boolean(), nullable=False, server_default=sa.true()))


def downgrade():
    op.drop_column('app_settings', 'notify_external_opportunities', mssql_drop_default=True)
