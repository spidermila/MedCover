"""add work_report.generate permission

Revision ID: 953cffa1cb85
Revises: 20dbd30fbcfe
Create Date: 2026-05-10 01:10:00.000000

Inserts the new 'work_report.generate' permission and assigns it to the
Admin, Coordinator, and Member roles.  Viewer and Debriefing Manager
roles do NOT receive this permission.
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = '953cffa1cb85'
down_revision = '20dbd30fbcfe'
branch_labels = None
depends_on = None

# Roles that may generate a výkaz práce
_ALLOWED_ROLES = ("Admin", "Coordinator", "Member")


def upgrade() -> None:
    conn = op.get_bind()

    # Insert permission (idempotent: skip if already present)
    existing = conn.execute(
        sa.text("SELECT id FROM permission WHERE code = 'work_report.generate'")
    ).fetchone()

    if existing is None:
        conn.execute(
            sa.text(
                "INSERT INTO permission (code, description) "
                "VALUES ('work_report.generate', 'Generate own monthly work report (výkaz práce)')"
            )
        )

    # Assign permission to allowed roles
    perm_row = conn.execute(
        sa.text("SELECT id FROM permission WHERE code = 'work_report.generate'")
    ).fetchone()
    perm_id = perm_row[0]

    for role_name in _ALLOWED_ROLES:
        role_row = conn.execute(
            sa.text("SELECT id FROM role WHERE name = :name"),
            {"name": role_name},
        ).fetchone()
        if role_row is None:
            continue
        role_id = role_row[0]
        already = conn.execute(
            sa.text(
                "SELECT 1 FROM role_permissions "
                "WHERE role_id = :rid AND permission_id = :pid"
            ),
            {"rid": role_id, "pid": perm_id},
        ).fetchone()
        if already is None:
            conn.execute(
                sa.text(
                    "INSERT INTO role_permissions (role_id, permission_id) "
                    "VALUES (:rid, :pid)"
                ),
                {"rid": role_id, "pid": perm_id},
            )


def downgrade() -> None:
    conn = op.get_bind()
    perm_row = conn.execute(
        sa.text("SELECT id FROM permission WHERE code = 'work_report.generate'")
    ).fetchone()
    if perm_row is None:
        return
    perm_id = perm_row[0]
    conn.execute(
        sa.text("DELETE FROM role_permissions WHERE permission_id = :pid"),
        {"pid": perm_id},
    )
    conn.execute(
        sa.text("DELETE FROM permission WHERE id = :pid"),
        {"pid": perm_id},
    )
