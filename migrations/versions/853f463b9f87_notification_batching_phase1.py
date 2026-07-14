"""notification batching phase 1

Revision ID: 853f463b9f87
Revises: 08afa74293a8
Create Date: 2026-07-14 00:00:00.000000

Adds structural scaffolding for issue #268 (batched event notifications):

* outbox_email: five nullable columns (user_id, event_id, change_type,
  change_value, send_after) + composite index (status, send_after) used
  by the future drain query.
* app_settings: four NOT NULL integer columns holding the proximity-tier
  delay values in minutes (defaults 5 / 60 / 360 / 1440).

No behaviour change: all new outbox_email columns default to NULL and
the existing send path leaves them unset. The delay tier values are
read-only in the admin UI at this phase.
"""
from alembic import op
import sqlalchemy as sa


revision = '853f463b9f87'
down_revision = '08afa74293a8'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('outbox_email', schema=None) as batch_op:
        batch_op.add_column(sa.Column('user_id', sa.Uuid(), nullable=True))
        batch_op.add_column(sa.Column('event_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('change_type', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('change_value', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('send_after', sa.DateTime(timezone=True), nullable=True))
        batch_op.create_foreign_key(
            'fk_outbox_email_user_id_user_account',
            'user_account',
            ['user_id'], ['id'],
            ondelete='SET NULL',
        )
        batch_op.create_foreign_key(
            'fk_outbox_email_event_id_event',
            'event',
            ['event_id'], ['id'],
            ondelete='SET NULL',
        )
        batch_op.create_index(
            'ix_outbox_email_status_send_after',
            ['status', 'send_after'],
            unique=False,
        )

    with op.batch_alter_table('app_settings', schema=None) as batch_op:
        batch_op.add_column(sa.Column('notify_delay_under_24h_min', sa.Integer(),
                                      server_default='5', nullable=False))
        batch_op.add_column(sa.Column('notify_delay_1_7_days_min', sa.Integer(),
                                      server_default='60', nullable=False))
        batch_op.add_column(sa.Column('notify_delay_1_4_weeks_min', sa.Integer(),
                                      server_default='360', nullable=False))
        batch_op.add_column(sa.Column('notify_delay_over_month_min', sa.Integer(),
                                      server_default='1440', nullable=False))


def downgrade():
    with op.batch_alter_table('app_settings', schema=None) as batch_op:
        batch_op.drop_column('notify_delay_over_month_min', mssql_drop_default=True)
        batch_op.drop_column('notify_delay_1_4_weeks_min', mssql_drop_default=True)
        batch_op.drop_column('notify_delay_1_7_days_min', mssql_drop_default=True)
        batch_op.drop_column('notify_delay_under_24h_min', mssql_drop_default=True)

    with op.batch_alter_table('outbox_email', schema=None) as batch_op:
        batch_op.drop_index('ix_outbox_email_status_send_after')
        batch_op.drop_constraint(
            'fk_outbox_email_event_id_event', type_='foreignkey'
        )
        batch_op.drop_constraint(
            'fk_outbox_email_user_id_user_account', type_='foreignkey'
        )
        batch_op.drop_column('send_after')
        batch_op.drop_column('change_value')
        batch_op.drop_column('change_type')
        batch_op.drop_column('event_id')
        batch_op.drop_column('user_id')
