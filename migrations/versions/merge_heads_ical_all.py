"""merge heads: ical_all_token + equipment item status

Revision ID: a1f2e3d4c5b6
Revises: b801953d30cb, d697cc60c5d2
Create Date: 2026-05-30 10:00:00.000000

"""
import sqlalchemy as sa  # noqa: F401
from alembic import op  # noqa: F401


# revision identifiers, used by Alembic.
revision = 'a1f2e3d4c5b6'
down_revision = ('b801953d30cb', 'd697cc60c5d2')
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
