"""split notify_event_lifecycle into notify_event_published and notify_assignments_opened

Revision ID: ca37e9989a8a
Revises: 9b422409dc10
Create Date: 2026-05-13 08:36:58.895145

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "ca37e9989a8a"
down_revision = "9b422409dc10"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("app_settings", schema=None) as batch_op:
        batch_op.add_column(sa.Column("notify_event_published", sa.Boolean(), server_default="true", nullable=False))
        batch_op.add_column(sa.Column("notify_assignments_opened", sa.Boolean(), server_default="true", nullable=False))
        batch_op.drop_column("notify_event_lifecycle")


def downgrade():
    with op.batch_alter_table("app_settings", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "notify_event_lifecycle",
                sa.BOOLEAN(),
                server_default=sa.text("true"),
                autoincrement=False,
                nullable=False,
            )
        )
        batch_op.drop_column("notify_assignments_opened")
        batch_op.drop_column("notify_event_published")
