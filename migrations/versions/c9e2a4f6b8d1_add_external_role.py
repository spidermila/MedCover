"""add the External role

Revision ID: c9e2a4f6b8d1
Revises: b8d4f0e2a6c3
Create Date: 2026-09-29 00:00:00.000000

External users hold only this role (given in the MemberBase directory) and
see only the events they are assigned to or responsible for.
"""

from alembic import op

revision = "c9e2a4f6b8d1"
down_revision = "b8d4f0e2a6c3"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("INSERT INTO role (name) SELECT 'External' WHERE NOT EXISTS (SELECT 1 FROM role WHERE name = 'External')")


def downgrade():
    op.execute("DELETE FROM user_roles WHERE role_id IN (SELECT id FROM role WHERE name = 'External')")
    op.execute("DELETE FROM role WHERE name = 'External'")
