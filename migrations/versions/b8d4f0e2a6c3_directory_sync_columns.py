"""add directory-sync columns to user_account and qualification

Revision ID: b8d4f0e2a6c3
Revises: a7c3e9d1f5b2
Create Date: 2026-09-26 00:00:00.000000

The user sync copies each person's Místní skupina and kind from the
MemberBase directory and matches qualifications by crcQualificationId. The
export to the directory derived that ID from the MedCover ID, so existing
qualifications get the same value here.
"""

import uuid

import sqlalchemy as sa
from alembic import op

revision = "b8d4f0e2a6c3"
down_revision = "a7c3e9d1f5b2"
branch_labels = None
depends_on = None

# Same namespace as scripts/export_memberbase.py: crcQualificationId = uuid5(namespace, MedCover id).
QUALIFICATION_NAMESPACE = uuid.UUID("11fac960-c2df-46cd-a74e-686fbe95cf6c")


def upgrade():
    op.add_column("user_account", sa.Column("crc_unit_id", sa.String(length=64), nullable=True))
    op.add_column("user_account", sa.Column("unit_name", sa.String(length=255), nullable=True))
    op.add_column("user_account", sa.Column("kind", sa.String(length=16), nullable=True))
    op.add_column("qualification", sa.Column("crc_qualification_id", sa.String(length=36), nullable=True))
    conn = op.get_bind()
    for (qual_id,) in conn.execute(sa.text("SELECT id FROM qualification")).all():
        conn.execute(
            sa.text("UPDATE qualification SET crc_qualification_id = :crc WHERE id = :id"),
            {"crc": str(uuid.uuid5(QUALIFICATION_NAMESPACE, str(qual_id))), "id": qual_id},
        )
    op.create_index(
        "ix_qualification_crc_qualification_id",
        "qualification",
        ["crc_qualification_id"],
        unique=True,
        mssql_where=sa.text("crc_qualification_id IS NOT NULL"),
    )


def downgrade():
    op.drop_index("ix_qualification_crc_qualification_id", table_name="qualification")
    op.drop_column("qualification", "crc_qualification_id")
    op.drop_column("user_account", "kind")
    op.drop_column("user_account", "unit_name")
    op.drop_column("user_account", "crc_unit_id")
