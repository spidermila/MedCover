"""rename preferred_hour_utc to preferred_hour in digest_schedule

Revision ID: 904fa21b5ed3
Revises: bbd3aa765181
Create Date: 2026-05-12 09:58:18.146596

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "904fa21b5ed3"
down_revision = "bbd3aa765181"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("digest_schedule", schema=None) as batch_op:
        batch_op.add_column(sa.Column("preferred_hour", sa.Integer(), server_default="7", nullable=False))
        batch_op.drop_column("preferred_hour_utc")


def downgrade():
    with op.batch_alter_table("digest_schedule", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "preferred_hour_utc", sa.INTEGER(), server_default=sa.text("7"), autoincrement=False, nullable=False
            )
        )
        batch_op.drop_column("preferred_hour")
