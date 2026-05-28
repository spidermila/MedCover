"""add event_type enum, planned_participants_count, rename patients_count to post_event_count

Revision ID: c11afc34311c
Revises: c384ea97aef5
Create Date: 2026-05-11 08:07:43.665218

"""
import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision = 'c11afc34311c'
down_revision = 'c384ea97aef5'
branch_labels = None
depends_on = None


def upgrade():
    # Create the shared enum type first (used by both event and event_template)
    event_type_enum = sa.Enum('MEDICAL_COVER', 'TRAINING', 'PRESENTATION', name='event_type_enum')
    event_type_enum.create(op.get_bind(), checkfirst=True)

    with op.batch_alter_table('event', schema=None) as batch_op:
        # Rename patients_count → post_event_count to preserve existing data
        batch_op.alter_column('patients_count', new_column_name='post_event_count')
        batch_op.add_column(sa.Column('planned_participants_count', sa.Integer(), nullable=True))
        # Add event_type with server default so all existing rows get MEDICAL_COVER
        batch_op.add_column(sa.Column(
            'event_type',
            sa.Enum('MEDICAL_COVER', 'TRAINING', 'PRESENTATION', name='event_type_enum'),
            nullable=False,
            server_default='MEDICAL_COVER',
        ))

    # Remove the server default now that all existing rows have been populated
    with op.batch_alter_table('event', schema=None) as batch_op:
        batch_op.alter_column('event_type', server_default=None)

    with op.batch_alter_table('event_template', schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            'event_type',
            sa.Enum('MEDICAL_COVER', 'TRAINING', 'PRESENTATION', name='event_type_enum'),
            nullable=False,
            server_default='MEDICAL_COVER',
        ))

    with op.batch_alter_table('event_template', schema=None) as batch_op:
        batch_op.alter_column('event_type', server_default=None)


def downgrade():
    with op.batch_alter_table('event_template', schema=None) as batch_op:
        batch_op.drop_column('event_type')

    with op.batch_alter_table('event', schema=None) as batch_op:
        batch_op.drop_column('event_type')
        batch_op.drop_column('planned_participants_count')
        batch_op.alter_column('post_event_count', new_column_name='patients_count')

    sa.Enum(name='event_type_enum').drop(op.get_bind(), checkfirst=True)
