"""add last_login_at to user_account

Revision ID: 661e4a600825
Revises: 904fa21b5ed3
Create Date: 2026-05-12 18:41:07.623913

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "661e4a600825"
down_revision = "904fa21b5ed3"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("user_account", schema=None) as batch_op:
        batch_op.add_column(sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True))


def downgrade():
    with op.batch_alter_table("user_account", schema=None) as batch_op:
        batch_op.drop_column("last_login_at")
