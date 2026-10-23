"""Add external collaborator role and explicit invitation scope.

Revision ID: 4a8b0c1d2e3f
Revises: 037b3977dcb5
"""
from alembic import op
import sqlalchemy as sa

revision = '4a8b0c1d2e3f'
down_revision = '037b3977dcb5'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('registration_invite', sa.Column('is_external', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.execute("INSERT INTO role (name) SELECT 'External' WHERE NOT EXISTS (SELECT 1 FROM role WHERE name = 'External')")


def downgrade():
    connection = op.get_bind()
    if connection.scalar(sa.text("SELECT COUNT(*) FROM user_roles ur JOIN role r ON r.id = ur.role_id WHERE r.name = 'External'")):
        raise RuntimeError('Cannot downgrade while external collaborators have the External role')
    if connection.scalar(sa.text('SELECT COUNT(*) FROM registration_invite WHERE is_external = 1 AND used_at IS NULL')):
        raise RuntimeError('Cannot downgrade while external registration invitations exist')
    op.execute("DELETE FROM role WHERE name = 'External'")
    op.drop_column('registration_invite', 'is_external', mssql_drop_default=True)
