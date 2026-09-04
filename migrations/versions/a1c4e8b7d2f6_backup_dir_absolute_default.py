"""backup_dir now stores an absolute path; default becomes /backups

Revision ID: a1c4e8b7d2f6
Revises: 8d9a1e2f3b4c
Create Date: 2026-09-04 00:00:00.000000

The backup directory previously defaulted to the relative string "backups",
resolved at runtime against the project root (/app in the container). That
resolution only worked because the CWD happened to be /app in every code
path — the digest block and scheduler task were one refactor away from
looking at the wrong directory.

It also blocked the incoming shared-volume fix: the deployment mounts a
persistent volume at /backups (outside /app, so it never collides with the
source bind mount in dev). To use it, the app must read from an absolute
path.

This migration:
  * Bumps the server_default from "backups" to "/backups".
  * Rewrites the existing single row's stored value from "backups" (or any
    other non-absolute path) to "/backups". Existing custom absolute paths
    are left alone.
"""

from alembic import op
import sqlalchemy as sa


revision = "a1c4e8b7d2f6"
down_revision = "8d9a1e2f3b4c"
branch_labels = None
depends_on = None


def upgrade():
    # MSSQL requires dropping the existing DEFAULT constraint before altering.
    op.execute(
        """
        DECLARE @cn sysname;
        SELECT @cn = dc.name
        FROM sys.default_constraints dc
        JOIN sys.columns c ON c.default_object_id = dc.object_id
        WHERE dc.parent_object_id = OBJECT_ID('app_settings')
          AND c.name = 'backup_dir';
        IF @cn IS NOT NULL EXEC('ALTER TABLE app_settings DROP CONSTRAINT ' + @cn);
        """
    )
    op.alter_column(
        "app_settings",
        "backup_dir",
        existing_type=sa.String(length=512),
        server_default="/backups",
        existing_nullable=False,
    )
    # Migrate any legacy relative value (only "backups" was ever produced by
    # the previous default, but be defensive: anything not starting with "/"
    # is treated as legacy).
    op.execute("UPDATE app_settings SET backup_dir = '/backups' WHERE backup_dir NOT LIKE '/%'")


def downgrade():
    op.execute(
        """
        DECLARE @cn sysname;
        SELECT @cn = dc.name
        FROM sys.default_constraints dc
        JOIN sys.columns c ON c.default_object_id = dc.object_id
        WHERE dc.parent_object_id = OBJECT_ID('app_settings')
          AND c.name = 'backup_dir';
        IF @cn IS NOT NULL EXEC('ALTER TABLE app_settings DROP CONSTRAINT ' + @cn);
        """
    )
    op.alter_column(
        "app_settings",
        "backup_dir",
        existing_type=sa.String(length=512),
        server_default="backups",
        existing_nullable=False,
    )
    op.execute("UPDATE app_settings SET backup_dir = 'backups' WHERE backup_dir = '/backups'")
