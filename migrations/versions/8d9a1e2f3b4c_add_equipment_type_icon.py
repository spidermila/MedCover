"""add icon to equipment type

Revision ID: 8d9a1e2f3b4c
Revises: d4b5c8a2f107
Create Date: 2026-08-31 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "8d9a1e2f3b4c"
down_revision = "d4b5c8a2f107"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "equipment_type",
        sa.Column("icon", sa.Unicode(length=16), server_default=sa.text("N'📦'"), nullable=False),
    )
    op.alter_column("equipment_type", "icon", existing_type=sa.Unicode(length=16), server_default=None)


def downgrade():
    op.drop_column("equipment_type", "icon")
