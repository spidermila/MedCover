"""Add login lockout fields to user_account.

Revision ID: c7d8e9f0a1b2
Revises: 1613fcb025fb
Create Date: 2026-05-10 15:00:00.000000

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c7d8e9f0a1b2"
down_revision = "1613fcb025fb"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_account",
        sa.Column(
            "failed_login_attempts",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "user_account",
        sa.Column(
            "login_locked_until",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("user_account", "login_locked_until")
    op.drop_column("user_account", "failed_login_attempts")
