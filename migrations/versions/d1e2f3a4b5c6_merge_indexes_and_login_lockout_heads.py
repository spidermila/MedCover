"""Merge indexes and login-lockout migration heads.

Revision ID: d1e2f3a4b5c6
Revises: b1c2d3e4f5a6, c7d8e9f0a1b2
Create Date: 2026-05-10 15:32:00.000000

"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "d1e2f3a4b5c6"
down_revision = ("b1c2d3e4f5a6", "c7d8e9f0a1b2")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
