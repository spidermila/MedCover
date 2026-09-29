"""Merge invitation qualifications and event time format migrations.

Keep both published revision histories intact so databases upgraded on either
branch can acquire the other feature without reapplying their existing schema.
"""

revision = "f7a8b9c0d1e2"
down_revision = ("e6f7a8b9c0d1", "a7c3e9f1d2b4")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
