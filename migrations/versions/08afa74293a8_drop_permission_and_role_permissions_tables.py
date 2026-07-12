"""drop permission and role_permissions tables

Revision ID: 08afa74293a8
Revises: 68fee3388efa
Create Date: 2026-07-12 00:00:00.000000

Permissions are now resolved entirely from the ROLE_PERMISSIONS dict in
app/models/role.py. The permission and role_permissions DB tables are no
longer needed.
"""
from alembic import op
import sqlalchemy as sa

revision = '08afa74293a8'
down_revision = '2c159bca01be'
branch_labels = None
depends_on = None


def upgrade():
    op.drop_table('role_permissions')
    op.drop_table('permission')


def downgrade():
    op.create_table(
        'permission',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('code', sa.String(length=64), nullable=False),
        sa.Column('description', sa.String(length=255), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('code'),
    )
    op.create_table(
        'role_permissions',
        sa.Column('role_id', sa.Integer(), nullable=False),
        sa.Column('permission_id', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['permission_id'], ['permission.id']),
        sa.ForeignKeyConstraint(['role_id'], ['role.id']),
        sa.PrimaryKeyConstraint('role_id', 'permission_id'),
    )
