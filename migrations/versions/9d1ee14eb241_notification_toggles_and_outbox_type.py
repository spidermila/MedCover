"""Add notification toggles to app_settings and notification_type to outbox_email.

Revision ID: 9d1ee14eb241
Revises: 953cffa1cb85
Create Date: 2026-05-10

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '9d1ee14eb241'
down_revision = 'd1e2f3a4b5c6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Notification toggles on app_settings (all enabled by default)
    op.add_column('app_settings', sa.Column('notify_assignment', sa.Boolean(), nullable=False, server_default='true'))
    op.add_column('app_settings', sa.Column('notify_event_lifecycle', sa.Boolean(), nullable=False, server_default='true'))
    op.add_column('app_settings', sa.Column('notify_event_cancelled', sa.Boolean(), nullable=False, server_default='true'))
    op.add_column('app_settings', sa.Column('notify_unfilled_reminder', sa.Boolean(), nullable=False, server_default='true'))
    op.add_column('app_settings', sa.Column('notify_debriefing', sa.Boolean(), nullable=False, server_default='true'))

    # Notification type label on outbox_email for traceability
    op.add_column('outbox_email', sa.Column('notification_type', sa.String(64), nullable=True))
    op.create_index(op.f('ix_outbox_email_notification_type'), 'outbox_email', ['notification_type'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_outbox_email_notification_type'), table_name='outbox_email')
    op.drop_column('outbox_email', 'notification_type')
    op.drop_column('app_settings', 'notify_debriefing')
    op.drop_column('app_settings', 'notify_unfilled_reminder')
    op.drop_column('app_settings', 'notify_event_cancelled')
    op.drop_column('app_settings', 'notify_event_lifecycle')
    op.drop_column('app_settings', 'notify_assignment')
