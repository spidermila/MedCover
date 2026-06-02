"""add app_settings table

Revision ID: dfc13fca938f
Revises: d37d41a4e38d
Create Date: 2026-05-07 00:34:52.114203

"""
import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision = 'dfc13fca938f'
down_revision = 'd37d41a4e38d'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'app_settings',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('org_name', sa.String(length=255), nullable=True),
        sa.Column('timezone', sa.String(length=64), nullable=False),
        sa.Column('smtp_server', sa.String(length=255), nullable=True),
        sa.Column('smtp_port', sa.Integer(), nullable=False),
        sa.Column('smtp_use_tls', sa.Boolean(), nullable=False),
        sa.Column('smtp_username', sa.String(length=255), nullable=True),
        sa.Column('smtp_password_enc', sa.Text(), nullable=True),
        sa.Column('smtp_default_sender', sa.String(length=255), nullable=True),
        sa.Column('setup_complete', sa.Boolean(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade():
    op.drop_table('app_settings')
