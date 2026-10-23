"""Persist the external opportunity window in application settings.

Revision ID: 7d1e3f405162
Revises: 4a8b0c1d2e3f
"""
from alembic import op
import sqlalchemy as sa

revision = '7d1e3f405162'
down_revision = '4a8b0c1d2e3f'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('app_settings', sa.Column('external_opportunity_days', sa.Integer(), nullable=False, server_default='10'))


def downgrade():
    op.drop_column('app_settings', 'external_opportunity_days', mssql_drop_default=True)
