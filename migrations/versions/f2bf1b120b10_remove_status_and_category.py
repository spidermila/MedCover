"""remove equipment_item.status and equipment_type.category

Revision ID: f2bf1b120b10
Revises: e3fb60c87488
Create Date: 2026-07-26 00:00:00.000000

Availability is now derived purely from the maintenance window
(unavailability_since / unavailability_until), making the status flag
redundant.  All equipment types are shared, so the category column is
also removed.
"""
from alembic import op
import sqlalchemy as sa

revision = 'f2bf1b120b10'
down_revision = 'e3fb60c87488'
branch_labels = None
depends_on = None


def _drop_default_constraint(table: str, column: str) -> None:
    """Drop the auto-named DEFAULT constraint SQL Server creates for nullable columns.

    SQL Server assigns an unpredictable name (e.g. DF__equipment__statu__04E4BC85)
    that cannot be hard-coded.  We discover the constraint name at runtime and
    drop it before removing the column, which is required by SQL Server.
    """
    op.execute(f"""
        DECLARE @cn NVARCHAR(256)
        SELECT @cn = d.name
        FROM sys.default_constraints d
        JOIN sys.columns c
          ON d.parent_object_id = c.object_id
         AND d.parent_column_id = c.column_id
        WHERE c.object_id = OBJECT_ID(N'{table}') AND c.name = N'{column}'
        IF @cn IS NOT NULL
            EXEC(N'ALTER TABLE [{table}] DROP CONSTRAINT [' + @cn + N']')
    """)


def upgrade():
    _drop_default_constraint('equipment_item', 'status')
    op.drop_column('equipment_item', 'status')
    _drop_default_constraint('equipment_type', 'category')
    op.drop_column('equipment_type', 'category')


def downgrade():
    op.add_column(
        'equipment_item',
        sa.Column(
            'status',
            sa.String(length=50),
            nullable=False,
            server_default='available',
        ),
    )
    op.add_column(
        'equipment_type',
        sa.Column(
            'category',
            sa.String(length=50),
            nullable=False,
            server_default='shared',
        ),
    )
