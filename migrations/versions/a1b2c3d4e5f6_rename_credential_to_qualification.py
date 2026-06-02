"""rename credential tables and permissions to qualification

Revision ID: a1b2c3d4e5f6
Revises: f8edde653722
Create Date: 2026-05-07 20:00:00.000000

"""
from alembic import op

revision = 'a1b2c3d4e5f6'
down_revision = '5bf568d4f009'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Rename tables
    op.rename_table('credential', 'qualification')
    op.rename_table('credential_parents', 'qualification_parents')
    op.rename_table('user_credentials', 'user_qualifications')
    op.rename_table('spot_credentials', 'spot_qualifications')
    op.rename_table('spot_template_credentials', 'spot_template_qualifications')

    # Fix foreign key column names inside renamed tables
    with op.batch_alter_table('qualification_parents') as batch_op:
        batch_op.alter_column('credential_id', new_column_name='qualification_id')

    with op.batch_alter_table('user_qualifications') as batch_op:
        batch_op.alter_column('credential_id', new_column_name='qualification_id')

    with op.batch_alter_table('spot_qualifications') as batch_op:
        batch_op.alter_column('credential_id', new_column_name='qualification_id')

    with op.batch_alter_table('spot_template_qualifications') as batch_op:
        batch_op.alter_column('credential_id', new_column_name='qualification_id')

    # Rename permission codes
    op.execute("""
        UPDATE permission SET code = 'qualification.view'   WHERE code = 'credential.view';
        UPDATE permission SET code = 'qualification.create' WHERE code = 'credential.create';
        UPDATE permission SET code = 'qualification.edit'   WHERE code = 'credential.edit';
        UPDATE permission SET code = 'qualification.delete' WHERE code = 'credential.delete';
        UPDATE permission SET code = 'user.assign_qualification' WHERE code = 'user.assign_credential';
    """)

    # Update entity_type in audit_log_entry
    op.execute("""
        UPDATE audit_log_entry SET entity_type = 'Qualification' WHERE entity_type = 'Credential';
    """)


def downgrade() -> None:
    op.execute("""
        UPDATE audit_log_entry SET entity_type = 'Credential' WHERE entity_type = 'Qualification';
    """)
    op.execute("""
        UPDATE permission SET code = 'credential.view'   WHERE code = 'qualification.view';
        UPDATE permission SET code = 'credential.create' WHERE code = 'qualification.create';
        UPDATE permission SET code = 'credential.edit'   WHERE code = 'qualification.edit';
        UPDATE permission SET code = 'credential.delete' WHERE code = 'qualification.delete';
        UPDATE permission SET code = 'user.assign_credential' WHERE code = 'user.assign_qualification';
    """)

    with op.batch_alter_table('spot_template_qualifications') as batch_op:
        batch_op.alter_column('qualification_id', new_column_name='credential_id')
    with op.batch_alter_table('spot_qualifications') as batch_op:
        batch_op.alter_column('qualification_id', new_column_name='credential_id')
    with op.batch_alter_table('user_qualifications') as batch_op:
        batch_op.alter_column('qualification_id', new_column_name='credential_id')
    with op.batch_alter_table('qualification_parents') as batch_op:
        batch_op.alter_column('qualification_id', new_column_name='credential_id')

    op.rename_table('spot_template_qualifications', 'spot_template_credentials')
    op.rename_table('spot_qualifications', 'spot_credentials')
    op.rename_table('user_qualifications', 'user_credentials')
    op.rename_table('qualification_parents', 'credential_parents')
    op.rename_table('qualification', 'credential')
