"""Remember whether capacity closed condition-event registration.

Revision ID: a1b2c3d4e5f6
Revises: 9e8f7a6b5c4d
"""
from alembic import op
import sqlalchemy as sa

revision = "a1b2c3d4e5f6"
down_revision = "9e8f7a6b5c4d"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("event", sa.Column("capacity_closed", sa.Boolean(), nullable=False, server_default="0"))


def downgrade():
    op.drop_column("event", "capacity_closed", mssql_drop_default=True)
