"""add is_archived to user_account

Revision ID: bbd3aa765181
Revises: c11afc34311c
Create Date: 2026-05-11 08:52:23.299286

"""
import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision = 'bbd3aa765181'
down_revision = 'c11afc34311c'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('user_account', schema=None) as batch_op:
        batch_op.add_column(sa.Column('is_archived', sa.Boolean(), server_default='false', nullable=False))


def downgrade():
    with op.batch_alter_table('user_account', schema=None) as batch_op:
        batch_op.drop_column('is_archived')
