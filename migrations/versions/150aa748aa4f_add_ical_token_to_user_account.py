"""add ical_token to user_account

Revision ID: 150aa748aa4f
Revises: 92f9337e848e
Create Date: 2026-05-13 13:08:36.240393

"""
import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision = '150aa748aa4f'
down_revision = '92f9337e848e'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('user_account', schema=None) as batch_op:
        batch_op.add_column(sa.Column('ical_token', sa.String(length=64), nullable=True))
        batch_op.create_index(batch_op.f('ix_user_account_ical_token'), ['ical_token'], unique=True)


def downgrade():
    with op.batch_alter_table('user_account', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_user_account_ical_token'))
        batch_op.drop_column('ical_token')
