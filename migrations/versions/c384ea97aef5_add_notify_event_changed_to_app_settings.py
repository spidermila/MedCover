"""add notify_event_changed to app_settings

Revision ID: c384ea97aef5
Revises: 9d1ee14eb241
Create Date: 2026-05-10 23:40:53.948590

"""
import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision = 'c384ea97aef5'
down_revision = '9d1ee14eb241'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('app_settings', schema=None) as batch_op:
        batch_op.add_column(sa.Column('notify_event_changed', sa.Boolean(), server_default='true', nullable=False))


def downgrade():
    with op.batch_alter_table('app_settings', schema=None) as batch_op:
        batch_op.drop_column('notify_event_changed')
