"""Store qualifications granted by registration invitations."""

from alembic import op
import sqlalchemy as sa

revision = "e6f7a8b9c0d1"
down_revision = "d5e6f7a8b9c0"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "invite_qualifications",
        sa.Column("invite_id", sa.Integer(), nullable=False),
        sa.Column("qualification_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["invite_id"], ["registration_invite.id"]),
        sa.ForeignKeyConstraint(["qualification_id"], ["qualification.id"]),
        sa.PrimaryKeyConstraint("invite_id", "qualification_id"),
    )


def downgrade():
    op.drop_table("invite_qualifications")
