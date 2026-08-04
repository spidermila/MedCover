"""add signature_image and signature_mimetype to user_account

Revision ID: 1d73232b0f18
Revises: f2bf1b120b10
Create Date: 2026-08-04 00:00:00.000000

Stores an optional handwritten-signature image per user. The image is embedded
into the user's monthly work-report xlsx so they no longer need to paste it in
by hand. Bytes are always PNG (mode L) after server-side processing; the
mimetype column is kept for future flexibility.
"""
from alembic import op
import sqlalchemy as sa

revision = '1d73232b0f18'
down_revision = 'f2bf1b120b10'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'user_account',
        sa.Column('signature_image', sa.LargeBinary(), nullable=True),
    )
    op.add_column(
        'user_account',
        sa.Column('signature_mimetype', sa.String(length=50), nullable=True),
    )


def downgrade():
    op.drop_column('user_account', 'signature_mimetype')
    op.drop_column('user_account', 'signature_image')
