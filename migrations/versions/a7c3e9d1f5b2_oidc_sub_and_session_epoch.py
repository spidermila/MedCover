"""add oidc_sub and session_epoch to user_account

Revision ID: a7c3e9d1f5b2
Revises: d5e6f7a8b9c0
Create Date: 2026-09-24 00:00:00.000000

Login through Keycloak: oidc_sub links Keycloak's back-channel logout tokens
to the user, and session_epoch lets that logout end the user's sessions.
"""

from alembic import op
import sqlalchemy as sa


revision = "a7c3e9d1f5b2"
down_revision = "d5e6f7a8b9c0"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("user_account", sa.Column("oidc_sub", sa.String(length=64), nullable=True))
    op.create_index(op.f("ix_user_account_oidc_sub"), "user_account", ["oidc_sub"], unique=False)
    op.add_column(
        "user_account",
        sa.Column("session_epoch", sa.Integer(), server_default="0", nullable=False),
    )


def downgrade():
    op.drop_column("user_account", "session_epoch", mssql_drop_default=True)
    op.drop_index(op.f("ix_user_account_oidc_sub"), table_name="user_account")
    op.drop_column("user_account", "oidc_sub")
