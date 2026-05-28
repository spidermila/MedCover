"""add instance_name to outbox_email

Revision ID: 92f9337e848e
Revises: ca37e9989a8a
Create Date: 2026-05-13 12:39:55.730349

"""
import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision = '92f9337e848e'
down_revision = 'ca37e9989a8a'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('outbox_email', schema=None) as batch_op:
        batch_op.add_column(sa.Column('instance_name', sa.String(length=64), nullable=True))
        batch_op.create_index(batch_op.f('ix_outbox_email_instance_name'), ['instance_name'], unique=False)


def downgrade():
    with op.batch_alter_table('outbox_email', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_outbox_email_instance_name'))
        batch_op.drop_column('instance_name')
