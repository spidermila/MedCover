"""Add ical_all_token to UserAccount.

Revision ID: d697cc60c5d2
Revises: ebe3ddc11f1e
Create Date: 2026-05-30
"""
import sqlalchemy as sa
from alembic import op

revision = 'd697cc60c5d2'
down_revision = 'ebe3ddc11f1e'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('user_account', sa.Column('ical_all_token', sa.String(64), nullable=True))
    op.create_index('ix_user_account_ical_all_token', 'user_account', ['ical_all_token'], unique=True)


def downgrade():
    op.drop_index('ix_user_account_ical_all_token', table_name='user_account')
    op.drop_column('user_account', 'ical_all_token')
