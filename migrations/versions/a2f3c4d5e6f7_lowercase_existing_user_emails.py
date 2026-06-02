"""lowercase existing user emails

Revision ID: a2f3c4d5e6f7
Revises: 953cffa1cb85
Create Date: 2026-05-10 10:00:00.000000

"""
from alembic import op


# revision identifiers, used by Alembic.
revision = "a2f3c4d5e6f7"
down_revision = "953cffa1cb85"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE user_account SET email = LOWER(email) WHERE email != LOWER(email)")


def downgrade() -> None:
    pass
