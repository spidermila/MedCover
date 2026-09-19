"""Add condition staffing without removing legacy participation.

Revision ID: 9e8f7a6b5c4d
Revises: 8d9a1e2f3b4c
"""
from alembic import op
import sqlalchemy as sa

revision = "9e8f7a6b5c4d"
down_revision = "8d9a1e2f3b4c"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    duplicates = connection.execute(sa.text(
        "SELECT s.event_id, a.user_id, COUNT(*) AS assignments FROM assignment a "
        "JOIN event_spot s ON s.id = a.spot_id GROUP BY s.event_id, a.user_id HAVING COUNT(*) > 1"
    )).all()
    if duplicates:
        raise RuntimeError(f"Duplicate event/user assignments; resolve before migration: {duplicates}")
    op.add_column("event", sa.Column("staffing_mode", sa.String(10), nullable=True))
    op.execute("UPDATE event SET staffing_mode = 'SPOTS'")
    op.alter_column("event", "staffing_mode", existing_type=sa.String(10), nullable=False)
    op.add_column("event", sa.Column("minimum_participants", sa.Integer(), nullable=True))
    op.add_column("event", sa.Column("maximum_participants", sa.Integer(), nullable=True))
    op.create_check_constraint("ck_event_condition_capacity", "event",
        "staffing_mode = 'SPOTS' OR (minimum_participants IS NOT NULL AND "
        "maximum_participants IS NOT NULL AND minimum_participants >= 1 AND "
        "maximum_participants >= minimum_participants)")
    op.add_column("assignment", sa.Column("event_id", sa.Integer(), nullable=True))
    op.create_foreign_key("fk_assignment_event", "assignment", "event", ["event_id"], ["id"])
    op.execute("UPDATE a SET event_id = s.event_id FROM assignment a JOIN event_spot s ON s.id = a.spot_id")
    if connection.scalar(sa.text("SELECT COUNT(*) FROM assignment WHERE event_id IS NULL")):
        raise RuntimeError("Assignment backfill failed: orphaned assignments remain.")
    op.alter_column("assignment", "event_id", existing_type=sa.Integer(), nullable=False)
    constraints = connection.scalars(sa.text(
        "SELECT kc.name FROM sys.key_constraints kc "
        "JOIN sys.index_columns ic ON ic.object_id = kc.parent_object_id "
        "AND ic.index_id = kc.unique_index_id "
        "JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id "
        "WHERE kc.parent_object_id = OBJECT_ID('assignment') AND kc.type = 'UQ' "
        "GROUP BY kc.name HAVING COUNT(*) = 1 AND MAX(c.name) = 'spot_id'"
    )).all()
    for name in constraints:
        op.drop_constraint(name, "assignment", type_="unique")
    op.alter_column("assignment", "spot_id", existing_type=sa.Integer(), nullable=True)
    op.create_index("ix_assignment_spot_unique", "assignment", ["spot_id"], unique=True,
                    mssql_where=sa.text("spot_id IS NOT NULL"))
    op.create_index("ix_assignment_event_id", "assignment", ["event_id"])
    op.create_unique_constraint("uq_assignment_event_user", "assignment", ["event_id", "user_id"])
    for column in ("minimum_participants", "maximum_participants"):
        op.add_column("event_template", sa.Column(column, sa.Integer(), nullable=True))
    for table, owner, key, short in (
        ("event_qualification_requirement", "event", "event_id", "event"),
        ("event_template_qualification_requirement", "event_template", "template_id", "template"),
    ):
        op.create_table(table,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(key, sa.Integer(), sa.ForeignKey(f"{owner}.id", ondelete="CASCADE"), nullable=False),
            sa.Column("qualification_id", sa.Integer(), sa.ForeignKey("qualification.id"), nullable=False),
            sa.Column("minimum_count", sa.Integer(), nullable=False),
            sa.UniqueConstraint(key, "qualification_id", name=f"uq_{short}_requirement"),
            sa.CheckConstraint("minimum_count > 0", name=f"ck_{short}_requirement_minimum"),
        )


def downgrade():
    connection = op.get_bind()
    if connection.scalar(sa.text("SELECT COUNT(*) FROM event WHERE staffing_mode = 'CONDITIONS'")):
        raise RuntimeError("Cannot downgrade: condition events would lose their plan and participation.")
    if connection.scalar(sa.text("SELECT COUNT(*) FROM event_template WHERE minimum_participants IS NOT NULL "
                                 "OR maximum_participants IS NOT NULL")) or connection.scalar(
            sa.text("SELECT COUNT(*) FROM event_template_qualification_requirement")):
        raise RuntimeError("Cannot downgrade: condition templates would lose their plan.")
    op.drop_table("event_template_qualification_requirement")
    op.drop_table("event_qualification_requirement")
    for column in ("minimum_participants", "maximum_participants"):
        op.drop_column("event_template", column)
    op.drop_constraint("uq_assignment_event_user", "assignment", type_="unique")
    op.drop_index("ix_assignment_spot_unique", table_name="assignment")
    op.drop_index("ix_assignment_event_id", table_name="assignment")
    op.alter_column("assignment", "spot_id", existing_type=sa.Integer(), nullable=False)
    op.create_unique_constraint("uq_assignment_spot_id", "assignment", ["spot_id"])
    op.drop_constraint("fk_assignment_event", "assignment", type_="foreignkey")
    op.drop_column("assignment", "event_id")
    op.drop_constraint("ck_event_condition_capacity", "event", type_="check")
    for column in ("minimum_participants", "maximum_participants", "staffing_mode"):
        op.drop_column("event", column)
